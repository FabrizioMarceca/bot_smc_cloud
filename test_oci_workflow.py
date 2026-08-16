"""Regression checks for OCI CLI JMESPath queries."""

from pathlib import Path
import unittest


ROOT = Path(__file__).parent
SCRIPT = (ROOT / ".github" / "scripts" / "oci_provision.py").read_text(
    encoding="utf-8"
)
WORKFLOW = (ROOT / ".github" / "workflows" / "oci-arm-provision.yml").read_text(
    encoding="utf-8"
)


class OciWorkflowQueryTests(unittest.TestCase):
    def test_lifecycle_state_is_quoted_in_filters(self) -> None:
        self.assertNotIn("data[?lifecycle-state", SCRIPT)
        self.assertIn('"lifecycle-state"==`RUNNING`', SCRIPT)
        for state in ("PROVISIONING", "STARTING", "RUNNING", "STOPPING", "STOPPED", "TERMINATING"):
            self.assertIn(f'"lifecycle-state"==`{state}`', SCRIPT)

    def test_launch_timeout_is_fail_safe(self) -> None:
        self.assertIn("except subprocess.TimeoutExpired as exc:", SCRIPT)
        self.assertIn("verifico se OCI ha creato l'istanza", SCRIPT)
        self.assertIn("passo alla regione successiva", SCRIPT)
        self.assertIn("interrompo per evitare duplicati", SCRIPT)
        self.assertIn("find_instance_by_launch_token", SCRIPT)
        self.assertIn("def wait_for_launch_instance", SCRIPT)
        self.assertIn("wait_for_launch_instance(region, LAUNCH_TOKEN)", SCRIPT)
        self.assertIn('"provision_token": LAUNCH_TOKEN', SCRIPT)
        self.assertIn('"--freeform-tags", json.dumps({"provision_token": LAUNCH_TOKEN})', SCRIPT)
        self.assertIn('"--opc-request-id", request_id', SCRIPT)
        self.assertNotIn('"--cli-read-timeout"', SCRIPT)
        self.assertNotIn('"--cli-connection-timeout"', SCRIPT)
        self.assertIn('"--read-timeout", "180"', SCRIPT)
        self.assertIn('"oci",\n        "--read-timeout", "180",\n        "--connection-timeout", "30",\n        *args,', SCRIPT)
        self.assertIn('], timeout=205, stdin=SSH_PUBLIC_KEY)', SCRIPT)

    def test_launch_error_classes_are_explicit(self) -> None:
        self.assertIn("def classify_launch_error(message: str) -> str:", SCRIPT)
        self.assertIn('return "capacity"', SCRIPT)
        self.assertIn('outofhostcapacity', SCRIPT)
        self.assertIn('return "fatal"', SCRIPT)
        self.assertIn('return "unknown"', SCRIPT)
        self.assertIn('error_class = classify_launch_error(message)', SCRIPT)

    def test_global_cli_options_are_not_launch_options(self) -> None:
        self.assertNotIn('"--cli-read-timeout", "180",\n                    "--cli-connection-timeout", "30",', SCRIPT)

    def test_launch_errors_are_not_masked_or_silently_successful(self) -> None:
        self.assertIn('result.stderr or "", result.stdout or ""', SCRIPT)
        self.assertIn("errore launch fatale/sconosciuto", SCRIPT)
        self.assertIn('print("Nessuna capacità ARM disponibile nelle regioni candidate.")', SCRIPT)
        self.assertIn('print("Nessuna capacità ARM disponibile nelle regioni candidate.")\nsys.exit(1)', SCRIPT)

    def test_keepalive_runs_after_provision_failure(self) -> None:
        self.assertIn("if: always() && needs.provision.outputs.public_ip == ''", WORKFLOW)

    def test_hyphenated_public_ip_field_is_quoted(self) -> None:
        self.assertIn('data[?"public-ip" != null]', SCRIPT)
        self.assertIn('[0]."public-ip"', SCRIPT)


if __name__ == "__main__":
    unittest.main()
