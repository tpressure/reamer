#!/usr/bin/env python3

import argparse
import json
import math
import secrets
import socket
import threading
import time
from collections import deque
from hashlib import sha256
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs
from migrations import MigrationStore, vm_identity

DEFAULT_PORT = 12345
DEFAULT_HTTP_PORT = 8080
DEFAULT_HEALTHY_THRESHOLD_MS = 5000
DEFAULT_WARNING_THRESHOLD_MS = 10000
HISTORY_SECONDS = 60
HISTOGRAM_BUCKET_SECONDS = 5
# Keep complete edge buckets while they slide out of the 60-second viewport.
HISTORY_RETENTION_SECONDS = HISTORY_SECONDS + HISTOGRAM_BUCKET_SECONDS - 1
METRIC_NAMES = ("cpu_percent", "memory_percent")
STRESS_KINDS = ("cpu", "memory")


class HeartbeatServer:
    def __init__(
        self,
        host: str,
        port: int,
        enable_http: bool = False,
        http_port: int = DEFAULT_HTTP_PORT,
        healthy_threshold_ms: int = DEFAULT_HEALTHY_THRESHOLD_MS,
        warning_threshold_ms: int = DEFAULT_WARNING_THRESHOLD_MS,
        migration_db: str = ':memory:',
    ) -> None:
        self.host = host
        self.port = port
        self.enable_http = enable_http
        self.http_port = http_port
        self.healthy_threshold_ms = healthy_threshold_ms
        self.warning_threshold_ms = warning_threshold_ms
        self.clients = {}
        self.lock = threading.Lock()
        self.pending_commands = {}
        self.control_token = secrets.token_urlsafe(32)
        self.migrations = MigrationStore(migration_db)

    def serve_forever(self) -> None:
        if self.enable_http:
            self.start_http_server()

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.port))
            sock.listen()

            print(f"Listening on {self.host}:{self.port}")

            while True:
                conn, addr = sock.accept()
                thread = threading.Thread(
                    target=self.handle_client,
                    args=(conn, addr),
                    daemon=True,
                )
                thread.start()

    def handle_client(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        peer = f"{addr[0]}:{addr[1]}"
        print(f"Client connected: {peer}")

        with conn:
            reader = conn.makefile("r", encoding="utf-8")
            for line in reader:
                line = line.strip()
                if not line:
                    continue

                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    print(f"Ignoring malformed message from {peer}: {line}")
                    continue

                if not isinstance(message, dict) or message.get("type") != "heartbeat":
                    print(f"Ignoring unknown message type from {peer}: {message!r}")
                    continue

                client_id = message.get("client_id") or peer
                if not isinstance(client_id, str):
                    continue
                supports_control = message.get("control_protocol") == 1
                self.record_heartbeat(client_id, peer, message.get("metrics"), message.get("stress"), supports_control, message.get("vm_uuid"))
                if supports_control:
                    reply = {"type": "heartbeat_ack", "commands": self.commands_for(client_id)}
                    try:
                        conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
                    except OSError:
                        break

        print(f"Client disconnected: {peer}")

    def record_heartbeat(self, client_id: str, peer: str, metrics: object, stress: object = None, supports_control: bool = False, vm_uuid: object = None) -> None:
        timestamp = datetime.now(timezone.utc)
        second = int(time.monotonic())
        if not isinstance(metrics, dict):
            metrics = {}
        with self.lock:
            client = self.clients.setdefault(client_id, {
                "max_gap_ms": 0,
                "last_heartbeat": timestamp,
                "history": deque(maxlen=HISTORY_RETENTION_SECONDS),
            })
            client["max_gap_ms"] = max(
                client["max_gap_ms"], self.get_interval_ms(client["last_heartbeat"], timestamp)
            )
            client.update(address=peer, last_heartbeat=timestamp)
            client['vm_uuid'] = vm_identity(vm_uuid)
            for name, maximum in (("vcpu_count", 2**20), ("memory_total_bytes", 2**63 - 1)):
                value = metrics.get(name)
                client[name] = value if type(value) is int and 0 < value <= maximum else None
            client["supports_control"] = supports_control
            client["stress"] = self.validate_stress(stress)
            for kind, state in client["stress"].items():
                pending = self.pending_commands.get((client_id, kind))
                if pending and pending["id"] == state["last_command"]:
                    del self.pending_commands[(client_id, kind)]
            history = client["history"]
            self.prune_history(history, second)
            # At most one aggregate per second, even with very fast heartbeats.
            if not history or history[-1]["second"] != second:
                history.append({"second": second})
            for name in METRIC_NAMES:
                value = metrics.get(name)
                if type(value) in (int, float) and 0 <= value <= 100:
                    total, count = history[-1].get(name, (0, 0))
                    history[-1][name] = (total + value, count + 1)
            self.print_clients_locked()

    @staticmethod
    def validate_stress(stress: object) -> dict:
        result = {}
        for kind in STRESS_KINDS:
            raw = stress.get(kind, {}) if isinstance(stress, dict) else {}
            if not isinstance(raw, dict):
                raw = {}
            state = raw.get("state")
            result[kind] = {
                "state": state if state in ("idle", "running", "stopping", "error") else "unknown",
                "last_command": raw.get("last_command", "")[:128] if isinstance(raw.get("last_command", ""), str) else "",
                "error": raw.get("error", "")[:200] if isinstance(raw.get("error", ""), str) else "",
            }
            if kind == "memory":
                value = raw.get("write_mibps")
                result[kind]["write_mibps"] = (
                    value if state == "running" and type(value) in (int, float)
                    and 0 <= value <= 1e12 and math.isfinite(value) else None
                )
        return result

    def request_stress(self, client_id: str, kind: str, action: str) -> None:
        if kind not in STRESS_KINDS or action not in ("start", "stop"):
            raise ValueError("Unknown stress test or action")
        with self.lock:
            client = self.clients.get(client_id)
            if not client or not client.get("supports_control"):
                raise ValueError("Client is unavailable or needs an upgrade")
            if action == "start" and self.get_heartbeat_age_ms(client["last_heartbeat"]) > self.warning_threshold_ms:
                raise ValueError("Wait for the client to reconnect before starting a test")
            if action == "start" and client.get("stress", {}).get(kind, {}).get("state") == "stopping":
                raise ValueError("Wait for the current stress test to finish stopping")
            self.pending_commands[(client_id, kind)] = {
                "id": secrets.token_hex(16), "kind": kind, "action": action, "created": time.monotonic(),
            }
            client["control_error"] = ""

    def commands_for(self, client_id: str) -> list[dict]:
        with self.lock:
            self.expire_commands_locked()
            return [
                {key: command[key] for key in ("id", "kind", "action")}
                for (target, _), command in self.pending_commands.items() if target == client_id
            ]

    def expire_commands_locked(self) -> None:
        for key, command in list(self.pending_commands.items()):
            if command["action"] == "start" and time.monotonic() - command["created"] > 30:
                del self.pending_commands[key]
                if key[0] in self.clients:
                    self.clients[key[0]]["control_error"] = "Start request expired; try again when the client reconnects."

    @staticmethod
    def prune_history(history: deque, second: int) -> None:
        first_bucket = ((second + 1 - HISTORY_SECONDS) // HISTOGRAM_BUCKET_SECONDS) * HISTOGRAM_BUCKET_SECONDS
        while history and history[0]["second"] < first_bucket:
            history.popleft()

    def print_clients_locked(self) -> None:
        print("\nKnown clients:")
        for client_id in sorted(self.clients):
            client = self.clients[client_id]
            print(
                f"- {client_id} ({client['address']}) last heartbeat: "
                f"{self.format_timestamp(client['last_heartbeat'])}, "
                f"max heartbeat gap: {client['max_gap_ms']} ms"
            )
        print()

    def start_http_server(self) -> None:
        server = self
        logo = (Path(__file__).resolve().parent / "static" / "reamer-logo.webp").read_bytes()
        logo_etag = f'"{sha256(logo).hexdigest()}"'

        class StatusHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/static/reamer-logo.webp":
                    not_modified = self.headers.get("If-None-Match") == logo_etag
                    self.send_response(304 if not_modified else 200)
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.send_header("ETag", logo_etag)
                    if not not_modified:
                        self.send_header("Content-Type", "image/webp")
                        self.send_header("Content-Length", str(len(logo)))
                    self.end_headers()
                    if not not_modified:
                        self.wfile.write(logo)
                    return

                if self.path not in ("/", "/status"):
                    self.send_error(404, "Not Found")
                    return

                page = server.render_status_page() if self.path == "/" else server.render_status_content()
                body = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                if self.path == '/compute-report':
                    try:
                        self.connection.settimeout(10)
                        length = int(self.headers.get('Content-Length', '0'))
                        if not 0 < length <= 262144 or self.headers.get_content_type() != 'application/json':
                            raise ValueError('Expected a JSON report of at most 256 KiB')
                        reply = server.migrations.ingest(json.loads(self.rfile.read(length)))
                    except (ValueError, UnicodeError) as exc:
                        self.send_error(400, str(exc))
                        return
                    body = json.dumps(reply).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == "/stress":
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                        if not 0 < length <= 4096:
                            raise ValueError("Invalid request size")
                        fields = parse_qs(self.rfile.read(length).decode("utf-8"), strict_parsing=True)
                        token = fields.get("token", [""])[0]
                        if not secrets.compare_digest(token.encode("utf-8"), server.control_token.encode("ascii")):
                            self.send_error(403, "Reload the page before sending a command")
                            return
                        server.request_stress(fields.get("client_id", [""])[0], fields.get("kind", [""])[0], fields.get("action", [""])[0])
                    except (ValueError, UnicodeError) as exc:
                        self.send_error(400, str(exc))
                        return
                    if self.headers.get("X-Requested-With") == "fetch":
                        self.send_response(204)
                    else:
                        self.send_response(303)
                        self.send_header("Location", "/")
                    self.end_headers()
                    return

                if self.path != "/reset":
                    self.send_error(404, "Not Found")
                    return

                server.clear_clients()
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        http_server = ThreadingHTTPServer((self.host, self.http_port), StatusHandler)
        thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        thread.start()
        print(f"HTTP status page enabled on http://{self.host}:{self.http_port}/")

    def render_status_content(self) -> str:
        second = int(time.monotonic())
        clients = self.get_clients_snapshot(second)
        total_clients = len(clients)
        ordered_clients = []
        for client in clients:
            age_ms = self.get_heartbeat_age_ms(client["last_heartbeat"])
            status = self.get_client_status(age_ms)
            ordered_clients.append((self.get_status_priority(status["css_class"]), client, age_ms, status))

        ordered_clients.sort(key=lambda item: (item[0], item[1]["client_id"]))
        summary_status_class = self.get_summary_status_class(ordered_clients)
        counts = {"status-healthy": 0, "status-warning": 0, "status-stale": 0}
        for _, _, _, status in ordered_clients:
            counts[status["css_class"]] += 1
        summary_label = {
            "status-healthy": "All clients healthy",
            "status-warning": "Delayed heartbeats",
            "status-stale": "Clients need attention",
        }[summary_status_class]
        if not clients:
            summary_status_class = "status-empty"
            summary_label = "Waiting for heartbeats"

        rows = []
        for _, client, age_ms, status in ordered_clients:
            max_gap_ms = max(client["max_gap_ms"], age_ms)
            rows.append(
                f"<tr class=\"{status['css_class']}\">"
                f'<td><span class="status-badge {status["css_class"]}">{escape(status["label"])}</span></td>'
                f'<th scope="row" class="client-id">{escape(client["client_id"])}'
                f'<span class="client-capacity" title="Available vCPUs and total usable RAM reported by the client">{self.format_capacity(client)}</span></th>'
                f'<td class="mono">{escape(client["address"])}</td>'
                f'<td class="timestamp">{escape(self.format_timestamp(client["last_heartbeat"]))}</td>'
                f'<td class="numeric">{age_ms} <span class="unit">ms</span></td>'
                f'<td class="numeric">{max_gap_ms} <span class="unit">ms</span></td>'
                "</tr>"
            )
            rows.append(
                '<tr class="history-row"><td colspan="6"><div class="client-history">'
                + self.render_histogram(client["history"], "cpu_percent", "CPU", second)
                + self.render_histogram(client["history"], "memory_percent", "Memory", second)
                + self.render_stress_controls(client, age_ms)
                + '</div></td></tr>'
            )
            rows.append('<tr class="migration-row"><td colspan="6">'
                        + self.render_migration_details(client) + '</td></tr>')

        if not rows:
            rows.append(
                '<tr><td colspan="6" class="empty-state">'
                '<strong>No heartbeats received yet.</strong>'
                '<span>Clients will appear here when their first heartbeat arrives.</span>'
                '</td></tr>'
            )

        table_rows = "\n".join(rows)

        return f'''
    <section class="overview" aria-label="Client summary">
      <div class="metric" aria-label="Total Clients: {total_clients}">
        <span class="metric-label">Total clients</span>
        <span class="metric-value">{total_clients}</span>
      </div>
      <div class="metric status-healthy">
        <span class="metric-label">Healthy</span>
        <span class="metric-value">{counts["status-healthy"]}</span>
      </div>
      <div class="metric status-warning">
        <span class="metric-label">Warning</span>
        <span class="metric-value">{counts["status-warning"]}</span>
      </div>
      <div class="metric status-stale">
        <span class="metric-label">Stale</span>
        <span class="metric-value">{counts["status-stale"]}</span>
      </div>
    </section>
    <section aria-labelledby="clients-heading">
      <div class="section-heading">
        <div class="clients-title"><h2 id="clients-heading">Clients</h2><span class="history-key">60s history · 5s averages · 0–100%</span></div>
        <span class="summary-status {summary_status_class}">{summary_label}</span>
      </div>
      <div class="table-wrap{' is-empty' if not clients else ''}" tabindex="0" role="region" aria-label="Client heartbeat details">
        <table aria-labelledby="clients-heading">
          <thead>
            <tr>
              <th scope="col">Status</th>
              <th scope="col">Client ID</th>
              <th scope="col">Address</th>
              <th scope="col">Last heartbeat</th>
              <th scope="col" class="numeric">Age</th>
              <th scope="col" class="numeric">Max gap</th>
            </tr>
          </thead>
          <tbody>
            {table_rows}
          </tbody>
        </table>
      </div>
    </section>
'''

    def render_status_page(self) -> str:
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Reamer · Heartbeat Status</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0c1118;
      --panel: #121a24;
      --border: #253140;
      --text: #e7edf5;
      --muted: #9baabd;
      --healthy: #72dbac;
      --warning: #f0c674;
      --stale: #ff959e;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--text); font: 14px/1.5 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    main {{ max-width: 1240px; margin: 0 auto; padding: 24px 32px 40px; }}
    .brand-bar {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; padding-bottom: 16px; border-bottom: 1px solid var(--border); }}
    .logo {{ display: block; width: 176px; height: 64px; object-fit: cover; opacity: 0.85; }}
    .refresh-note {{ color: var(--muted); font-size: 12px; text-align: right; }}
    .page-header {{ display: flex; align-items: center; justify-content: space-between; gap: 24px; margin: 28px 0 24px; }}
    h1 {{ margin: 0; font-size: clamp(24px, 4vw, 32px); font-weight: 650; letter-spacing: -0.035em; }}
    .subtitle {{ margin: 6px 0 0; color: var(--muted); }}
    form {{ margin: 0; }}
    .reset-button {{ background: transparent; border: 1px solid #435164; border-radius: 8px; color: var(--text); cursor: pointer; font: inherit; font-size: 13px; padding: 10px 16px; white-space: nowrap; }}
    .reset-button:hover {{ background: #2b1c25; border-color: var(--stale); color: var(--stale); }}
    :focus-visible {{ outline: 2px solid #71c7ff; outline-offset: 4px; }}
    .overview {{ display: grid; grid-template-columns: 1.4fr repeat(3, 1fr); background: var(--panel); border: 1px solid var(--border); border-radius: 12px; margin-bottom: 28px; overflow: hidden; }}
    .metric {{ padding: 20px 24px; }}
    .metric + .metric {{ border-left: 1px solid var(--border); }}
    .metric-label {{ display: block; color: var(--muted); font-size: 12px; font-weight: 600; }}
    .metric-value {{ display: block; margin-top: 4px; font-size: 30px; font-weight: 600; line-height: 1.2; font-variant-numeric: tabular-nums; letter-spacing: -0.035em; }}
    .status-healthy {{ --status-color: var(--healthy); }}
    .status-warning {{ --status-color: var(--warning); }}
    .status-stale {{ --status-color: var(--stale); }}
    .status-empty {{ --status-color: var(--muted); }}
    .metric .metric-value {{ color: var(--status-color, var(--text)); }}
    .section-heading {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 12px; }}
    h2 {{ margin: 0; font-size: 16px; font-weight: 600; }}
    .clients-title {{ display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 14px; }}
    .history-key {{ color: var(--muted); font-size: 10px; }}
    .summary-status {{ color: var(--status-color); font-size: 12px; }}
    .summary-status::before, .status-badge::before {{ content: ""; display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: currentColor; margin-right: 7px; vertical-align: middle; }}
    .table-wrap {{ overflow-x: auto; border: 1px solid var(--border); border-radius: 12px; background: var(--panel); scrollbar-color: #435164 var(--panel); scrollbar-width: thin; }}
    table {{ border-collapse: collapse; width: 100%; text-align: left; }}
    .table-wrap.is-empty thead {{ display: none; }}
    th, td {{ padding: 16px 20px; white-space: nowrap; }}
    thead th {{ background: #18222e; color: var(--muted); font-size: 11px; font-weight: 600; letter-spacing: 0.045em; text-transform: uppercase; }}
    tbody tr + tr {{ border-top: 1px solid var(--border); }}
    tbody tr:hover {{ background: #18222e; }}
    tbody tr.history-row {{ border-top: 0; }}
    .history-row td {{ padding-top: 0; padding-bottom: 14px; }}
    .client-history {{ display: flex; flex-wrap: wrap; gap: 8px 28px; }}
    .history-chart {{ --chart-color: #71c7ff; display: grid; grid-template-columns: 100px 108px; align-items: center; gap: 12px; }}
    .history-chart.memory_percent {{ --chart-color: #b4a2ff; }}
    .history-heading {{ display: flex; justify-content: space-between; align-items: baseline; gap: 10px; font-size: 11px; }}
    .history-heading strong {{ color: var(--muted); font-weight: 500; }}
    .history-reading {{ color: var(--text); font-size: 11px; font-variant-numeric: tabular-nums; }}
    .history-bars {{ position: relative; overflow: hidden; height: 22px; border-bottom: 1px solid var(--border); }}
    .history-bar {{ position: absolute; bottom: 0; height: 100%; width: calc(100% / 12 - 2px); display: flex; align-items: end; }}
    .history-bar i {{ display: block; width: 100%; min-height: 1px; background: var(--chart-color); opacity: 0.65; border-radius: 1px 1px 0 0; }}
    .history-bar:hover i {{ opacity: 1; }}
    .stress-controls {{ display: flex; align-items: center; flex-wrap: wrap; gap: 6px; margin-left: auto; }}
    .stress-button {{ padding: 5px 9px; border: 1px solid #435164; border-radius: 5px; background: transparent; color: var(--muted); font: inherit; font-size: 11px; cursor: pointer; white-space: nowrap; }}
    .stress-button:hover:not(:disabled) {{ color: var(--text); border-color: #71c7ff; }}
    .stress-button.running {{ color: var(--warning); border-color: #6a5531; }}
    .stress-button:disabled, .stress-button[aria-disabled="true"] {{ opacity: 0.5; cursor: default; }}
    .stress-error, .control-feedback {{ color: var(--stale); font-size: 11px; white-space: normal; }}
    .stress-error {{ flex-basis: 100%; max-width: 440px; }}
    .stress-bandwidth {{ color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; white-space: nowrap; }}
    .migration-row {{ display: none; }}
    .show-migrations .migration-row {{ display: table-row; }}
    .migration-row td {{ padding-top: 0; font-size: 12px; color: var(--muted); }}
    .migration-details {{ display: flex; flex-wrap: wrap; gap: 8px 20px; font-variant-numeric: tabular-nums; }}
    .migration-details strong {{ color: var(--text); font-weight: 600; }}
    .migration-toggle {{ display: inline-flex; gap: 8px; align-items: center; color: var(--muted); font-size: 12px; margin-bottom: 14px; cursor: pointer; }}
    .control-feedback:empty {{ display: none; }}
    .client-id {{ font-weight: 600; white-space: normal; overflow-wrap: anywhere; min-width: 120px; max-width: 280px; }}
    .client-capacity {{ display: block; margin-top: 3px; color: var(--muted); font-size: 11px; font-weight: 400; white-space: nowrap; }}
    .status-badge {{ display: inline-block; border: 1px solid currentColor; border-radius: 6px; padding: 3px 8px; color: var(--status-color); font-size: 11px; font-weight: 600; }}
    .mono, .numeric {{ font-family: ui-monospace, "SFMono-Regular", Consolas, monospace; font-size: 12px; font-variant-numeric: tabular-nums; }}
    .mono, .timestamp {{ color: var(--muted); }}
    .timestamp {{ font-size: 12px; font-variant-numeric: tabular-nums; }}
    .numeric {{ text-align: right; }}
    .unit {{ color: var(--muted); }}
    .empty-state {{ padding: 52px 20px; text-align: center; white-space: normal; }}
    .empty-state strong {{ display: block; font-size: 15px; font-weight: 500; }}
    .empty-state span {{ display: block; margin-top: 6px; color: var(--muted); font-size: 13px; }}
    .page-footer {{ display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px 24px; margin-top: 16px; color: var(--muted); font-size: 11px; }}
    .thresholds {{ display: flex; flex-wrap: wrap; gap: 8px 18px; }}
    .thresholds span {{ white-space: nowrap; }}
    @media (max-width: 640px) {{
      main {{ padding: 16px; }}
      .logo {{ width: 143px; height: 52px; }}
      .page-header {{ align-items: flex-start; gap: 16px; }}
      .subtitle {{ max-width: 220px; font-size: 12px; }}
      .reset-button {{ padding: 9px 12px; font-size: 12px; }}
      .overview {{ grid-template-columns: repeat(2, 1fr); }}
      .metric {{ padding: 16px 20px; }}
      .metric:nth-child(3) {{ border-left: 0; }}
      .metric:nth-child(n+3) {{ border-top: 1px solid var(--border); }}
      th, td {{ padding: 14px 16px; }}
      .client-history {{ width: calc(100vw - 66px); gap: 8px; }}
      .history-chart {{ grid-template-columns: 100px minmax(0, 1fr); width: 100%; }}
      .summary-status {{ flex-shrink: 0; font-size: 11px; white-space: nowrap; }}
      .stress-controls {{ margin-left: 0; }}
    }}
  </style>
</head>
<body>
  <main>
    <div class="brand-bar">
      <img class="logo" src="/static/reamer-logo.webp" alt="Reamer" width="176" height="64">
      <span class="refresh-note" id="refresh-note" role="status">Auto-refresh · every second</span>
    </div>
    <header class="page-header">
      <div>
        <h1>Heartbeat status</h1>
        <p class="subtitle">Connection health and resource usage across your clients.</p>
      </div>
      <form method="post" action="/reset">
        <button type="submit" class="reset-button">Reset Clients</button>
      </form>
    </header>
    <p id="control-feedback" class="control-feedback" role="status"></p>
    <label class="migration-toggle"><input id="migration-toggle" type="checkbox">Show migration details</label>
    <noscript><p class="subtitle">JavaScript is disabled. Reload this page to update client status.</p></noscript>
    {self.render_status_content()}
    <footer class="page-footer">
      <div class="thresholds" aria-label="Heartbeat age thresholds">
        <span>Healthy ≤ {self.healthy_threshold_ms} ms</span>
        <span>Warning &gt; {self.healthy_threshold_ms} and ≤ {self.warning_threshold_ms} ms</span>
        <span>Stale &gt; {self.warning_threshold_ms} ms</span>
      </div>
      <span>Clients needing attention appear first</span>
    </footer>
  </main>
  <script>
    const refreshNote = document.getElementById("refresh-note");
    const migrationToggle = document.getElementById("migration-toggle");
    try {{ migrationToggle.checked = localStorage.getItem("reamer-migrations") === "true"; }} catch (error) {{}}
    function toggleMigrations() {{
      document.body.classList.toggle("show-migrations", migrationToggle.checked);
      try {{ localStorage.setItem("reamer-migrations", String(migrationToggle.checked)); }} catch (error) {{}}
    }}
    migrationToggle.addEventListener("change", toggleMigrations);
    toggleMigrations();
    document.addEventListener("submit", async event => {{
      const form = event.target;
      if (!form.matches(".stress-form")) return;
      event.preventDefault();
      const button = form.querySelector("button");
      if (button.getAttribute("aria-disabled") === "true") return;
      const feedback = document.getElementById("control-feedback");
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      button.setAttribute("aria-disabled", "true");
      feedback.textContent = "";
      try {{
        const response = await fetch("/stress", {{
          method: "POST", body: new URLSearchParams(new FormData(form)),
          headers: {{"X-Requested-With": "fetch"}}, signal: controller.signal,
        }});
        if (!response.ok) throw new Error(response.status === 403
          ? "The server restarted. Wait for the next update and try again."
          : "Could not send the stress command. Check the client connection and try again.");
      }} catch (error) {{
        feedback.textContent = error.message;
      }} finally {{
        clearTimeout(timeout);
        button.removeAttribute("aria-disabled");
      }}
    }});
    async function refreshStatus() {{
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 5000);
      try {{
        const response = await fetch("/status", {{
          cache: "no-store",
          signal: controller.signal,
        }});
        if (!response.ok) throw new Error("Status request failed");
        const updated = new DOMParser().parseFromString(await response.text(), "text/html");
        // Keep the document, logo, and scrollable table container mounted.
        const selectors = [".overview", ".summary-status", "tbody"];
        const replacements = selectors.map(selector => updated.querySelector(selector));
        const table = updated.querySelector(".table-wrap");
        if (!table || replacements.some(element => !element)) {{
          throw new Error("Invalid status response");
        }}
        const focusedForm = document.activeElement.closest(".stress-form");
        const focusedControl = focusedForm ? [focusedForm.elements.client_id.value, focusedForm.elements.kind.value] : null;
        selectors.forEach((selector, index) => {{
          const current = document.querySelector(selector);
          const next = replacements[index];
          current.className = next.className;
          if (current.innerHTML !== next.innerHTML) {{
            current.replaceChildren(...next.childNodes);
          }}
        }});
        document.querySelector(".table-wrap").className = table.className;
        if (focusedControl) {{
          const form = Array.from(document.querySelectorAll(".stress-form")).find(form =>
            form.elements.client_id.value === focusedControl[0] && form.elements.kind.value === focusedControl[1]);
          if (form) form.querySelector("button").focus({{preventScroll: true}});
        }}
        refreshNote.textContent = "Auto-refresh · every second";
      }} catch (error) {{
        refreshNote.textContent = "Updates interrupted · retrying…";
      }} finally {{
        clearTimeout(timeout);
        // Wait for completion so slow requests cannot overlap or arrive out of order.
        setTimeout(refreshStatus, 1000);
      }}
    }}
    setTimeout(refreshStatus, 1000);
  </script>
</body>
</html>
"""

    def render_migration_details(self, client: dict) -> str:
        stats = self.migrations.summary(client.get('vm_uuid'))
        if not stats:
            text = 'Waiting for compute-node reports' if client.get('vm_uuid') else 'VM identity unavailable — update the client to report its DMI UUID'
            return f'<div class="migration-details">{text}</div>'
        node = escape(stats['node']) if stats['node'] else 'Unknown / in transit / stopped'
        if stats['node_stale'] and stats['node']:
            node += ' (report stale)'
        def duration(value):
            return '—' if value is None else f'{value:g} ms'
        return ('<div class="migration-details">'
                f'<span title="Latest boot or receive-completion event in the compute logs">Node <strong>{node}</strong></span>'
                f'<span title="{escape(stats["last_at"] or "No completed migration reported")}">Last downtime <strong>{duration(stats["last_ms"])}</strong></span>'
                f'<span>Min {duration(stats["min_ms"])} · Avg {duration(stats["avg_ms"])} · Max {duration(stats["max_ms"])}</span>'
                f'<span>{stats["count"]} migrations</span></div>')

    @staticmethod
    def format_capacity(client: dict) -> str:
        cpus = client.get("vcpu_count")
        memory = client.get("memory_total_bytes")
        cpu_label = f"{cpus} vCPU{'s' if cpus != 1 else ''}" if cpus is not None else "vCPUs —"
        if memory is None:
            memory_label = "RAM —"
        elif memory >= 1024**3:
            memory_label = f"{memory / 1024**3:.1f} GiB RAM"
        else:
            memory_label = f"{memory / 1024**2:.0f} MiB RAM"
        return f"{cpu_label} · {memory_label}"

    def render_stress_controls(self, client: dict, age_ms: int) -> str:
        forms = []
        for kind, label in (("cpu", "CPU"), ("memory", "memory")):
            state = client["stress"].get(kind, {})
            pending = client["pending"].get(kind, {})
            stopping = state.get("state") == "stopping" or pending.get("action") == "stop"
            running = state.get("state") in ("running", "stopping") or pending.get("action") == "start"
            action = "stop" if running else "start"
            text = f"Stop {label}" if running else f"{label.capitalize() if kind == 'memory' else label} stress"
            title = "Use all available vCPUs" if kind == "cpu" else "Use 80% of available vCPUs (rounded down, at least one) and half the total VM memory"
            disabled = not client["supports_control"] or (action == "start" and age_ms > self.warning_threshold_ms)
            if not client["supports_control"]:
                title = "Upgrade this client to enable stress controls"
            elif stopping:
                text, title, disabled = f"Stopping {label}…", "Waiting for process cleanup; heartbeats continue", False
            elif pending.get("action") == "start":
                text, title = f"Cancel {label}", "Start requested; click to cancel or stop"
            elif running:
                title = f"Stop the running {label} test"
            elif state.get("state") == "error":
                text, title = f"Retry {label}", state.get("error", "Stress test failed")
            pending_attribute = ' aria-disabled="true"' if stopping else ""
            forms.append(
                '<form class="stress-form" method="post" action="/stress">'
                f'<input type="hidden" name="token" value="{self.control_token}">'
                f'<input type="hidden" name="client_id" value="{escape(client["client_id"], quote=True)}">'
                f'<input type="hidden" name="kind" value="{kind}">'
                f'<input type="hidden" name="action" value="{action}">'
                f'<button class="stress-button{" running" if running else ""}" type="submit" '
                f'title="{escape(title, quote=True)}"{" disabled" if disabled else ""}'
                f'{pending_attribute}>{text}</button></form>'
            )
            if kind == "memory" and state.get("state") == "running" and not stopping:
                bandwidth = state.get("write_mibps")
                reading = "—" if bandwidth is None else (f"{bandwidth / 1024:.2f} GiB/s" if bandwidth >= 1024 else f"{bandwidth:.1f} MiB/s")
                if age_ms > self.warning_threshold_ms:
                    reading = "—"
                forms.append(f'<span class="stress-bandwidth" title="Combined memtouch write bandwidth across all workers">Write {reading}</span>')
        errors = [state["error"] for state in client["stress"].values() if state.get("error")]
        if client["control_error"]:
            errors.append(client["control_error"])
        if errors:
            forms.append(f'<span class="stress-error">{escape(" · ".join(errors))}</span>')
        return '<div class="stress-controls">' + "".join(forms) + '</div>'

    @staticmethod
    def render_histogram(history: list[dict], name: str, label: str, second: int) -> str:
        window_end = second + 1
        window_start = window_end - HISTORY_SECONDS
        first_bucket = (window_start // HISTOGRAM_BUCKET_SECONDS) * HISTOGRAM_BUCKET_SECONDS
        last_bucket = (second // HISTOGRAM_BUCKET_SECONDS) * HISTOGRAM_BUCKET_SECONDS
        buckets = {start: [0, 0] for start in range(first_bucket, last_bucket + 1, HISTOGRAM_BUCKET_SECONDS)}
        latest = None
        for sample in history:
            bucket_start = (sample["second"] // HISTOGRAM_BUCKET_SECONDS) * HISTOGRAM_BUCKET_SECONDS
            if bucket_start in buckets and sample["second"] <= second and name in sample:
                total, count = sample[name]
                bucket = buckets[bucket_start]
                bucket[0] += total
                bucket[1] += count
                if sample["second"] >= window_start:
                    latest = (total / count, second - sample["second"])

        reading = "No samples"
        display_value = "—"
        if latest is not None:
            value, age = latest
            reading = f"{value:.1f}% · {age}s ago"
            display_value = f"{value:.1f}%"
        bars = []
        for bucket_start, (total, count) in buckets.items():
            end = window_end - bucket_start
            start = max(0, end - HISTOGRAM_BUCKET_SECONDS)
            description = f"{start}–{end}s ago: "
            if bucket_start == last_bucket and window_end % HISTOGRAM_BUCKET_SECONDS:
                description += "current interval, still collecting; "
            fill = ""
            if count:
                value = total / count
                description += f"{label} {value:.1f}% average"
                fill = f'<i style="height: {value:.2f}%"></i>'
            else:
                description += "no samples"
            left = 100 * (bucket_start - window_start) / HISTORY_SECONDS
            bars.append(
                f'<span class="history-bar" data-bucket="{bucket_start}" '
                f'style="left: calc({left:.4f}% + 1px)" title="{description}">{fill}</span>'
            )

        return (
            f'<div class="history-chart {name}">'
            f'<div class="history-heading"><strong>{label}</strong>'
            f'<span class="history-reading" title="{reading}">{display_value}</span></div>'
            f'<div class="history-bars" role="img" aria-label="{label} usage over the last 60 seconds; '
            f'five-second averages on a 0 to 100 percent scale. Latest: {reading}. '
            'Completed bars keep their values as they move left; only the current interval is still collecting. '
            'Empty bars indicate missing samples.">'
            + "".join(bars)
            + '</div></div>'
        )

    def get_clients_snapshot(self, second: int | None = None) -> list[dict[str, object]]:
        if second is None:
            second = int(time.monotonic())
        with self.lock:
            self.expire_commands_locked()
            for client in self.clients.values():
                self.prune_history(client.get("history", deque()), second)
            return [
                {
                    "client_id": client_id,
                    "vm_uuid": client.get('vm_uuid'),
                    "address": client["address"],
                    "last_heartbeat": client["last_heartbeat"],
                    "max_gap_ms": client["max_gap_ms"],
                    "vcpu_count": client.get("vcpu_count"),
                    "memory_total_bytes": client.get("memory_total_bytes"),
                    "history": [dict(sample) for sample in client.get("history", [])],
                    "supports_control": client.get("supports_control", False),
                    "stress": {kind: dict(state) for kind, state in client.get("stress", {}).items()},
                    "pending": {kind: dict(command) for (target, kind), command in self.pending_commands.items() if target == client_id},
                    "control_error": client.get("control_error", ""),
                }
                for client_id, client in sorted(self.clients.items())
            ]

    def clear_clients(self) -> None:
        with self.lock:
            self.clients.clear()
            self.pending_commands.clear()
        print("Cleared all clients via HTTP reset.")

    @staticmethod
    def get_interval_ms(start: datetime, end: datetime) -> int:
        delta = end - start
        return int(delta.total_seconds() * 1000)

    @classmethod
    def get_heartbeat_age_ms(cls, timestamp: datetime) -> int:
        return cls.get_interval_ms(timestamp, datetime.now(timezone.utc))

    def get_client_status(self, age_ms: int) -> dict[str, str]:
        if age_ms <= self.healthy_threshold_ms:
            return {"label": "Healthy", "css_class": "status-healthy"}
        if age_ms <= self.warning_threshold_ms:
            return {"label": "Warning", "css_class": "status-warning"}
        return {"label": "Stale", "css_class": "status-stale"}

    @staticmethod
    def get_status_priority(css_class: str) -> int:
        priorities = {
            "status-stale": 0,
            "status-warning": 1,
            "status-healthy": 2,
        }
        return priorities.get(css_class, 99)

    def get_summary_status_class(self, ordered_clients: list[tuple[int, dict[str, object], int, dict[str, str]]]) -> str:
        if not ordered_clients:
            return "status-healthy"
        return ordered_clients[0][3]["css_class"]

    @staticmethod
    def format_timestamp(timestamp: datetime) -> str:
        return timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simple heartbeat TCP server")
    parser.add_argument("--host", default="0.0.0.0", help="Host/interface to bind to")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to listen on")
    parser.add_argument('--migration-db', default='reamer-migrations.sqlite3', help='Persistent migration database')
    parser.add_argument(
        "--enable-http",
        action="store_true",
        help="Enable an HTTP status page that lists known clients",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help="Port for the optional HTTP status server",
    )
    parser.add_argument(
        "--healthy-threshold-ms",
        type=int,
        default=DEFAULT_HEALTHY_THRESHOLD_MS,
        help="Maximum heartbeat age in milliseconds for a green status",
    )
    parser.add_argument(
        "--warning-threshold-ms",
        type=int,
        default=DEFAULT_WARNING_THRESHOLD_MS,
        help="Maximum heartbeat age in milliseconds for a yellow status",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.healthy_threshold_ms < 0 or args.warning_threshold_ms < 0:
        raise SystemExit("Heartbeat thresholds must be non-negative.")
    if args.warning_threshold_ms < args.healthy_threshold_ms:
        raise SystemExit(
            "--warning-threshold-ms must be greater than or equal to --healthy-threshold-ms."
        )

    server = HeartbeatServer(
        args.host,
        args.port,
        enable_http=args.enable_http,
        http_port=args.http_port,
        healthy_threshold_ms=args.healthy_threshold_ms,
        warning_threshold_ms=args.warning_threshold_ms,
        migration_db=args.migration_db,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
