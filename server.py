#!/usr/bin/env python3

import argparse
import json
import socket
import threading
from hashlib import sha256
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PORT = 12345
DEFAULT_HTTP_PORT = 8080
DEFAULT_HEALTHY_THRESHOLD_MS = 5000
DEFAULT_WARNING_THRESHOLD_MS = 10000


class HeartbeatServer:
    def __init__(
        self,
        host: str,
        port: int,
        enable_http: bool = False,
        http_port: int = DEFAULT_HTTP_PORT,
        healthy_threshold_ms: int = DEFAULT_HEALTHY_THRESHOLD_MS,
        warning_threshold_ms: int = DEFAULT_WARNING_THRESHOLD_MS,
    ) -> None:
        self.host = host
        self.port = port
        self.enable_http = enable_http
        self.http_port = http_port
        self.healthy_threshold_ms = healthy_threshold_ms
        self.warning_threshold_ms = warning_threshold_ms
        self.clients = {}
        self.lock = threading.Lock()

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

                if message.get("type") != "heartbeat":
                    print(f"Ignoring unknown message type from {peer}: {message!r}")
                    continue

                client_id = message.get("client_id") or peer
                timestamp = datetime.now(timezone.utc)

                with self.lock:
                    existing_client = self.clients.get(client_id)
                    max_gap_ms = 0
                    if existing_client is not None:
                        gap_ms = self.get_interval_ms(
                            existing_client["last_heartbeat"],
                            timestamp,
                        )
                        max_gap_ms = max(existing_client["max_gap_ms"], gap_ms)

                    self.clients[client_id] = {
                        "address": peer,
                        "last_heartbeat": timestamp,
                        "max_gap_ms": max_gap_ms,
                    }
                    self.print_clients_locked()

        print(f"Client disconnected: {peer}")

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
        clients = self.get_clients_snapshot()
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
                f'<th scope="row" class="client-id">{escape(client["client_id"])}</th>'
                f'<td class="mono">{escape(client["address"])}</td>'
                f'<td class="timestamp">{escape(self.format_timestamp(client["last_heartbeat"]))}</td>'
                f'<td class="numeric">{age_ms} <span class="unit">ms</span></td>'
                f'<td class="numeric">{max_gap_ms} <span class="unit">ms</span></td>'
                "</tr>"
            )

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
        <h2 id="clients-heading">Clients</h2>
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
    .summary-status {{ color: var(--status-color); font-size: 12px; }}
    .summary-status::before, .status-badge::before {{ content: ""; display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: currentColor; margin-right: 7px; vertical-align: middle; }}
    .table-wrap {{ overflow-x: auto; border: 1px solid var(--border); border-radius: 12px; background: var(--panel); scrollbar-color: #435164 var(--panel); scrollbar-width: thin; }}
    table {{ border-collapse: collapse; width: 100%; text-align: left; }}
    .table-wrap.is-empty thead {{ display: none; }}
    th, td {{ padding: 16px 20px; white-space: nowrap; }}
    thead th {{ background: #18222e; color: var(--muted); font-size: 11px; font-weight: 600; letter-spacing: 0.045em; text-transform: uppercase; }}
    tbody tr + tr {{ border-top: 1px solid var(--border); }}
    tbody tr:hover {{ background: #18222e; }}
    .client-id {{ font-weight: 600; white-space: normal; overflow-wrap: anywhere; min-width: 120px; max-width: 280px; }}
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
        <p class="subtitle">Connection health across your clients.</p>
      </div>
      <form method="post" action="/reset">
        <button type="submit" class="reset-button">Reset Clients</button>
      </form>
    </header>
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
        selectors.forEach((selector, index) => {{
          const current = document.querySelector(selector);
          const next = replacements[index];
          current.className = next.className;
          if (current.innerHTML !== next.innerHTML) {{
            current.replaceChildren(...next.childNodes);
          }}
        }});
        document.querySelector(".table-wrap").className = table.className;
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

    def get_clients_snapshot(self) -> list[dict[str, object]]:
        with self.lock:
            return [
                {
                    "client_id": client_id,
                    "address": client["address"],
                    "last_heartbeat": client["last_heartbeat"],
                    "max_gap_ms": client["max_gap_ms"],
                }
                for client_id, client in sorted(self.clients.items())
            ]

    def clear_clients(self) -> None:
        with self.lock:
            self.clients.clear()
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
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
