"""
webhook_server.py
=================
Server Flask per:
  - Ricevere segnali da TradingView via webhook (POST /webhook)
  - Dashboard web real-time per il terminale Bot SMC (GET /dashboard)
  - API stato in tempo reale (GET /api/status)
  - API performance P&L (GET /api/performance) per grafico a torta
  - Health check (GET /health)

Creato da run_master.py con la factory create_app(), questo modulo NON
inizializza MT5 autonomamente: riceve l'engine e il notifier gia' pronti.

Endpoint:
    POST /webhook       -> riceve payload JSON con entry/sl/tp/symbol/side
    GET  /dashboard      -> dashboard web interattiva (HTML/CSS/JS embedded)
    GET  /api/status     -> JSON stato bot in tempo reale
    GET  /api/performance -> JSON P&L giornaliero/settimanale/mensile
    GET  /health         -> health check

Sicurezza: il webhook valida il campo 'token' contro WEBHOOK_SECRET_TOKEN.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from flask import Flask, jsonify, redirect, render_template_string, request, send_file

from mt5_engine import MT5Engine
from trade_manager import (
    InvalidSignalError,
    LotSizingError,
    TradeValidator,
    mt5_symbol_info_provider,
    MAGIC_MAIN,
)
from position_monitor import get_tracker
import utils

logger = logging.getLogger(__name__)

_BOT_START_TIME = time.time()

# Import pigro di smc_engine (potrebbe non essere disponibile in ambienti senza MT5)
try:
    from smc_engine import get_current_session as _get_current_session_raw
    from smc_engine import is_near_news_hour as _is_near_news_hour_raw
    from smc_engine import get_dxy_bias as _get_dxy_bias_raw
    from smc_signals import detect_dxy_conflict as _detect_dxy_conflict_raw
except ImportError:
    _get_current_session_raw = None  # type: ignore[assignment]
    _is_near_news_hour_raw = None  # type: ignore[assignment]
    _get_dxy_bias_raw = None  # type: ignore[assignment]
    _detect_dxy_conflict_raw = None  # type: ignore[assignment]


def _get_session_safe() -> str:
    try:
        if _get_current_session_raw:
            return _get_current_session_raw()
    except Exception:
        pass
    return "--"


def _get_dxy_bias_safe() -> str:
    """Trend del DXY come stringa ('bullish'/'bearish'/...).

    ``get_dxy_bias()`` ritorna un dict: estraiamo solo il trend, che e' cio'
    che la dashboard e il filtro conflitto DXY consumano.
    """
    try:
        if _get_dxy_bias_raw:
            dxy = _get_dxy_bias_raw()
            if dxy and dxy.get("trend"):
                return dxy["trend"]
    except Exception:
        pass
    return "N/D"


def _get_near_news_safe() -> bool:
    try:
        if _is_near_news_hour_raw:
            return _is_near_news_hour_raw()
    except Exception:
        pass
    return False


def _get_field(data: dict, *keys: str, default=None):
    """Estrae un campo dal payload, provando nomi multipli (it/en)."""
    for key in keys:
        val = data.get(key)
        if val is not None:
            return val
    return default


def infer_trading_mode(raw_timeframe: object) -> Optional[str]:
    """Deprecated: il timeframe non determina più la modalità del webhook.

    La strategia richiede che ogni segnale dichiari esplicitamente ``mode``;
    mantenere questa funzione solo per compatibilità di import, senza usarla
    per accettare o classificare ordini.
    """
    return None


# ==========================================================================
# DASHBOARD HTML/CSS/JS (embedded, no external files)
# ==========================================================================

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SMC Bot · Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <style>
        :root {
            --bg: #0a0e17;
            --panel: #111827;
            --panel-hover: #1a2332;
            --border: #1f2937;
            --text: #f3f4f6;
            --muted: #9ca3af;
            --green: #10b981;
            --green-glow: #34d399;
            --red: #ef4444;
            --red-glow: #f87171;
            --blue: #3b82f6;
            --yellow: #f59e0b;
            --purple: #8b5cf6;
            --orange: #f97316;
            --radius: 10px;
            --shadow: 0 4px 24px rgba(0,0,0,0.4);
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Segoe UI', 'Inter', system-ui, -apple-system, sans-serif;
            background: var(--bg);
            color: var(--text);
            min-height: 100vh;
            line-height: 1.5;
            overflow-x: hidden;
        }

        body::before {
            content: '';
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background:
                radial-gradient(ellipse at 20% 50%, rgba(16,185,129,0.04) 0%, transparent 50%),
                radial-gradient(ellipse at 80% 20%, rgba(59,130,246,0.04) 0%, transparent 50%);
            pointer-events: none;
            z-index: 0;
        }

        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
            position: relative;
            z-index: 1;
        }

        /* --- HEADER --- */
        header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 12px;
            margin-bottom: 20px;
            padding: 16px 20px;
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            box-shadow: var(--shadow);
        }

        .logo { font-size: 1.4rem; font-weight: 700; letter-spacing: -0.5px; }
        .logo span { color: var(--green); }

        .status-pill {
            display: inline-flex;
            align-items: center;
            gap: 7px;
            padding: 6px 14px;
            border-radius: 999px;
            font-size: 0.85rem;
            font-weight: 600;
        }
        .status-pill.online { background: rgba(16,185,129,0.15); color: var(--green); border: 1px solid rgba(16,185,129,0.3); }
        .status-pill.offline { background: rgba(239,68,68,0.15); color: var(--red); border: 1px solid rgba(239,68,68,0.3); }
        .status-pill.unknown { background: rgba(245,158,11,0.15); color: var(--yellow); border: 1px solid rgba(245,158,11,0.3); }
        .status-dot {
            width: 8px; height: 8px; border-radius: 50%; display: inline-block;
            animation: pulse 2s infinite;
        }
        .status-dot.green { background: var(--green-glow); box-shadow: 0 0 8px var(--green); }
        .status-dot.red { background: var(--red-glow); box-shadow: 0 0 8px var(--red); animation: none; }
        .status-dot.yellow { background: var(--yellow); box-shadow: 0 0 8px var(--yellow); }

        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.4; }
        }

        .header-stats {
            display: flex;
            gap: 20px;
            flex-wrap: wrap;
            font-size: 0.85rem;
            color: var(--muted);
        }
        .header-stats strong { color: var(--text); }

        /* --- GRID --- */
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
            gap: 16px;
            margin-bottom: 16px;
        }

        .grid-2 { grid-template-columns: repeat(2, 1fr); }
        .grid-3 { grid-template-columns: repeat(3, 1fr); }
        @media (max-width: 900px) { .grid-2, .grid-3 { grid-template-columns: 1fr; } }

        /* --- CARDS --- */
        .card {
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            box-shadow: var(--shadow);
            overflow: hidden;
            transition: border-color 0.2s, box-shadow 0.2s;
        }
        .card:hover {
            border-color: #2d3a4a;
            box-shadow: 0 6px 32px rgba(0,0,0,0.5);
        }

        .card-header {
            padding: 12px 16px;
            border-bottom: 1px solid var(--border);
            font-weight: 600;
            font-size: 0.9rem;
            display: flex;
            align-items: center;
            gap: 8px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .card-header .emoji { font-size: 1.2rem; }
        .card-body { padding: 14px 16px; }

        /* --- MODE CARDS --- */
        .mode-card {
            border-top: 3px solid transparent;
            min-height: 260px;
        }

        .mode-card.daytrading { border-top-color: var(--blue); }
        .mode-card.swing { border-top-color: var(--purple); }

        .mode-card .mode-badge {
            display: inline-block;
            padding: 3px 10px;
            border-radius: 999px;
            font-size: 0.7rem;
            font-weight: 700;
            letter-spacing: 0.5px;
        }

        .mode-badge.daytrading { background: rgba(59,130,246,0.15); color: var(--blue); }
        .mode-badge.swing { background: rgba(139,92,246,0.15); color: var(--purple); }

        .mode-meta {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 6px 12px;
            font-size: 0.78rem;
            color: var(--muted);
            margin-top: 8px;
        }
        .mode-meta strong { color: var(--text); }

        /* --- POSITION ROW --- */
        .pos-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 8px 10px;
            margin-top: 6px;
            border-radius: 6px;
            background: rgba(255,255,255,0.02);
            border: 1px solid var(--border);
            font-size: 0.78rem;
            gap: 8px;
            flex-wrap: wrap;
        }
        .pos-row .pnl { font-weight: 700; }
        .pos-row .pnl.pos { color: var(--green); }
        .pos-row .pnl.neg { color: var(--red); }
        .pos-row .pos-tag {
            padding: 2px 7px;
            border-radius: 4px;
            font-size: 0.7rem;
            font-weight: 700;
        }
        .pos-tag.buy { background: rgba(16,185,129,0.15); color: var(--green); }
        .pos-tag.sell { background: rgba(239,68,68,0.15); color: var(--red); }

        .no-positions {
            text-align: center;
            color: var(--muted);
            font-size: 0.78rem;
            padding: 16px 0;
        }
        .no-positions::before {
            content: '\\23F3';
            display: block;
            font-size: 1.8rem;
            margin-bottom: 4px;
        }

        /* --- PIPELINE STATS --- */
        .stat-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 6px 0;
            border-bottom: 1px solid rgba(255,255,255,0.03);
        }
        .stat-row:last-child { border-bottom: none; }
        .stat-value {
            font-weight: 700;
            font-variant-numeric: tabular-nums;
            font-family: 'JetBrains Mono', 'Fira Code', 'Consolas', monospace;
        }

        /* --- MARKET CONTEXT --- */
        .context-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px 14px;
        }
        .context-item {
            display: flex;
            flex-direction: column;
        }
        .context-label { font-size: 0.7rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }
        .context-value { font-size: 0.9rem; font-weight: 600; }

        /* --- BUTTONS --- */
        .btn {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 7px 16px;
            border-radius: 8px;
            font-size: 0.82rem;
            font-weight: 600;
            cursor: pointer;
            border: 1px solid var(--border);
            background: var(--panel);
            color: var(--text);
            transition: all 0.2s;
            white-space: nowrap;
        }
        .btn:hover { border-color: var(--green); background: rgba(16,185,129,0.08); }
        .btn.active { border-color: var(--green); background: rgba(16,185,129,0.15); color: var(--green); }

        /* Session table */
        .session-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.78rem;
        }
        .session-table th {
            text-align: left;
            padding: 6px 10px;
            color: var(--muted);
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            border-bottom: 1px solid var(--border);
        }
        .session-table td {
            padding: 8px 10px;
            border-bottom: 1px solid rgba(255,255,255,0.03);
        }
        .session-table tr.active-session td {
            background: rgba(16,185,129,0.06);
        }
        .session-table tr.active-session td:first-child {
            border-left: 3px solid var(--green);
            padding-left: 7px;
            border-radius: 3px 0 0 3px;
        }

        /* Pie chart */
        .chart-wrap {
            position: relative;
            width: 100%;
            max-height: 280px;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .chart-wrap canvas { max-height: 260px; }
        .chart-tabs {
            display: flex;
            gap: 4px;
            margin-left: auto;
        }
        .chart-tab {
            padding: 4px 12px;
            border-radius: 6px;
            font-size: 0.72rem;
            cursor: pointer;
            border: 1px solid var(--border);
            background: transparent;
            color: var(--muted);
            transition: all 0.2s;
        }
        .chart-tab:hover { border-color: var(--green); color: var(--text); }
        .chart-tab.active { border-color: var(--green); background: rgba(16,185,129,0.12); color: var(--green); }

        /* --- LOGS --- */
        .log-container {
            max-height: 320px;
            overflow-y: auto;
            font-family: 'JetBrains Mono', 'Fira Code', 'Consolas', monospace;
            font-size: 0.7rem;
            line-height: 1.6;
        }
        .log-container::-webkit-scrollbar { width: 5px; }
        .log-container::-webkit-scrollbar-track { background: transparent; }
        .log-container::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
        .log-line {
            padding: 3px 8px;
            border-left: 2px solid transparent;
            white-space: pre-wrap;
            word-break: break-all;
        }
        .log-line.warn { color: var(--yellow); border-left-color: var(--yellow); }
        .log-line.error { color: var(--red); border-left-color: var(--red); }
        .log-line.info { color: var(--muted); }

        /* --- FOOTER --- */
        footer {
            text-align: center;
            color: var(--muted);
            font-size: 0.7rem;
            padding: 16px 0 8px;
            opacity: 0.6;
        }
        footer span { color: var(--green); }

        .refresh-indicator {
            font-size: 0.7rem;
            color: var(--muted);
            animation: fadeInOut 0.5s ease;
        }
        @keyframes fadeInOut {
            0% { opacity: 0; }
            50% { opacity: 1; }
            100% { opacity: 0.5; }
        }

        /* --- LOG FILTER + ACTION BUTTONS --- */
        #log-filter {
            margin-left: auto;
            background: var(--bg);
            color: var(--text);
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 3px 8px;
            font-size: 0.72rem;
            cursor: pointer;
            outline: none;
        }
        #log-filter:hover { border-color: var(--green); }

        .log-btn {
            background: var(--bg);
            color: var(--text);
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 3px 10px;
            font-size: 0.72rem;
            cursor: pointer;
            transition: border-color 0.2s, background 0.2s, color 0.2s;
            white-space: nowrap;
        }
        .log-btn:hover { border-color: var(--green); background: rgba(16,185,129,0.08); }
        .log-btn.danger:hover { border-color: var(--red); background: rgba(239,68,68,0.08); color: var(--red); }

        .section-title {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 14px;
            flex-wrap: wrap;
            gap: 8px;
        }
        .section-title h2 {
            font-size: 1rem;
            font-weight: 700;
        }

        @keyframes slideDown {
            from { opacity: 0; transform: translateY(-10px); }
            to { opacity: 1; transform: translateY(0); }
        }
        .slide-down { animation: slideDown 0.3s ease; }

        @media (max-width: 600px) {
            .container { padding: 10px; }
            header { flex-direction: column; align-items: flex-start; }
            .header-stats { flex-direction: column; gap: 4px; }
            .grid { grid-template-columns: 1fr; }
            .context-grid { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
<div class="container">

    <!-- ===== HEADER ===== -->
    <header>
        <div>
            <div class="logo">\U0001F916 SMC <span>Trading Bot</span></div>
            <span class="refresh-indicator" id="refresh-ts">caricamento...</span>
        </div>
        <div id="status-pill" class="status-pill unknown">
            <span class="status-dot yellow"></span> Connessione...
        </div>
        <div class="header-stats">
            <span>\u23F1\uFE0F <strong id="uptime">--</strong></span>
            <span>\U0001F4B0 <strong id="balance">--</strong></span>
            <span>\U0001F4C8 <strong id="equity">--</strong></span>
            <span>\U0001F4B5 <strong id="pnl-total">--</strong></span>
        </div>
    </header>

    <!-- ===== ROW 0: BUTTONS ===== -->
    <div class="section-title">
        <h2>\U0001F4CA Modalit\u00E0 di Trading</h2>
        <div style="display:flex;gap:8px;flex-wrap:wrap;">
            <button class="btn active" id="btn-mode-cards" onclick="showModeCards()">\U0001F5C2\uFE0F Per Modalit\u00E0</button>
            <button class="btn" id="btn-view-all" onclick="showViewAll()">\U0001F50D Visualizza Tutto</button>
        </div>
    </div>

    <!-- ===== ROW 1: MODE CARDS (default) / VIEW ALL ===== -->
    <div id="view-mode-cards">
        <div class="grid grid-3" id="mode-cards">
            <!-- Filled by JS -->
        </div>
    </div>
    <div id="view-all-table" style="display:none;">
        <div class="card slide-down">
            <div class="card-header">
                <span>\U0001F4CB Tutte le Posizioni</span>
                <span class="mode-badge" id="all-pos-count" style="background:rgba(245,158,11,0.15);color:var(--yellow);margin-left:auto;">0</span>
            </div>
            <div class="card-body" id="all-positions" style="max-height:450px;overflow-y:auto;">caricamento...</div>
        </div>
    </div>

    <!-- ===== ROW 2: MARKET SESSIONS + PIE CHART ===== -->
    <div class="grid grid-2">
        <!-- Market Sessions -->
        <div class="card">
            <div class="card-header">\U0001F310 Orari Mercati (UTC)</div>
            <div class="card-body">
                <table class="session-table">
                    <thead>
                        <tr><th>Sessione</th><th>Orario (UTC)</th><th>Stato</th></tr>
                    </thead>
                    <tbody>
                        <tr id="sess-row-asia"><td>\U0001F1EF\U0001F1F5 Asia</td><td>00:00 \u2013 09:00</td><td id="sess-asia">\u26AA Inattiva</td></tr>
                        <tr id="sess-row-london"><td>\U0001F1EC\U0001F1E7 London</td><td>08:00 \u2013 17:00</td><td id="sess-london">\u26AA Inattiva</td></tr>
                        <tr id="sess-row-ny"><td>\U0001F1FA\U0001F1F8 New York</td><td>13:00 \u2013 22:00</td><td id="sess-ny">\u26AA Inattiva</td></tr>
                    </tbody>
                </table>
                <div style="margin-top:10px;padding:8px 12px;border-radius:6px;background:rgba(255,255,255,0.02);border:1px solid var(--border);font-size:0.75rem;color:var(--muted);">
                    \u23F0 Ora UTC: <strong id="utc-now" style="color:var(--text);">--</strong> &nbsp;|&nbsp;
                    Sessione attiva: <strong id="active-session-label" style="color:var(--green);">--</strong>
                </div>
            </div>
        </div>

        <!-- Performance Pie Chart -->
        <div class="card">
            <div class="card-header">
                <span>\U0001F4CA Andamento P&amp;L</span>
                <div class="chart-tabs">
                    <button class="chart-tab active" onclick="switchPerf('daily', this)">Oggi</button>
                    <button class="chart-tab" onclick="switchPerf('weekly', this)">Settimana</button>
                    <button class="chart-tab" onclick="switchPerf('monthly', this)">Mese</button>
                </div>
            </div>
            <div class="card-body">
                <div class="chart-wrap"><canvas id="perf-chart"></canvas></div>
                <div style="text-align:center;margin-top:8px;font-size:0.8rem;">
                    <span>\U0001F4B0 Totale: <strong id="perf-total" style="font-family:'JetBrains Mono','Consolas',monospace;">--</strong></span>
                </div>
            </div>
        </div>
    </div>

    <!-- ===== ROW 3: PIPELINE + MARKET + ERRORI ===== -->
    <div class="grid grid-3">
        <div class="card">
            <div class="card-header">\U0001F4CA Pipeline Stats</div>
            <div class="card-body" id="pipeline-stats">caricamento...</div>
        </div>
        <div class="card">
            <div class="card-header">\U0001F30D Contesto Mercato</div>
            <div class="card-body context-grid" id="market-context">caricamento...</div>
        </div>
        <div class="card">
            <div class="card-header">
                <span>\u274C Errori</span>
                <span class="mode-badge" id="error-count"
                      style="background:rgba(239,68,68,0.15);color:var(--red);margin-left:auto;">0</span>
            </div>
            <div class="card-body log-container" id="error-list">caricamento...</div>
        </div>
    </div>

    <!-- ===== ROW 4: POSIZIONI CHIUSE ===== -->
    <div class="card">
        <div class="card-header">
            <span>\U0001F4C1 Posizioni Chiuse</span>
            <span style="font-weight:400;color:var(--muted);font-size:0.7rem;">(ultime 24h)</span>
            <span class="mode-badge" id="closed-count"
                  style="background:rgba(139,92,246,0.15);color:var(--purple);margin-left:auto;">0</span>
        </div>
        <div class="card-body" id="closed-positions">caricamento...</div>
    </div>

    <!-- ===== ROW 4.5: STORICO POSIZIONI (daily/weekly/monthly/custom) ===== -->
    <div class="card">
        <div class="card-header">
            <span>\U0001F4DA Storico Posizioni</span>
            <div class="chart-tabs" id="hist-tabs">
                <button class="chart-tab active" onclick="switchHistory('daily', this)">Giornaliero</button>
                <button class="chart-tab" onclick="switchHistory('weekly', this)">Settimanale</button>
                <button class="chart-tab" onclick="switchHistory('monthly', this)">Mensile</button>
                <button class="chart-tab" onclick="switchHistory('custom', this)">Personalizzato</button>
            </div>
        </div>
        <div class="card-body">
            <!-- Date pickers (visibili solo in modalita' custom) -->
            <div id="hist-date-range" style="display:none;margin-bottom:10px;gap:10px;align-items:center;flex-wrap:wrap;">
                <label style="font-size:0.75rem;color:var(--muted);">Da:</label>
                <input type="datetime-local" id="hist-from" style="background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:4px 8px;font-size:0.75rem;">
                <label style="font-size:0.75rem;color:var(--muted);">A:</label>
                <input type="datetime-local" id="hist-to" style="background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:4px 8px;font-size:0.75rem;">
                <button class="btn" onclick="fetchHistory()" style="padding:4px 14px;font-size:0.75rem;">\U0001F50D Cerca</button>
            </div>
            <!-- Summary bar -->
            <div id="hist-summary" style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:10px;padding:8px 12px;border-radius:6px;background:rgba(255,255,255,0.02);border:1px solid var(--border);font-size:0.78rem;">
                <span>\U0001F4B0 P&amp;L: <strong id="hist-pnl" style="font-family:'JetBrains Mono','Consolas',monospace;">--</strong></span>
                <span>\U0001F3AF Win Rate: <strong id="hist-winrate" style="font-family:'JetBrains Mono','Consolas',monospace;">--</strong></span>
                <span>\u2705 Wins: <strong id="hist-wins" style="color:var(--green);">0</strong></span>
                <span>\u274C Losses: <strong id="hist-losses" style="color:var(--red);">0</strong></span>
                <span>\U0001F4CB Totale: <strong id="hist-count">0</strong></span>
            </div>
            <!-- Positions table -->
            <div id="hist-positions" style="max-height:350px;overflow-y:auto;">caricamento...</div>
        </div>
    </div>

    <!-- ===== ROW 5: LOGS ===== -->
    <div class="card">
        <div class="card-header">
            <span>\U0001F4DC Log Recenti</span>
            <span style="font-weight:400;color:var(--muted);font-size:0.7rem;" id="log-count">(ultime 100 righe)</span>
            <select id="log-filter" title="Filtra per livello">
                <option value="all">Tutti</option>
                <option value="INFO">INFO+</option>
                <option value="WARN">WARN+</option>
                <option value="ERROR">ERROR+</option>
            </select>
            <button class="log-btn" id="btn-export" onclick="exportLogs()" title="Scarica il file dei log completo">\u2B07\uFE0F Esporta</button>
            <button class="log-btn danger" id="btn-clear" onclick="clearLogs()" title="Svuota il file dei log">\U0001F5D1\uFE0F Svuota</button>
        </div>
        <div class="card-body log-container" id="logs">caricamento...</div>
    </div>

    <footer>
        SMC Trading Bot · <span>Multi-Mode</span> (daytrading + swing)
    </footer>
</div>

<script>
const API = '/api/status';
const PERF_API = '/api/performance';
let pollTimer = null;
let perfChart = null;
let perfPeriod = 'daily';
let lastPerfData = null;
let lastStatusData = null;

function fmtNum(n, dec) {
    if (n == null || isNaN(n)) return '--';
    return Number(n).toFixed(dec || 2);
}

function fmtTime(sec) {
    if (sec == null || isNaN(sec)) return '--';
    const s = Math.floor(Number(sec));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const ss = s % 60;
    if (h > 0) return h + 'h ' + m + 'm ' + ss + 's';
    if (m > 0) return m + 'm ' + ss + 's';
    return ss + 's';
}

function fmtPips(n, sym) {
    if (n == null || isNaN(n)) return '--';
    const v = Number(n);
    const dec = (sym || '').includes('XAU') ? 2 : 4;
    return v.toFixed(dec);
}

// ================================================================
// VIEW TOGGLE: Mode Cards vs View All
// ================================================================
function showModeCards() {
    document.getElementById('view-mode-cards').style.display = 'block';
    document.getElementById('view-all-table').style.display = 'none';
    document.getElementById('btn-mode-cards').classList.add('active');
    document.getElementById('btn-view-all').classList.remove('active');
    if (lastStatusData) updateModeCards(lastStatusData);
}

function showViewAll() {
    document.getElementById('view-mode-cards').style.display = 'none';
    document.getElementById('view-all-table').style.display = 'block';
    document.getElementById('btn-mode-cards').classList.remove('active');
    document.getElementById('btn-view-all').classList.add('active');
    if (lastStatusData) updateAllPositions(lastStatusData);
}

function updateAllPositions(d) {
    const modes = ['daytrading', 'swing'];
    const emojis = { daytrading: '\U0001F4CA', swing: '\U0001F3D7\uFE0F' };
    const colormap = { daytrading: 'var(--blue)', swing: 'var(--purple)' };
    const all = [];
    for (const mode of modes) {
        const cfg = (d.modes || {})[mode] || {};
        for (const p of (cfg.positions || [])) {
            all.push({ ...p, mode: mode });
        }
    }
    const el = document.getElementById('all-positions');
    const countEl = document.getElementById('all-pos-count');
    if (countEl) countEl.textContent = all.length;

    if (all.length === 0) {
        el.innerHTML = '<div class="no-positions">Nessuna posizione attiva in nessuna modalit\u00E0</div>';
        return;
    }

    let html = '';
    for (const p of all) {
        const pnlClass = (p.pnl_pips || 0) >= 0 ? 'pos' : 'neg';
        const sideClass = (p.direction || '').toUpperCase() === 'BUY' ? 'buy' : 'sell';
        const rrStr = p.rr != null ? fmtNum(p.rr, 1) : '--';
        const modeEmoji = emojis[p.mode] || '';
        const modeColor = colormap[p.mode] || 'var(--muted)';
        const pendingTag = p.type === 'PENDING'
            ? '<span class="pos-tag" style="background:rgba(249,115,22,0.15);color:#f97316">IN ATTESA</span>'
            : '';
        html += '<div class="pos-row">' +
            '<span class="pos-tag ' + sideClass + '">' + (p.direction || '--') + '</span>' +
            '<span style="color:' + modeColor + ';font-weight:700;">' + modeEmoji + ' ' + p.mode.toUpperCase() + '</span>' +
            '<span>#' + (p.ticket || '--') + '</span>' +
            pendingTag +
            '<span>E: ' + fmtPips(p.entry, p.symbol || d.symbol) + '</span>' +
            '<span>SL: ' + fmtPips(p.sl, p.symbol || d.symbol) + '</span>' +
            '<span>TP: ' + fmtPips(p.tp, p.symbol || d.symbol) + '</span>' +
            '<span class="pnl ' + pnlClass + '">' + ((p.pnl_pips || 0) >= 0 ? '+' : '') + fmtPips(p.pnl_pips, p.symbol || d.symbol) + ' pip</span>' +
            '<span style="color:var(--muted)">RR ' + rrStr + '</span>' +
            '</div>';
    }
    el.innerHTML = html;
}

// ================================================================
// HEADER
// ================================================================
function updateHeader(d) {
    const pill = document.getElementById('status-pill');
    const dot = pill.querySelector('.status-dot');
    pill.className = 'status-pill';
    dot.className = 'status-dot';

    if (d.mt5_connected) {
        pill.classList.add('online');
        dot.classList.add('green');
        pill.innerHTML = '<span class="status-dot green"></span> MT5 Connesso';
    } else if (d.bot_running) {
        pill.classList.add('unknown');
        dot.classList.add('yellow');
        pill.innerHTML = '<span class="status-dot yellow"></span> MT5 Disconnesso';
    } else {
        pill.classList.add('offline');
        dot.classList.add('red');
        pill.innerHTML = '<span class="status-dot red"></span> Bot Fermo';
    }

    document.getElementById('uptime').textContent = fmtTime(d.uptime_seconds);
    document.getElementById('balance').textContent = '$' + fmtNum(d.balance, 2);
    document.getElementById('equity').textContent = '$' + fmtNum(d.equity, 2);

    const pnlEl = document.getElementById('pnl-total');
    const pnl = d.pnl;
    if (pnl != null && !isNaN(pnl)) {
        pnlEl.textContent = (pnl >= 0 ? '+' : '') + '$' + fmtNum(pnl, 2);
        pnlEl.style.color = pnl >= 0 ? 'var(--green)' : 'var(--red)';
    } else {
        pnlEl.textContent = '--';
        pnlEl.style.color = 'var(--muted)';
    }

    document.getElementById('refresh-ts').textContent = 'Aggiornato: ' + (d.timestamp || '--');

    updateSessionRows(d.market);
}

// ================================================================
// MARKET SESSIONS
// ================================================================
function updateSessionRows(mc) {
    if (!mc) return;
    const currentSession = mc.session || '';
    const sessions = ['asian', 'london', 'newyork'];
    const names = { asian: 'Asia', london: 'London', newyork: 'New York' };

    for (const s of sessions) {
        const el = document.getElementById('sess-' + s);
        const row = document.getElementById('sess-row-' + s);
        if (!el) continue;
        if (currentSession === s) {
            el.innerHTML = '<span style="color:var(--green);font-weight:700;">\U0001F7E2 Attiva</span>';
            if (row) row.classList.add('active-session');
        } else {
            el.innerHTML = '<span style="color:var(--muted);">\u26AA Inattiva</span>';
            if (row) row.classList.remove('active-session');
        }
    }

    const activeLabel = document.getElementById('active-session-label');
    if (activeLabel) {
        const cap = currentSession ? currentSession.charAt(0).toUpperCase() + currentSession.slice(1) : '--';
        activeLabel.textContent = currentSession ? (names[currentSession] || cap) : 'Chiuso';
        activeLabel.style.color = currentSession ? 'var(--green)' : 'var(--muted)';
    }

    const utcEl = document.getElementById('utc-now');
    if (utcEl) {
        const now = new Date();
        utcEl.textContent = now.toISOString().slice(11, 19) + ' UTC';
    }
}

// ================================================================
// MODE CARDS
// ================================================================
function updateModeCards(d) {
    const container = document.getElementById('mode-cards');
    if (document.getElementById('view-mode-cards').style.display === 'none') return;
    const modes = ['daytrading', 'swing'];
    const emojis = { daytrading: '\U0001F4CA', swing: '\U0001F3D7\uFE0F' };
    const tfs = { daytrading: 'D1 \u2192 H4 \u2192 M15 \u2192 M5 \u2192 M1', swing: 'D1 \u2192 H4 \u2192 H1 \u2192 M15' };

    let html = '';
    for (const mode of modes) {
        const cfg = (d.modes || {})[mode] || {};
        const magic = cfg.magic || (mode === 'daytrading' ? 1002 : 1003);
        const slRange = cfg.sl_range || '--';
        const risk = cfg.risk_pct != null ? cfg.risk_pct + '%' : '--';
        const positions = cfg.positions || [];
        const lastScan = cfg.last_scan || '--';
        const signalsToday = cfg.signals_today || 0;

        let posHtml = '';
        if (positions.length === 0) {
            posHtml = '<div class="no-positions">Nessuna posizione attiva</div>';
        } else {
            for (const p of positions) {
                const pnlClass = (p.pnl_pips || 0) >= 0 ? 'pos' : 'neg';
                const sideClass = (p.direction || '').toUpperCase() === 'BUY' ? 'buy' : 'sell';
                const rrStr = p.rr != null ? fmtNum(p.rr, 1) : '--';
                const pendingTag = p.type === 'PENDING'
                    ? '<span class="pos-tag" style="background:rgba(249,115,22,0.15);color:#f97316">IN ATTESA</span>'
                    : '';
                posHtml += '<div class="pos-row">' +
                    '<span class="pos-tag ' + sideClass + '">' + (p.direction || '--') + '</span>' +
                    '<span>#' + (p.ticket || '--') + '</span>' +
                    pendingTag +                     '<span>E: ' + fmtPips(p.entry, p.symbol || d.symbol) + '</span>' +
                     '<span>SL: ' + fmtPips(p.sl, p.symbol || d.symbol) + '</span>' +
                     '<span>TP: ' + fmtPips(p.tp, p.symbol || d.symbol) + '</span>' +
                     '<span class="pnl ' + pnlClass + '">' + ((p.pnl_pips || 0) >= 0 ? '+' : '') + fmtPips(p.pnl_pips, p.symbol || d.symbol) + ' pip</span>' +
                    '<span style="color:var(--muted)">RR ' + rrStr + '</span>' +
                    '</div>';
            }
        }

        html += '<div class="card mode-card ' + mode + '">' +
            '<div class="card-header">' +
                '<span class="emoji">' + emojis[mode] + '</span>' +
                '<span style="text-transform:capitalize">' + mode + '</span>' +
                '<span class="mode-badge ' + mode + '">' + mode.toUpperCase() + '</span>' +
            '</div>' +
            '<div class="card-body">' +
                '<div class="mode-meta">' +
                    '<div>\U0001F522 Magic: <strong>' + magic + '</strong></div>' +
                    '<div>\u23F1\uFE0F TF: <strong>' + tfs[mode] + '</strong></div>' +
                    '<div>\U0001F6D1 SL Range: <strong>' + slRange + '</strong></div>' +
                    '<div>\U0001F4A3 Rischio: <strong>' + risk + '</strong></div>' +
                    '<div>\U0001F550 Ultimo scan: <strong>' + lastScan + '</strong></div>' +
                    '<div>\U0001F3AF Segnali oggi: <strong>' + signalsToday + '</strong></div>' +
                '</div>' +
                '<div style="margin-top:12px;font-size:0.78rem;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px;">Posizioni attive</div>' +
                posHtml +
            '</div>' +
        '</div>';
    }
    container.innerHTML = html;
}

function updatePipelineStats(d) {
    const el = document.getElementById('pipeline-stats');
    const ps = d.pipeline || {};
    el.innerHTML =
        '<div class="stat-row"><span>\U0001F50D Scan oggi</span><span class="stat-value" style="color:var(--blue)">' + (ps.scans_today || 0) + '</span></div>' +
        '<div class="stat-row"><span>\U0001F3AF Segnali generati</span><span class="stat-value" style="color:var(--yellow)">' + (ps.signals_today || 0) + '</span></div>' +
        '<div class="stat-row"><span>\U0001F4DD Ordini eseguiti</span><span class="stat-value" style="color:var(--green)">' + (ps.orders_today || 0) + '</span></div>' +
        '<div class="stat-row"><span>\u23F1\uFE0F Intervallo scan</span><span class="stat-value">' + (ps.scan_interval || '--') + 's</span></div>' +
        '<div class="stat-row"><span>\U0001F4CB Modalit\u00E0 attive</span><span class="stat-value">' + ((ps.active_modes || []).join(', ') || '--') + '</span></div>';
}

function updateMarketContext(d) {
    const el = document.getElementById('market-context');
    const mc = d.market || {};
    const newsColor = mc.near_news ? 'var(--red)' : 'var(--green)';
    const weekendColor = mc.is_weekend ? 'var(--red)' : 'var(--green)';
    const newsLabel = mc.near_news ? '\u26A0\uFE0F SI' : '\u2705 No';
    const weekendLabel = mc.is_weekend ? '\U0001F512 SI' : '\u2705 No';

    let dxyHtml = mc.dxy_bias || 'N/D';
    if (mc.dxy_bias === 'bullish') dxyHtml = '<span style="color:var(--green)">\U0001F4C8 Bullish</span>';
    else if (mc.dxy_bias === 'bearish') dxyHtml = '<span style="color:var(--red)">\U0001F4C9 Bearish</span>';

    el.innerHTML =
        '<div class="context-item"><span class="context-label">Sessione</span><span class="context-value">' + (mc.session || '--') + '</span></div>' +
        '<div class="context-item"><span class="context-label">DXY Bias</span><span class="context-value">' + dxyHtml + '</span></div>' +
        '<div class="context-item"><span class="context-label">News Imminenti</span><span class="context-value" style="color:' + newsColor + '">' + newsLabel + '</span></div>' +
        '<div class="context-item"><span class="context-label">Weekend</span><span class="context-value" style="color:' + weekendColor + '">' + weekendLabel + '</span></div>';
}

function escHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

let lastLogs = [];

function logLevel(line) {
    const m = line.match(/\|\s*([A-Z]+)\s*\|/);
    if (m) {
        const lv = m[1];
        if (lv === 'ERROR' || lv === 'CRITICAL') return 'ERROR';
        if (lv === 'WARNING') return 'WARN';
        return 'INFO';
    }
    if (/CRITICAL|ERROR/i.test(line)) return 'ERROR';
    if (/WARNING|WARN/i.test(line)) return 'WARN';
    return 'INFO';
}

function renderLogs() {
    const el = document.getElementById('logs');
    const filter = document.getElementById('log-filter').value;
    let list = lastLogs;
    if (filter !== 'all') {
        list = lastLogs.filter(l => {
            const lv = logLevel(l);
            if (filter === 'ERROR') return lv === 'ERROR';
            if (filter === 'WARN') return lv === 'WARN' || lv === 'ERROR';
            return true;
        });
    }
    const countEl = document.getElementById('log-count');
    if (countEl) countEl.textContent = '(mostrando ' + list.length + ' di ' + lastLogs.length + ')';

    if (list.length === 0) {
        el.innerHTML = '<div class="log-line info">Nessun log per questo filtro</div>';
        return;
    }
    let html = '';
    for (const line of list) {
        const lv = logLevel(line);
        let cls = 'info';
        if (lv === 'ERROR') cls = 'error';
        else if (lv === 'WARN') cls = 'warn';
        html += '<div class="log-line ' + cls + '">' + escHtml(line) + '</div>';
    }
    el.innerHTML = html;
    el.scrollTop = el.scrollHeight;
}

function updateLogs(d) {
    lastLogs = d.logs || [];
    renderLogs();
}

function updateClosedPositions(d) {
    const el = document.getElementById('closed-positions');
    if (!el) return;
    const list = d.closed_positions || [];
    const countEl = document.getElementById('closed-count');
    if (countEl) countEl.textContent = list.length;
    if (list.length === 0) {
        el.innerHTML = '<div class="no-positions">Nessuna posizione chiusa (ultime 24h)</div>';
        return;
    }
    let html = '';
    for (const p of list) {
        const profitClass = (p.profit || 0) >= 0 ? 'pos' : 'neg';
        const sideClass = (p.direction || '').toUpperCase() === 'BUY' ? 'buy' : 'sell';
        const sign = (p.profit || 0) >= 0 ? '+' : '';
        const isPartial = (p.close_type || '') === 'PARZIALE';
        const typeTag = isPartial
            ? '<span class="pos-tag" style="background:rgba(245,158,11,0.15);color:var(--yellow)">PARZIALE</span>'
            : '';
        html += '<div class="pos-row">' +
            '<span class="pos-tag ' + sideClass + '">' + (p.direction || '--') + '</span>' +
            typeTag +
            '<span>#' + (p.ticket || '--') + '</span>' +
            '<span>' + escHtml(p.symbol || '--') + '</span>' +
            '<span>E: ' + fmtPips(p.entry, p.symbol) + '</span>' +
            '<span>X: ' + fmtPips(p.exit, p.symbol) + '</span>' +
            '<span>Vol: ' + fmtNum(p.volume, 2) + '</span>' +
            '<span class="pnl ' + profitClass + '">' + sign + fmtNum(p.profit, 2) + '$</span>' +
            '<span style="color:var(--muted)">' + escHtml(p.time || '') + '</span>' +
            '</div>';
    }
    el.innerHTML = html;
}

function updateErrors(d) {
    const el = document.getElementById('error-list');
    if (!el) return;
    const countEl = document.getElementById('error-count');
    const errors = d.errors || [];
    if (countEl) countEl.textContent = d.errors_count || 0;
    if (errors.length === 0) {
        el.innerHTML = '<div class="no-positions">Nessun errore \U0001F389</div>';
        return;
    }
    let html = '';
    for (const line of errors) {
        html += '<div class="log-line error">' + escHtml(line) + '</div>';
    }
    el.innerHTML = html;
    el.scrollTop = el.scrollHeight;
}

function exportLogs() {
    window.location.href = '/api/logs/export';
}

async function clearLogs() {
    if (!window.confirm('Svuotare il file dei log?')) return;
    try {
        const resp = await fetch('/api/logs/clear', { method: 'POST' });
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        lastLogs = [];
        renderLogs();
        updateErrors({ errors: [], errors_count: 0 });
        refresh();
    } catch (e) {
        window.alert('Errore nello svuotare i log: ' + e.message);
    }
}

(function() {
    const sel = document.getElementById('log-filter');
    if (sel) sel.addEventListener('change', renderLogs);
})();

// ================================================================
// STORICO POSIZIONI (daily/weekly/monthly/custom date range)
// ================================================================
const HIST_API = '/api/history';
let histPeriod = 'daily';

function switchHistory(period, el) {
    histPeriod = period;
    document.querySelectorAll('#hist-tabs .chart-tab').forEach(function(t) { t.classList.remove('active'); });
    el.classList.add('active');

    const dateRange = document.getElementById('hist-date-range');
    if (period === 'custom') {
        dateRange.style.display = 'flex';
    } else {
        dateRange.style.display = 'none';
        fetchHistory();
    }
}

function fetchHistory() {
    let url = HIST_API;
    if (histPeriod === 'custom') {
        const fromEl = document.getElementById('hist-from');
        const toEl = document.getElementById('hist-to');
        if (!fromEl.value || !toEl.value) {
            document.getElementById('hist-positions').innerHTML = '<div class="no-positions">Seleziona entrambe le date</div>';
            return;
        }
        url += '?from=' + encodeURIComponent(fromEl.value) + '&to=' + encodeURIComponent(toEl.value);
    } else {
        url += '?period=' + histPeriod;
    }

    fetch(url)
        .then(function(r) { return r.json(); })
        .then(function(data) { renderHistory(data); })
        .catch(function(e) {
            console.warn('History fetch error:', e.message);
        });
}

function renderHistory(data) {
    let list = data.positions || [];
    const s = data.summary || {};

    // Ordina per data decrescente (piu' recente in cima)
    list = list.slice().sort(function(a, b) {
        return (b.timestamp || 0) - (a.timestamp || 0);
    });

    // Summary
    const pnlEl = document.getElementById('hist-pnl');
    if (pnlEl) {
        const net = s.total_pnl || 0;
        pnlEl.textContent = (net >= 0 ? '+' : '') + '$' + fmtNum(net, 2);
        pnlEl.style.color = net >= 0 ? 'var(--green)' : 'var(--red)';
    }
    const wrEl = document.getElementById('hist-winrate');
    if (wrEl) wrEl.textContent = (s.win_rate != null ? s.win_rate : 0) + '%';
    document.getElementById('hist-wins').textContent = s.wins || 0;
    document.getElementById('hist-losses').textContent = s.losses || 0;
    document.getElementById('hist-count').textContent = s.count || 0;

    // Positions
    const el = document.getElementById('hist-positions');
    if (list.length === 0) {
        el.innerHTML = '<div class="no-positions">Nessuna posizione chiusa nel periodo</div>';
        return;
    }

    // Header con totale P&L in cima alla tabella
    let html = '<div style="display:flex;align-items:center;justify-content:space-between;padding:8px 10px;margin-bottom:8px;border-radius:6px;background:rgba(255,255,255,0.04);border:1px solid var(--border);font-size:0.78rem;font-weight:700;">' +
        '<span style="color:var(--muted);">TOTALE CHIUSO</span>' +
        `<span style="font-family:'JetBrains Mono','Consolas',monospace;color:${(s.total_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)'};">` +
        ((s.total_pnl || 0) >= 0 ? '+' : '') + '$' + fmtNum(s.total_pnl || 0, 2) + '</span>' +
        '</div>';

    for (const p of list) {
        const profitClass = (p.profit || 0) >= 0 ? 'pos' : 'neg';
        const sideClass = (p.direction || '').toUpperCase() === 'BUY' ? 'buy' : 'sell';
        const sign = (p.profit || 0) >= 0 ? '+' : '';
        const isPartial = (p.close_type || '') === 'PARZIALE';
        const typeTag = isPartial
            ? '<span class="pos-tag" style="background:rgba(245,158,11,0.15);color:var(--yellow)">PARZIALE</span>'
            : '';
        // Sfondo riga: verde per profit, rosso per loss
        const rowBg = (p.profit || 0) >= 0
            ? 'background:rgba(16,185,129,0.04);border-left:3px solid var(--green);'
            : 'background:rgba(239,68,68,0.04);border-left:3px solid var(--red);';
        html += '<div class="pos-row" style="' + rowBg + '">' +
            '<span class="pos-tag ' + sideClass + '">' + (p.direction || '--') + '</span>' +
            typeTag +
            '<span>#' + (p.ticket || '--') + '</span>' +
            '<span>' + escHtml(p.symbol || '--') + '</span>' +
            '<span>E: ' + fmtPips(p.entry, p.symbol) + '</span>' +
            '<span>X: ' + fmtPips(p.exit, p.symbol) + '</span>' +
            '<span>Vol: ' + fmtNum(p.volume, 2) + '</span>' +
            '<span class="pnl ' + profitClass + '">' + sign + fmtNum(p.profit, 2) + '$</span>' +
            '<span style="color:var(--muted)">' + escHtml(p.time || '') + '</span>' +
            '</div>';
    }
    el.innerHTML = html;
}

// Init date pickers with defaults (oggi 00:00 -> adesso)
(function() {
    const now = new Date();
    const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0, 0);
    const fromEl = document.getElementById('hist-from');
    const toEl = document.getElementById('hist-to');
    if (fromEl) fromEl.value = todayStart.toISOString().slice(0, 16);
    if (toEl) toEl.value = now.toISOString().slice(0, 16);
    // Carica storico giornaliero all'avvio
    fetchHistory();
})();

// ================================================================
// PERFORMANCE PIE CHART
// ================================================================
function switchPerf(period, el) {
    perfPeriod = period;
    document.querySelectorAll('.chart-tab').forEach(function(tab) { tab.classList.remove('active'); });
    el.classList.add('active');
    if (lastPerfData) renderPerfChart(lastPerfData);
}

function renderPerfChart(data) {
    if (typeof Chart === 'undefined') return;
    var d = (data && data[perfPeriod]) ? data[perfPeriod] : null;
    var ctx = document.getElementById('perf-chart').getContext('2d');

    var profit = (d && d.profit) ? d.profit : 0;
    var loss = (d && d.loss) ? Math.abs(d.loss) : 0;
    var hasData = (profit > 0 || loss > 0);

    if (perfChart) perfChart.destroy();

    var pieData = hasData ? [Math.abs(profit), loss] : [1, 0];
    var pieColors = hasData
        ? [profit >= 0 ? '#10b981' : '#ef4444', loss > 0 ? '#ef4444' : '#1f2937']
        : ['#374151', '#1f2937'];

    perfChart = new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels: ['Profitto', 'Perdita'],
            datasets: [{
                data: pieData,
                backgroundColor: pieColors,
                borderColor: 'rgba(17,24,39,0.8)',
                borderWidth: 2,
                hoverBorderColor: '#10b981',
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: true,
            cutout: '65%',
            plugins: {
                legend: {
                    display: true,
                    position: 'bottom',
                    labels: {
                        color: '#9ca3af',
                        padding: 16,
                        font: { size: 12 },
                        usePointStyle: true,
                    }
                },
                tooltip: {
                    callbacks: {
                        label: function(ctx) {
                            return ' $' + fmtNum(ctx.raw, 2);
                        }
                    }
                }
            },
        }
    });

    var totalEl = document.getElementById('perf-total');
    var net = (d && d.net != null) ? d.net : 0;
    if (totalEl) {
        totalEl.textContent = (net >= 0 ? '+' : '') + '$' + fmtNum(net, 2);
        totalEl.style.color = net >= 0 ? 'var(--green)' : 'var(--red)';
    }

    var tabs = document.querySelectorAll('.chart-tab');
    var periods = ['daily', 'weekly', 'monthly'];
    var labels = ['Oggi', 'Settimana', 'Mese'];
    tabs.forEach(function(tab, i) {
        var pd = (data && data[periods[i]]) ? data[periods[i]] : null;
        var netVal = (pd && pd.net != null) ? pd.net : 0;
        var sign = netVal >= 0 ? '+' : '';
        tab.textContent = labels[i] + ' ' + sign + '$' + fmtNum(Math.abs(netVal), 0);
    });
}

async function fetchPerformance() {
    try {
        const resp = await fetch(PERF_API);
        if (!resp.ok) return;
        const data = await resp.json();
        lastPerfData = data;
        renderPerfChart(data);
    } catch (e) {
        console.warn('Performance fetch error:', e.message);
    }
}

// ================================================================
// MAIN REFRESH
// ================================================================
let fetchErrors = 0;

async function refresh() {
    try {
        const resp = await fetch(API);
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const d = await resp.json();
        lastStatusData = d;
        fetchErrors = 0;
        updateHeader(d);
        updateModeCards(d);
        updateAllPositions(d);
        updatePipelineStats(d);
        updateMarketContext(d);
        updateClosedPositions(d);
        updateLogs(d);
        updateErrors(d);
    } catch (e) {
        fetchErrors++;
        console.warn('Dashboard poll error (' + fetchErrors + '):', e.message);
        var tsEl = document.getElementById('refresh-ts');
        if (tsEl) tsEl.textContent = '\u26A0\uFE0F errore fetch (x' + fetchErrors + '), riprovo...';
        if (fetchErrors >= 3) {
            var pill = document.getElementById('status-pill');
            if (pill && pill.className.indexOf('online') === -1) {
                pill.className = 'status-pill offline';
                pill.innerHTML = '<span class="status-dot red"></span> Server non raggiungibile';
            }
        }
    }
}

(function() {
    var pill = document.getElementById('status-pill');
    if (pill) {
        pill.innerHTML = '<span class="status-dot yellow"></span> Caricamento dati...';
    }
    var tsEl = document.getElementById('refresh-ts');
    if (tsEl) tsEl.textContent = 'connessione in corso...';
})();

refresh();
fetchPerformance();
pollTimer = setInterval(refresh, 2000);
const chartTimer = setInterval(fetchPerformance, 15000);

window.addEventListener('beforeunload', function() {
    if (pollTimer) clearInterval(pollTimer);
    if (chartTimer) clearInterval(chartTimer);
});
</script>
</body>
</html>
"""


