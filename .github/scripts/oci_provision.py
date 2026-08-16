from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

COMPARTMENT_ID = os.environ.get("COMPARTMENT_ID", "").strip()
# Le availability domain appartengono al tenancy (root compartment):
# elencarle col root evita NotAuthorizedOrNotFound quando il secret
# OCI_COMPARTMENT_ID punta a un compartment diverso dal root.
TENANCY_ID = os.environ.get("OCI_TENANCY_ID", "").strip() or COMPARTMENT_ID
SSH_PUBLIC_KEY = os.environ["SSH_PUB"]
CANDIDATES = [item for item in os.environ.get("REGION_CANDIDATES", "").split("|") if "::" in item]
OUTPUT = Path(os.environ["GITHUB_OUTPUT"])
DEADLINE_SECONDS = int(os.environ.get("PROVISION_DEADLINE_SECONDS", "330"))
STARTED_AT = time.monotonic()
LAUNCH_TOKEN = f"{os.environ.get('GITHUB_RUN_ID', 'local')}-{uuid.uuid4().hex}"


def classify_launch_error(message: str) -> str:
    """Classifica un errore launch in ``capacity``, ``fatal`` o ``unknown``."""
    lowered = message.lower()
    if any(token in lowered for token in (
        "outofcapacity", "outofhostcapacity", "out of host capacity", "out of capacity",
        "toomanyrequests",
    )):
        return "capacity"
    if any(token in lowered for token in (
        "notauthorized", "not authorized", "invalidparameter",
        "invalid parameter", "limitexceeded", "serviceerror",
    )):
        return "fatal"
    return "unknown"


def is_configuration_error(message: str) -> bool:
    return classify_launch_error(message) == "fatal"


def remaining_seconds() -> int:
    return max(0, DEADLINE_SECONDS - int(time.monotonic() - STARTED_AT))


def oci_json(args: list[str], timeout: int = 45, stdin: str | None = None) -> dict:
    remaining = remaining_seconds()
    if remaining <= 0:
        raise TimeoutError("deadline provisioning OCI raggiunta")
    # Queste sono opzioni GLOBALI di OCI CLI: devono precedere il comando
    # (compute/network/iam), non essere aggiunte dopo "instance launch".
    command = [
        "oci",
        "--read-timeout", "180",
        "--connection-timeout", "30",
        *args,
        "--output", "json",
    ]
    result = subprocess.run(
        command,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=min(timeout, max(1, remaining)),
    )
    if result.returncode != 0:
        # OCI CLI può scrivere warning su stderr e il ServiceError su stdout:
        # non perdere nessuno dei due stream nella diagnosi.
        details = "\n".join(
            part.strip()
            for part in (result.stderr or "", result.stdout or "")
            if part and part.strip()
        )
        raise RuntimeError(details or "OCI command failed")
    return json.loads(result.stdout or "{}")


def output_values(**values: str) -> None:
    with OUTPUT.open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def public_ip(instance_id: str, region: str) -> str:
    data = oci_json([
        "compute", "instance", "list-vnics",
        "--instance-id", instance_id,
        "--region", region,
        "--query", 'data[?"public-ip" != null] | [0]."public-ip"',
    ], timeout=20)
    rows = data.get("data", [])
    return str(rows[0].get("public-ip", "")) if rows else ""


def wait_for_ip(instance_id: str, region: str) -> str:
    for _ in range(15):
        if remaining_seconds() <= 0:
            return ""
        try:
            ip = public_ip(instance_id, region)
            if ip and ip != "None":
                return ip
        except Exception:
            pass
        time.sleep(min(10, max(1, remaining_seconds())))
    return ""