# ==========================================================================
# POSIZIONI CHIUSE (funzione condivisa)
# ==========================================================================

def _query_closed_positions(from_dt: datetime, to_dt: datetime, mt5_mod) -> list[dict]:
    """Interroga MT5 per le posizioni chiuse in un intervallo di date.

    Usata sia da _collect_api_status() che da /api/history.
    """
    deals = mt5_mod.history_deals_get(from_date=from_dt, to_date=to_dt)
    if not deals:
        return []

    opens: dict[int, dict] = {}
    extra_pnl: dict[int, float] = {}
    closes: list = []
    for d in deals:
        pid = int(getattr(d, "position_id", 0) or 0)
        if pid <= 0:
            continue
        if d.entry == mt5_mod.DEAL_ENTRY_IN:
            opens[pid] = {
                "symbol": str(getattr(d, "symbol", "") or ""),
                "type": int(d.type),
                "entry": float(d.price),
                "volume": float(d.volume),
            }
        elif d.entry == mt5_mod.DEAL_ENTRY_OUT:
            closes.append(d)
        if d.entry != mt5_mod.DEAL_ENTRY_OUT:
            extra_pnl[pid] = (
                extra_pnl.get(pid, 0.0)
                + float(getattr(d, "swap", 0) or 0)
                + float(getattr(d, "commission", 0) or 0)
            )

    closes.sort(key=lambda d: int(d.time), reverse=True)
    cutoff = int(from_dt.timestamp())
    results: list[dict] = []

    for d in closes:
        pid = int(getattr(d, "position_id", 0) or 0)
        op = opens.get(pid)
        if op is None:
            try:
                pos_deals = mt5_mod.history_deals_get(position=pid)
                if pos_deals:
                    for pd in pos_deals:
                        if pd.entry == mt5_mod.DEAL_ENTRY_IN:
                            opens[pid] = {
                                "symbol": str(getattr(pd, "symbol", "") or ""),
                                "type": int(pd.type),
                                "entry": float(pd.price),
                                "volume": float(pd.volume),
                            }
                            break
                    for pd in pos_deals:
                        if pd.entry != mt5_mod.DEAL_ENTRY_OUT and int(pd.time) < cutoff:
                            extra_pnl[pid] = (
                                extra_pnl.get(pid, 0.0)
                                + float(getattr(pd, "swap", 0) or 0)
                                + float(getattr(pd, "commission", 0) or 0)
                            )
            except Exception:
                pass
            op = opens.get(pid)
        if op is None:
            continue

        direction = "BUY" if op["type"] == mt5_mod.DEAL_TYPE_BUY else "SELL"
        profit = (float(d.profit or 0)
                  + float(getattr(d, "swap", 0) or 0)
                  + float(getattr(d, "commission", 0) or 0)
                  + extra_pnl.get(pid, 0.0))
        close_type = ("PARZIALE"
                      if float(d.volume or 0) < op["volume"] - 0.0001
                      else "TOTALE")
        results.append({
            "ticket": pid,
            "symbol": op["symbol"],
            "direction": direction,
            "volume": round(float(d.volume or 0), 2),
            "entry": round(op["entry"], 5),
            "exit": round(float(d.price), 5),
            "profit": round(profit, 2),
            "close_type": close_type,
            "time": datetime.fromtimestamp(int(d.time)).strftime("%d/%m %H:%M"),
            "timestamp": int(d.time),
        })
    return results


# ==========================================================================
# DASHBOARD DATA COLLECTOR
# ==========================================================================

def _collect_api_status(engine: MT5Engine, master_bot=None) -> dict:
    """Raccoglie tutti i dati per /api/status.

    master_bot: riferimento opzionale all'istanza MasterBot per pipeline stats.
    """
    import config
    from mt5_adapter import mt5

    now = time.time()
    result: dict = {
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "bot_running": True,
        "uptime_seconds": round(now - _BOT_START_TIME, 0),
        "symbol": config.SYMBOLS[0] if config.SYMBOLS else "XAUUSD",
    }

    # --- MT5 status ---
    try:
        if master_bot is not None and hasattr(master_bot, "is_mt5_ready"):
            mt5_connected = bool(master_bot.is_mt5_ready)
        else:
            mt5_connected = engine.is_initialized if engine else False
        result["mt5_connected"] = mt5_connected

        if mt5_connected:
            info = mt5.account_info()
            if info:
                result["balance"] = round(float(info.balance), 2)
                result["equity"] = round(float(info.equity), 2)
                result["pnl"] = round(float(info.profit), 2)
            else:
                result["balance"] = result["equity"] = result["pnl"] = None
        else:
            result["balance"] = result["equity"] = result["pnl"] = None
    except Exception:
        result["mt5_connected"] = False
        result["balance"] = result["equity"] = result["pnl"] = None

    # --- Mode config & positions ---
    modes: dict[str, dict] = {}
    for mode in ("daytrading", "swing"):
        magic = config.get_mode_magic(mode)
        tfs = config.get_mode_timeframes(mode)
        sl_min = config.get_sl_min_pips(result["symbol"], mode)
        # La dashboard mostra la fascia nominale; l'estensione swing del 25%
        # è una tolleranza operativa usata solo per validare l'ordine.
        sl_max = config.get_sl_nominal_max_pips(result["symbol"], mode)

        mode_data: dict = {
            "magic": magic,
            "timeframes": {"htf": tfs[0], "mtf": tfs[1], "ltf": tfs[2]},
            "sl_range": f"{sl_min}-{sl_max} pip",
            "risk_pct": config.RISK_PERCENT,
            "positions": [],
            "last_scan": "--",
            "signals_today": 0,
        }

        # Positions for this mode (market + pending)
        try:
            if result.get("mt5_connected"):

                # 1) Posizioni MARKET aperte su TUTTI i simboli configurati
                all_positions: list = []
                for sym in config.SYMBOLS:
                    sym_positions = mt5.positions_get(symbol=sym)
                    if sym_positions:
                        all_positions.extend(sym_positions)
                if all_positions:
                    for pos in all_positions:
                        pos_magic = int(pos.magic)
                        # Includi solo magic della modalita' corrente.
                        # MAGIC_MAIN (1000) è legacy e viene mostrato solo
                        # nella vista tecnica della modalità daytrading.
                        include = (pos_magic == magic) or (mode == "daytrading" and pos_magic == MAGIC_MAIN)
                        if not include:
                            continue
                        pos_symbol = str(getattr(pos, "symbol", "") or "")
                        entry = float(pos.price_open)
                        current_sl = float(pos.sl)
                        current_tp = float(pos.tp)
                        direction = "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"
                        pip = utils.pip_size(pos_symbol)

                        try:
                            tick = mt5.symbol_info_tick(pos_symbol or result["symbol"])
                            price = float(tick.bid) if direction == "BUY" else float(tick.ask)
                        except Exception:
                            price = entry

                        pnl_pips = (price - entry) / pip if direction == "BUY" else (entry - price) / pip
                        risk_pips = abs(entry - current_sl) / pip if current_sl > 0 else 0
                        reward_pips = abs(current_tp - entry) / pip if current_tp > 0 else 0
                        rr = reward_pips / risk_pips if risk_pips > 0 else 0

                        mode_data["positions"].append({
                            "ticket": int(pos.ticket),
                            "symbol": pos_symbol,
                            "direction": direction,
                            "entry": round(entry, 5),
                            "sl": round(current_sl, 5),
                            "tp": round(current_tp, 5),
                            "pnl_pips": round(pnl_pips, 1),
                            "rr": round(rr, 1),
                            "type": "MARKET",
                        })

                # 2) Ordini PENDING su TUTTI i simboli configurati
                all_pending: list = []
                for sym in config.SYMBOLS:
                    sym_pending = mt5.orders_get(symbol=sym)
                    if sym_pending:
                        all_pending.extend(sym_pending)
                if all_pending:
                    for order in all_pending:
                        order_magic = int(order.magic)
                        include = (order_magic == magic) or (mode == "daytrading" and order_magic == MAGIC_MAIN)
                        if not include:
                            continue
                        order_symbol = str(getattr(order, "symbol", "") or "")
                        order_entry = float(order.price_open)
                        current_sl = float(order.sl)
                        current_tp = float(order.tp)
                        order_type = order.type
                        pip = utils.pip_size(order_symbol)
                        if order_type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP, getattr(mt5, 'ORDER_TYPE_BUY_STOP_LIMIT', None)):
                            direction = "BUY"
                        else:
                            direction = "SELL"

                        try:
                            tick = mt5.symbol_info_tick(order_symbol or result["symbol"])
                            price = float(tick.bid) if direction == "BUY" else float(tick.ask)
                        except Exception:
                            price = order_entry

                        pnl_pips = 0.0  # pending = no P&L yet
                        risk_pips = abs(order_entry - current_sl) / pip if current_sl > 0 else 0
                        reward_pips = abs(current_tp - order_entry) / pip if current_tp > 0 else 0
                        rr = reward_pips / risk_pips if risk_pips > 0 else 0

                        mode_data["positions"].append({
                            "ticket": int(order.ticket),
                            "symbol": order_symbol,
                            "direction": direction,
                            "entry": round(order_entry, 5),
                            "sl": round(current_sl, 5),
                            "tp": round(current_tp, 5),
                            "pnl_pips": round(pnl_pips, 1),
                            "rr": round(rr, 1),
                            "type": "PENDING",
                        })
        except Exception:
            pass

        # Scan time from MasterBot if available
        if master_bot and hasattr(master_bot, '_last_smc_scan'):
            key = f"{result['symbol']}:{mode}"
            last_ts = master_bot._last_smc_scan.get(key)
            if last_ts:
                ago = int(now - last_ts)
                mode_data["last_scan"] = f"{ago}s fa" if ago < 120 else f"{ago // 60}m fa"

        if master_bot and hasattr(master_bot, '_signals_today'):
            mode_data["signals_today"] = master_bot._signals_today

        modes[mode] = mode_data

    result["modes"] = modes

    # --- Posizioni chiuse (ultime 24h) ---
    closed_positions: list[dict] = []
    try:
        if result.get("mt5_connected"):
            from_dt, to_dt = utils.mt5_history_window(hours_back=24, minutes_ahead=1)
            deals = mt5.history_deals_get(from_date=from_dt, to_date=to_dt)
            if deals:
                opens: dict[int, dict] = {}
                extra_pnl: dict[int, float] = {}
                closes: list = []
                for d in deals:
                    pid = int(getattr(d, "position_id", 0) or 0)
                    if pid <= 0:
                        continue
                    if d.entry == mt5.DEAL_ENTRY_IN:
                        opens[pid] = {
                            "symbol": str(getattr(d, "symbol", "") or ""),
                            "type": int(d.type),
                            "entry": float(d.price),
                            "volume": float(d.volume),
                        }
                    elif d.entry == mt5.DEAL_ENTRY_OUT:
                        closes.append(d)
                    if d.entry != mt5.DEAL_ENTRY_OUT:
                        extra_pnl[pid] = (
                            extra_pnl.get(pid, 0.0)
                            + float(getattr(d, "swap", 0) or 0)
                            + float(getattr(d, "commission", 0) or 0)
                        )

                closes.sort(key=lambda d: int(d.time), reverse=True)
                closes = closes[:30]
                cutoff = int(from_dt.timestamp())

                for d in closes:
                    pid = int(getattr(d, "position_id", 0) or 0)
                    op = opens.get(pid)
                    if op is None:
                        try:
                            pos_deals = mt5.history_deals_get(position=pid)
                            if pos_deals:
                                for pd in pos_deals:
                                    if pd.entry == mt5.DEAL_ENTRY_IN:
                                        opens[pid] = {
                                            "symbol": str(getattr(pd, "symbol", "") or ""),
                                            "type": int(pd.type),
                                            "entry": float(pd.price),
                                            "volume": float(pd.volume),
                                        }
                                        break
                                for pd in pos_deals:
                                    if (pd.entry != mt5.DEAL_ENTRY_OUT
                                            and int(pd.time) < cutoff):
                                        extra_pnl[pid] = (
                                            extra_pnl.get(pid, 0.0)
                                            + float(getattr(pd, "swap", 0) or 0)
                                            + float(getattr(pd, "commission", 0) or 0)
                                        )
                        except Exception:
                            pass
                        op = opens.get(pid)
                    if op is None:
                        continue
                    direction = "BUY" if op["type"] == mt5.DEAL_TYPE_BUY else "SELL"
                    profit = (float(d.profit or 0)
                              + float(getattr(d, "swap", 0) or 0)
                              + float(getattr(d, "commission", 0) or 0)
                              + extra_pnl.get(pid, 0.0))
                    close_type = ("PARZIALE"
                                  if float(d.volume or 0) < op["volume"] - 0.0001
                                  else "TOTALE")
                    closed_positions.append({
                        "ticket": pid,
                        "symbol": op["symbol"],
                        "direction": direction,
                        "volume": round(float(d.volume or 0), 2),
                        "entry": round(op["entry"], 5),
                        "exit": round(float(d.price), 5),
                        "profit": round(profit, 2),
                        "close_type": close_type,
                        "time": datetime.fromtimestamp(int(d.time)).strftime("%d/%m %H:%M"),
                    })
                closed_positions = closed_positions[:30]
    except Exception as e:
        logger.warning("[API] Errore nel recupero posizioni chiuse da MT5: %s", e)
        closed_positions = []
    result["closed_positions"] = closed_positions

    # --- Fallback: se MT5 non ha storico, usa le chiusure tracciate in-memory ---
    if not closed_positions and master_bot and hasattr(master_bot, '_closed_positions_history'):
        tracked = getattr(master_bot, '_closed_positions_history', [])
        if tracked:
            # Filtra solo le ultime 24h
            cutoff = time.time() - 86400
            recent = [p for p in tracked if p.get("timestamp", 0) >= cutoff]
            result["closed_positions"] = recent[:30]

    # --- Pipeline stats ---
    pipeline: dict = {
        "scans_today": 0,
        "signals_today": 0,
        "orders_today": 0,
        "scan_interval": config.SMC_SCAN_INTERVAL_SECONDS,
        "active_modes": list(config.ENABLED_MODES),
    }
    if master_bot:
        pipeline["scans_today"] = getattr(master_bot, '_scans_today', 0)
        pipeline["signals_today"] = getattr(master_bot, '_signals_today', 0)
        pipeline["orders_today"] = getattr(master_bot, '_orders_today', 0)
    result["pipeline"] = pipeline

    # --- Market context ---
    result["market"] = {
        "session": _get_session_safe(),
        "dxy_bias": _get_dxy_bias_safe(),
        "near_news": _get_near_news_safe(),
        "is_weekend": False,
    }

    # --- Logs ---
    # IMPORTANTE: il file di log può crescere oltre i 15 MB. Leggere l'intero
    # file con readlines() a ogni poll (dashboard ogni 3s) bloccava /api/status
    # per secondi e mandava in timeout la landing. Ora leggiamo solo le ultime
    # ~3000 righe dal fondo del file (tail efficiente).
    try:
        log_path = os.path.join(os.path.dirname(__file__), "bot_smc.log")
        if os.path.exists(log_path):
            lines = _tail_log_lines(log_path, max_lines=3000)
            result["logs"] = lines[-100:]
            err_lines = [l for l in lines if utils.is_error_log_line(l)]
            result["errors"] = err_lines[-50:]
            result["errors_count"] = len(result["errors"])
        else:
            result["logs"] = ["[INFO] File bot_smc.log non trovato"]
            result["errors"] = []
            result["errors_count"] = 0
    except Exception:
        result["logs"] = ["[ERROR] Impossibile leggere il file di log"]
        result["errors"] = []
        result["errors_count"] = 0

    return result