def find_existing_instance(region: str) -> tuple[str, str, bool]:
    """Trova e riusa un'istanza esistente senza affidarsi all'ordine OCI.

    Dopo un timeout di ``launch`` possono coesistere più istanze con lo stesso
    display-name. La scelta deve privilegiare una RUNNING con IP, non il primo
    elemento restituito arbitrariamente dalla CLI.
    """
    data = oci_json([
        "compute", "instance", "list",
        "--compartment-id", COMPARTMENT_ID,
        "--display-name", "smc-bot-arm",
        "--all", "--region", region,
        "--query", 'data[?"lifecycle-state"==`PROVISIONING` || "lifecycle-state"==`STARTING` || "lifecycle-state"==`RUNNING` || "lifecycle-state"==`STOPPING` || "lifecycle-state"==`STOPPED` || "lifecycle-state"==`TERMINATING`].{id:id,state:"lifecycle-state"}',
    ])
    raw_instances = data.get("data", [])
    if isinstance(raw_instances, dict):
        raw_instances = [raw_instances]
    if not isinstance(raw_instances, list):
        return "", "", False

    priority = {
        "RUNNING": 0,
        "STARTING": 1,
        "PROVISIONING": 2,
        "STOPPING": 3,
        "STOPPED": 4,
        "TERMINATING": 5,
    }
    instances = sorted(
        (
            item for item in raw_instances
            if isinstance(item, dict) and item.get("id")
        ),
        key=lambda item: priority.get(str(item.get("state", "")), 99),
    )
    if not instances:
        return "", "", False

    # Prima cerca una VM RUNNING realmente raggiungibile. Se ce ne sono più
    # d'una, non lasciamo che una VM senza IP nasconda quella valida.
    for instance in instances:
        instance_id = str(instance["id"])
        state = str(instance.get("state", ""))
        if state != "RUNNING":
            continue
        try:
            ip = public_ip(instance_id, region)
        except Exception as exc:
            if is_configuration_error(str(exc)):
                sys.exit(2)
            continue
        if ip:
            return instance_id, ip, True

    # Se non c'è una RUNNING raggiungibile, riusa la prima istanza attiva o
    # riavvia quella STOPPED. In nessun caso si crea una seconda VM cieca.
    instance = instances[0]
    instance_id = str(instance["id"])
    state = str(instance.get("state", ""))

    if state == "STOPPED":
        print(f"[{region}] istanza STOPPED esistente: avvio {instance_id}")
        try:
            oci_json([
                "compute", "instance", "action",
                "--instance-id", instance_id,
                "--action", "START",
                "--region", region,
            ], timeout=30)
        except Exception as exc:
            message = str(exc)
            print(f"[{region}] impossibile avviare l'istanza STOPPED: {message}")
            if is_configuration_error(message):
                sys.exit(2)
            return instance_id, "", True

        became_running = False
        for _ in range(18):
            if remaining_seconds() <= 0:
                break
            try:
                current = oci_json([
                    "compute", "instance", "get",
                    "--instance-id", instance_id,
                    "--region", region,
                    "--query", 'data."lifecycle-state"',
                ], timeout=20).get("data", "")
                if current == "RUNNING":
                    became_running = True
                    break
            except Exception as exc:
                if is_configuration_error(str(exc)):
                    sys.exit(2)
            time.sleep(min(10, max(1, remaining_seconds())))
        if not became_running:
            print(f"[{region}] istanza STOPPED non diventata RUNNING")
            return instance_id, "", True

    return instance_id, wait_for_ip(instance_id, region), True


def find_instance_by_launch_token(region: str, token: str) -> str:
    """Correla un timeout launch solo con la risorsa creata da questo run."""
    data = oci_json([
        "compute", "instance", "list",
        "--compartment-id", COMPARTMENT_ID,
        "--display-name", "smc-bot-arm",
        "--all", "--region", region,
    ])
    matches = []
    for item in data.get("data", []):
        if not isinstance(item, dict):
            continue
        tags = item.get("freeform-tags") or {}
        if tags.get("provision_token") == token and item.get("id"):
            matches.append(item)
    priority = {
        "RUNNING": 0,
        "STARTING": 1,
        "PROVISIONING": 2,
        "STOPPING": 3,
        "STOPPED": 4,
        "TERMINATING": 5,
    }
    matches.sort(key=lambda item: priority.get(str(item.get("lifecycle-state", "")), 99))
    return str(matches[0]["id"]) if matches else ""


def wait_for_launch_instance(region: str, token: str, attempts: int = 6) -> str:
    """Attende la comparsa eventuale della VM dopo un timeout del launch.

    Il polling non ripete mai la creazione: serve solo a dare tempo a OCI di
    rendere visibile una richiesta già ricevuta dal backend.
    """
    for attempt in range(1, attempts + 1):
        if remaining_seconds() <= 0:
            break
        try:
            instance_id = find_instance_by_launch_token(region, token)
        except Exception as exc:
            message = str(exc)
            print(f"[{region}] verifica token tentativo {attempt} fallita: {message}")
            if is_configuration_error(message):
                raise
            instance_id = ""
        if instance_id:
            return instance_id
        if attempt < attempts:
            time.sleep(min(15, max(1, remaining_seconds())))
    return ""