def _tail_log_lines(log_path: str, max_lines: int = 3000) -> list[str]:
    """Legge le ultime ``max_lines`` righe di un file in modo efficiente.

    Legge solo un blocco finale del file (al massimo ~256 KB) invece di
    l'intero contenuto: indispensabile quando il log supera i 10 MB.
    Gestisce anche il caso di riga finale senza newline.
    """
    block_size = 8192
    max_bytes = block_size * 32  # 256 KB in lettura al massimo
    try:
        size = os.path.getsize(log_path)
        if size == 0:
            return []
        read_start = max(0, size - max_bytes)
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(read_start)
            data = f.read()
        lines = data.splitlines()
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
        return lines
    except OSError:
        return []


# ==========================================================================
# CREATE APP (Factory)
# ==========================================================================

def create_app(
    engine,
    notifier,
    webhook_queue=None,
    master_bot=None,
    symbol_info_provider=None,
    prop_mode=None,
    secret_token=None,
):
    """Factory dell'app Flask (usata da run_master.py).

    Args:
        engine: istanza MT5Engine (connessione gia' attiva)
        notifier: callable per inviare notifiche Telegram
        webhook_queue: queue.Queue dove accodare gli ordini da eseguire nel
            main thread. Se None, gli ordini vengono eseguiti nel thread del
            webhook (modalita' di compatibilita', non thread-safe con MT5).
        master_bot: riferimento opzionale all'istanza MasterBot per le stats.
        symbol_info_provider: provider opzionale per i dati del simbolo; se
            omesso usa il provider MT5 reale (utile per test senza terminale).
        prop_mode: override opzionale di config.PROP_MODE, utile nei test.
        secret_token: override opzionale del token webhook, utile nei test.

    Returns:
        App Flask configurata.
    """
    app = Flask(__name__)
    import config

    # Inietta dipendenze nell'app context
    app.config["ENGINE"] = engine
    app.config["NOTIFIER"] = notifier
    app.config["WEBHOOK_QUEUE"] = webhook_queue
    app.config["MASTER_BOT"] = master_bot
    app.config["SYMBOL_INFO_PROVIDER"] = (
        symbol_info_provider
        if symbol_info_provider is not None
        else mt5_symbol_info_provider
    )
    app.config["PROP_MODE"] = config.PROP_MODE if prop_mode is None else bool(prop_mode)
    app.config["WEBHOOK_SECRET_TOKEN"] = secret_token

    # ======================================================================
    # LANDING + DASHBOARD
    # ======================================================================

    def _serve_file_or_fallback(filename: str, fallback_html: str):
        """Serve un file HTML da disco (modificabile senza toccare il Python);
        se manca o non è leggibile, ripiega sulla copia incorporata.

        Gli header ``Cache-Control`` disattivano la cache del browser: le
        modifiche ai file HTML si vedono subito senza hard-refresh.
        """
        no_cache = {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        }
        try:
            page_path = os.path.join(os.path.dirname(__file__), filename)
            if os.path.exists(page_path):
                with open(page_path, "r", encoding="utf-8") as f:
                    return f.read(), 200, no_cache
        except OSError:
            logger.warning("[WEB] Lettura %s fallita, uso copia incorporata.", filename)
        return render_template_string(fallback_html)

    @app.route("/", methods=["GET"])
    def index():
        """Landing page di presentazione del bot.

        Serve ``landing.html`` da disco; se il file manca (es. repo minimale)
        reindirizza alla dashboard come fallback.
        """
        no_cache = {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        }
        landing_path = os.path.join(os.path.dirname(__file__), "landing.html")
        if os.path.exists(landing_path):
            try:
                with open(landing_path, "r", encoding="utf-8") as f:
                    return f.read(), 200, no_cache
            except OSError:
                logger.warning("[WEB] Lettura landing.html fallita, redirect a /dashboard.")
        return redirect("/dashboard", code=302)

    @app.route("/dashboard", methods=["GET"])
    def dashboard():
        """Dashboard web interattiva del bot SMC.

        Se esiste ``dashboard.html`` accanto al modulo lo serve da disco
        (più semplice da modificare senza toccare il Python); altrimenti
        ripiega sulla copia incorporata ``DASHBOARD_HTML``.
        """
        return _serve_file_or_fallback("dashboard.html", DASHBOARD_HTML)

    @app.route("/api/status", methods=["GET"])
    def api_status():
        """API JSON con tutti i dati di stato del bot in tempo reale."""
        try:
            eng = app.config.get("ENGINE")
            mb = app.config.get("MASTER_BOT")
            data = _collect_api_status(eng, master_bot=mb)
            return jsonify(data)
        except Exception as e:
            logger.error("[API] /api/status error: %s", e, exc_info=True)
            return jsonify({
                "error": str(e),
                "bot_running": True,
                "mt5_connected": False,
                "timestamp": datetime.now().strftime("%H:%M:%S"),
            }), 500

    @app.route("/api/prices", methods=["GET"])
    def api_prices():
        """API JSON con i prezzi live dei simboli configurati.

        Per ogni simbolo in ``config.SYMBOLS`` legge il tick corrente da MT5
        (bid/ask) e calcola lo spread in pip e la variazione % giornaliera
        rispetto alla chiusura della candela D1 precedente. Se MT5 non è
        connesso o un simbolo non è quotato, quel simbolo viene omesso
        (fail-open: il ticker non blocca la pagina).
        """
        from mt5_adapter import mt5
        import config as _config

        eng = app.config.get("ENGINE")
        prices: list[dict] = []
        timestamp = datetime.now().strftime("%H:%M:%S")

        # Cache della variazione % giornaliera: la chiusura D1 cambia al massimo
        # una volta al giorno, quindi history_rates_get viene chiamato al più
        # ogni 60s (il tick live bid/ask resta aggiornato a ogni poll).
        now_ts = time.time()
        if not hasattr(app, "_prices_change_cache"):
            app._prices_change_cache = {}  # symbol -> (ts, change_pct)
        change_cache = app._prices_change_cache

        if eng is not None and getattr(eng, "is_initialized", False):
            for symbol in _config.SYMBOLS:
                try:
                    tick = mt5.symbol_info_tick(symbol)
                    if tick is None:
                        continue
                    bid = float(tick.bid)
                    ask = float(tick.ask)
                    if bid <= 0 or ask <= 0:
                        continue
                    pip = utils.pip_size(symbol)
                    spread_pips = round((ask - bid) / pip, 2) if pip else 0.0

                    # Variazione % giornaliera rispetto alla chiusura D1 precedente
                    change_pct = 0.0
                    cached = change_cache.get(symbol)
                    if cached is not None and (now_ts - cached[0]) < 60:
                        change_pct = cached[1]
                    else:
                        try:
                            from datetime import timedelta as _td
                            from_dt = utils.utc_now() - _td(days=3)
                            rates = mt5.history_rates_get(
                                symbol, mt5.TIMEFRAME_D1, from_dt,
                                utils.utc_now() + _td(minutes=1),
                            )
                            if rates is not None and len(rates) >= 2:
                                prev_close = float(rates[-2].close)
                                if prev_close > 0:
                                    change_pct = round(
                                        (float(rates[-1].close) - prev_close) / prev_close * 100, 2
                                    )
                        except Exception:
                            change_pct = 0.0  # variazione non calcolabile: fail-open
                        change_cache[symbol] = (now_ts, change_pct)

                    prices.append({
                        "symbol": symbol,
                        "bid": round(bid, 5),
                        "ask": round(ask, 5),
                        "spread_pips": spread_pips,
                        "change_pct": change_pct,
                    })
                except Exception:
                    continue  # simbolo non quotato: omesso

        return jsonify({
            "timestamp": timestamp,
            "mt5_connected": bool(eng is not None and getattr(eng, "is_initialized", False)),
            "prices": prices,
        })

    # ======================================================================
    # WEBHOOK (TradingView)
    # ======================================================================

    @app.route("/webhook", methods=["POST"])
    def trading_webhook():
        """Riceve segnali da TradingView e li inoltra a MT5."""
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "payload JSON mancante o non valido"}), 400

        # --- VALIDAZIONE TOKEN DI SICUREZZA ---
        expected_token = app.config["WEBHOOK_SECRET_TOKEN"]
        if expected_token is None:
            expected_token = config.WEBHOOK_SECRET_TOKEN
        if expected_token:
            received_token = data.get("token", "")
            if received_token != expected_token:
                logger.warning("Webhook ricevuto con token NON valido: '%s...'", str(received_token)[:10])
                return jsonify({"error": "token non valido", "status": "unauthorized"}), 401

        # Durante una perdita MT5 il segnale non deve restare in coda: entry,
        # spread e struttura potrebbero essere cambiati al reconnect. Il bot
        # risponde 503 e TradingView potrà ritentare secondo la sua policy.
        master_bot = app.config.get("MASTER_BOT")
        eng = app.config.get("ENGINE")
        if master_bot is not None and not getattr(master_bot, "is_mt5_ready", False):
            return jsonify({
                "error": "MT5 non pronto: ingresso sospeso durante il reconnect",
                "status": "mt5_disconnected",
            }), 503
        if (
            master_bot is None
            and eng is not None
            and hasattr(eng, "is_initialized")
            and not getattr(eng, "is_initialized", False)
        ):
            return jsonify({
                "error": "MT5 non inizializzato: ingresso sospeso",
                "status": "mt5_disconnected",
            }), 503

        # La modalità è obbligatoria e non viene mai inferita dal timeframe.
        # Un alert senza modalità esplicita non può essere classificato in modo
        # affidabile tra daytrading e swing e viene rifiutato.
        raw_mode = _get_field(data, "mode", "strategy", default=None)
        if raw_mode is None or not str(raw_mode).strip():
            return jsonify({
                "error": "campo 'mode' obbligatorio: usare 'daytrading' oppure 'swing'",
                "status": "rejected",
            }), 400

        payload = {
            "symbol": _get_field(data, "symbol", "simbolo", default=""),
            "side": _get_field(data, "side", "azione", default=""),
            "entry": _get_field(data, "entry", "prezzo", default=0.0),
            "sl": _get_field(data, "sl", default=0.0),
            "tp1": _get_field(data, "tp1", "tp", default=None),
            "tp2": _get_field(data, "tp2", default=None),
            "tp3": _get_field(data, "tp3", default=None),
            "mode": raw_mode,
            "setup_type": _get_field(data, "setup_type", default="pro_trend"),
            "balance": _get_field(data, "balance", default=None),
        }

        # Normalizza side
        raw_side = str(payload["side"]).lower()
        if raw_side in ("compra", "buy", "long"):
            payload["side"] = "buy"
        elif raw_side in ("vendi", "sell", "short"):
            payload["side"] = "sell"

        # Usa balance del conto MT5 se non fornito
        if not payload["balance"]:
            eng: MT5Engine = app.config["ENGINE"]
            bal = eng.account_balance()
            payload["balance"] = bal if bal else 10000.0

        # --- BLOCCO DXY (punto 5): nessun trade contro il trend del Dollaro.
        # Stessa regola del percorso SMC autonomo: EUR/GBP/XAU inverse,
        # USDJPY diretta, GBPJPY cross neutro. Se il DXY non è disponibile
        # il segnale non viene bloccato (fail-open).
        if _get_dxy_bias_raw is not None and _detect_dxy_conflict_raw is not None:
            try:
                dxy_data = _get_dxy_bias_raw()
                if dxy_data and dxy_data.get("trend"):
                    dxy_conflict, dxy_reason = _detect_dxy_conflict_raw(
                        payload["symbol"], payload["side"], dxy_data["trend"],
                    )
                    if dxy_conflict:
                        logger.info(
                            "Webhook %s %s rifiutato: %s",
                            payload["symbol"], payload["side"], dxy_reason,
                        )
                        return jsonify({
                            "error": f"conflitto DXY: {dxy_reason}",
                            "status": "dxy_conflict",
                        }), 400
            except Exception as exc:
                logger.debug("Check DXY webhook non disponibile: %s", exc)

        logger.info("Webhook ricevuto: %s %s @ %s", payload["symbol"], payload["side"], payload.get("entry"))

        try:
            provider = app.config["SYMBOL_INFO_PROVIDER"]
            validator = TradeValidator(
                payload,
                provider,
                prop_mode=app.config["PROP_MODE"],
            )
            signal = validator.validate()
            total_lot = validator.calculate_lot_size(signal)
            order = validator.build_order(signal)

            now_utc = utils.utc_now()
            cutoff_reached = (
                signal.mode == "daytrading"
                and (now_utc.hour, now_utc.minute) >= (
                    config.DAYTRADING_CLOSE_HOUR_UTC,
                    config.DAYTRADING_CLOSE_MINUTE_UTC,
                )
            )
            if cutoff_reached:
                logger.info(
                    "Webhook daytrading %s rifiutato: oltre l'orario EOD.",
                    signal.symbol,
                )
                return jsonify({
                    "error": "orario EOD daytrading superato",
                    "status": "rejected",
                }), 409

            q: Optional[queue.Queue] = app.config.get("WEBHOOK_QUEUE")
            if q is not None:
                try:
                    q.put_nowait({
                        "order": order,
                        "total_lot": total_lot,
                        "mode": signal.mode or "daytrading",
                        "tp1": signal.tp1,
                        "tp2": signal.tp2 if signal.has_tp2 else signal.tp1,
                        "tp3": signal.tp3 if signal.has_tp3 else None,
                        "direction": signal.side.value.upper(),
                    })
                except queue.Full:
                    logger.error("Webhook queue piena: ordine %s scartato.", order["symbol"])
                    return jsonify({"error": "bot overload: work queue full", "status": "overloaded"}), 503

                logger.info("Webhook %s %s accodato per esecuzione main thread.", order["symbol"], order["side"])
                return jsonify({
                    "status": "accepted",
                    "message": "Ordine accodato per esecuzione.",
                    "lot_total": total_lot,
                }), 202

            # Fallback solo se la queue non e' disponibile
            eng: MT5Engine = app.config["ENGINE"]
            result = eng.place_order(order, plan_key="main")

            if result.ok and result.ticket:
                tracker = get_tracker()
                tracker.register(
                    ticket=result.ticket,
                    tp1=signal.tp1,
                    tp2=signal.tp2 if signal.has_tp2 else signal.tp1,
                    tp3=signal.tp3 if signal.has_tp3 else None,
                    initial_volume=total_lot,
                    direction=signal.side.value,
                )

            tp_label = ("TP3" if signal.has_tp3 else "TP2" if signal.has_tp2 else "TP1")
            msg = (
                f"\U0001F916 WEBHOOK TRADE\n"
                f"{signal.symbol}: {signal.side.value.upper()}\n"
                f"Entry: {signal.entry} | SL: {signal.sl}\n"
                f"TP1: {signal.tp1} | TP2: {signal.tp2}"
                f"{' | TP3: ' + str(signal.tp3) if signal.has_tp3 else ''}\n"
                f"Lotto: {total_lot} | {tp_label} runner\n"
                f"R:R: {abs(signal.farthest_tp - signal.entry) / signal.risk_distance:.1f}"
            )
            try:
                notifier_fn = app.config.get("NOTIFIER")
                if callable(notifier_fn):
                    notifier_fn(msg)
            except Exception:
                pass

            return jsonify({
                "status": "success" if result.ok else "partial",
                "order": {"ticket": result.ticket, "ok": result.ok},
                "lot_total": total_lot,
            }), 200

        except InvalidSignalError as e:
            logger.warning("Segnale non valido: %s", e)
            return jsonify({"error": str(e), "status": "rejected"}), 400
        except LotSizingError as e:
            logger.error("Errore calcolo lotti: %s", e)
            return jsonify({"error": str(e), "status": "lot_error"}), 422
        except Exception as e:
            logger.exception("Errore webhook")
            return jsonify({"error": str(e), "status": "error"}), 500

    # ======================================================================
    # PERFORMANCE DATA (pie chart: daily/weekly/monthly P&L)
    # ======================================================================

    @app.route("/api/performance", methods=["GET"])
    def api_performance():
        """API per il grafico a torta: P&L giornaliero, settimanale, mensile."""
        from mt5_adapter import mt5

        def _compute(start_dt: datetime) -> dict:
            try:
                eng = app.config.get("ENGINE")
                if not eng or not eng.is_initialized:
                    return {"profit": 0, "loss": 0, "net": 0, "count": 0}
                deals = mt5.history_deals_get(
                    from_date=start_dt,
                    to_date=utils.utc_now(),
                )
                if not deals:
                    return {"profit": 0, "loss": 0, "net": 0, "count": 0}
                profit = 0.0
                loss = 0.0
                count = 0
                for d in deals:
                    if d.entry != mt5.DEAL_ENTRY_OUT:
                        continue
                    pnl = (
                        float(getattr(d, "profit", 0) or 0)
                        + float(getattr(d, "swap", 0) or 0)
                        + float(getattr(d, "commission", 0) or 0)
                    )
                    if pnl > 0:
                        profit += pnl
                    elif pnl < 0:
                        loss += pnl
                    count += 1
                return {
                    "profit": round(profit, 2),
                    "loss": round(loss, 2),
                    "net": round(profit + loss, 2),
                    "count": count,
                }
            except Exception:
                return {"profit": 0, "loss": 0, "net": 0, "count": 0}

        now = utils.utc_now()
        daily_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        weekday = now.weekday()
        weekly_start = (now - timedelta(days=weekday)).replace(hour=0, minute=0, second=0, microsecond=0)
        monthly_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        return jsonify({
            "daily": _compute(daily_start),
            "weekly": _compute(weekly_start),
            "monthly": _compute(monthly_start),
        })

    # ======================================================================
    # STORICO POSIZIONI CHIUSE (API /api/history)
    # ======================================================================

    @app.route("/api/history", methods=["GET"])
    def api_history():
        """API per storico posizioni chiuse con filtro per data/ora.

        Query params:
            from : ISO datetime string (es. 2026-07-30T00:00)
            to   : ISO datetime string (es. 2026-07-31T23:59)
            period: 'daily' | 'weekly' | 'monthly' (override from/to)

        Se non specificati, torna le ultime 24h.
        """
        from mt5_adapter import mt5
        from datetime import timezone

        eng = app.config.get("ENGINE")
        if not eng or not eng.is_initialized:
            return jsonify({"positions": [], "summary": {"total_pnl": 0, "wins": 0, "losses": 0, "count": 0}})

        from_str = request.args.get("from")
        to_str = request.args.get("to")
        period = request.args.get("period", "").lower()

        now = utils.utc_now()

        if period in ("daily", "weekly", "monthly"):
            if period == "daily":
                from_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
            elif period == "weekly":
                wd = now.weekday()
                from_dt = (now - timedelta(days=wd)).replace(hour=0, minute=0, second=0, microsecond=0)
            else:
                from_dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            to_dt = now + timedelta(minutes=1)
        elif from_str and to_str:
            try:
                from_dt = datetime.fromisoformat(from_str)
                to_dt = datetime.fromisoformat(to_str)
            except ValueError:
                return jsonify({"error": "Formato data non valido. Usa ISO: 2026-07-30T00:00"}), 400
        else:
            from_dt = now - timedelta(hours=24)
            to_dt = now + timedelta(minutes=1)

        # Assicura timezone UTC
        if from_dt.tzinfo is None:
            from_dt = from_dt.replace(tzinfo=timezone.utc)
        if to_dt.tzinfo is None:
            to_dt = to_dt.replace(tzinfo=timezone.utc)

        positions = _query_closed_positions(from_dt, to_dt, mt5)

        # Summary
        total_pnl = sum(p["profit"] for p in positions)
        wins = sum(1 for p in positions if p["profit"] > 0)
        losses = sum(1 for p in positions if p["profit"] < 0)

        return jsonify({
            "positions": positions,
            "summary": {
                "total_pnl": round(total_pnl, 2),
                "wins": wins,
                "losses": losses,
                "count": len(positions),
                "win_rate": round(wins / len(positions) * 100, 1) if positions else 0,
            },
            "period": {
                "from": from_dt.isoformat(),
                "to": to_dt.isoformat(),
            },
        })

    # ======================================================================
    # HEALTH CHECK
    # ======================================================================

    @app.route("/health", methods=["GET"])
    def health():
        """Health check endpoint."""
        eng = app.config.get("ENGINE")
        mb = app.config.get("MASTER_BOT")
        if mb is not None and hasattr(mb, "is_mt5_ready"):
            mt5_ok = bool(mb.is_mt5_ready)
        else:
            mt5_ok = eng.is_initialized if eng else False
        return jsonify({
            "status": "ok" if mt5_ok else "mt5_disconnected",
            "mt5": mt5_ok,
        }), 200

    # ======================================================================
    # LOGS API
    # ======================================================================

    @app.route("/api/logs/clear", methods=["POST"])
    def api_logs_clear():
        """Svuota il file dei log (bot_smc.log)."""
        log_path = os.path.join(os.path.dirname(__file__), "bot_smc.log")
        try:
            if os.path.exists(log_path):
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write("")
            logger.info("[LOGS] File dei log svuotato dalla dashboard.")
            mb = app.config.get("MASTER_BOT")
            if mb is not None and hasattr(mb, "notify_logs_cleared"):
                try:
                    mb.notify_logs_cleared()
                except Exception:
                    pass
            return jsonify({"status": "ok"}), 200
        except Exception as e:
            logger.error("[LOGS] Errore svuotamento log: %s", e)
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.route("/api/logs/export", methods=["GET"])
    def api_logs_export():
        """Esporta il file dei log completo come download."""
        log_path = os.path.join(os.path.dirname(__file__), "bot_smc.log")
        if not os.path.exists(log_path):
            return jsonify({"error": "file log non trovato"}), 404
        return send_file(
            log_path,
            as_attachment=True,
            download_name="bot_smc.log",
            mimetype="text/plain",
        )

    return app