def launch_in_region(region: str, subnet_id: str) -> tuple[str, str]:
    try:
        ads = oci_json([
            "iam", "availability-domain", "list",
            "--compartment-id", TENANCY_ID,
            "--region", region,
        ]).get("data", [])
        images = oci_json([
            "compute", "image", "list",
            "--compartment-id", COMPARTMENT_ID,
            "--operating-system", "Canonical Ubuntu",
            "--operating-system-version", "22.04",
            "--shape", "VM.Standard.A1.Flex",
            "--sort-by", "TIMECREATED", "--sort-order", "DESC", "--limit", "1",
            "--region", region,
        ]).get("data", [])
    except Exception as exc:
        message = str(exc)
        print(f"[{region}] impossibile leggere AD/immagine: {message}")
        if is_configuration_error(message):
            sys.exit(2)
        return "", ""

    image_id = images[0].get("id", "") if images else ""
    if not image_id:
        print(f"[{region}] immagine Ubuntu ARM non trovata")
        return "", ""

    try:
        existing_id, existing_ip, exists = find_existing_instance(region)
    except Exception as exc:
        message = str(exc)
        print(f"[{region}] impossibile leggere le istanze: {message}")
        if is_configuration_error(message):
            sys.exit(2)
        return "", ""

    if exists:
        if existing_id and existing_ip:
            return existing_id, existing_ip
        print(f"[{region}] istanza esistente senza IP pubblico: interrompo per evitare duplicati")
        sys.exit(2)

    for ad in ads:
        ad_name = ad.get("name", "")
        if not ad_name:
            continue
        for ocpus in (2, 3, 4):
            if remaining_seconds() <= 0:
                return "", ""
            memory = ocpus * 6
            print(f"[{region}] tentativo AD={ad_name} {ocpus} OCPU / {memory} GB")
            request_id = str(uuid.uuid4())
            try:
                result = oci_json([
                    "compute", "instance", "launch",
                    "--compartment-id", COMPARTMENT_ID,
                    "--availability-domain", ad_name,
                    "--subnet-id", subnet_id,
                    "--image-id", image_id,
                    "--shape", "VM.Standard.A1.Flex",
                    "--shape-config", json.dumps({"ocpus": ocpus, "memoryInGBs": memory}),
                    "--ssh-authorized-keys-file", "/dev/stdin",
                    "--display-name", "smc-bot-arm",
                    "--freeform-tags", json.dumps({"provision_token": LAUNCH_TOKEN}),
                    "--opc-request-id", request_id,
                    "--assign-public-ip", "true",
                    "--region", region,
                # Margine Python rispetto al read-timeout OCI: lasciamo al
                # processo CLI il tempo di chiudere e restituire la risposta.
                ], timeout=205, stdin=SSH_PUBLIC_KEY)
            except subprocess.TimeoutExpired as exc:
                # Un timeout del client non annulla necessariamente la richiesta
                # già ricevuta da OCI. Prima di provare un'altra shape/regione,
                # ricontrolliamo l'istanza per evitare duplicati.
                print(
                    f"[{region}] timeout launch dopo {exc.timeout}s; "
                    "verifico se OCI ha creato l'istanza"
                )
                try:
                    existing_id = wait_for_launch_instance(region, LAUNCH_TOKEN)
                except Exception as verify_exc:
                    print(
                        f"[{region}] esito launch non determinabile e verifica "
                        f"fallita: {verify_exc}"
                    )
                    sys.exit(2)
                if existing_id:
                    existing_ip = wait_for_ip(existing_id, region)
                    if existing_ip:
                        print(f"[{region}] riuso istanza dopo timeout: {existing_id}")
                        return existing_id, existing_ip
                    print(
                        f"[{region}] istanza del run esistente senza IP; "
                        "interrompo per evitare duplicati"
                    )
                    sys.exit(2)
                print(
                    f"[{region}] esito launch non determinabile: nessuna istanza "
                    "con token del run; interrompo senza altri tentativi"
                )
                sys.exit(2)

            except Exception as exc:
                message = str(exc)
                error_class = classify_launch_error(message)
                if error_class == "capacity":
                    print(f"[{region}] capacità assente, continuo")
                    continue
                if error_class == "fatal":
                    print(f"[{region}] errore configurazione/autorizzazione: {message[-300:]}")
                    sys.exit(2)
                print(f"[{region}] errore launch fatale/sconosciuto:\n{message}")
                sys.exit(2)

            instance_id = str(result.get("data", {}).get("id", ""))
            if instance_id:
                ip = wait_for_ip(instance_id, region)
                if ip:
                    return instance_id, ip
                print(f"[{region}] istanza {instance_id} creata ma IP non disponibile; interrompo per evitare duplicati")
                sys.exit(2)

    return "", ""


if not CANDIDATES:
    print("ERRORE: nessuna coppia regione/subnet candidata")
    sys.exit(1)

for candidate in CANDIDATES:
    region, subnet = candidate.split("::", 1)
    print(f"Regione candidata: {region} | subnet: {subnet}")
    instance_id, ip = launch_in_region(region, subnet)
    if instance_id and ip:
        print(f"SUCCESSO: regione={region} subnet={subnet} ip={ip}")
        output_values(
            public_ip=ip,
            selected_region=region,
            selected_subnet=subnet,
        )
        sys.exit(0)
    if remaining_seconds() <= 0:
        break

print("Nessuna capacità ARM disponibile nelle regioni candidate.")
sys.exit(1)
