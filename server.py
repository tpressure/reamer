#!/usr/bin/env python3

import argparse
import json
import socket
import threading
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone

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

        class StatusHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path != "/":
                    self.send_error(404, "Not Found")
                    return

                body = server.render_status_page().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        http_server = ThreadingHTTPServer((self.host, self.http_port), StatusHandler)
        thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        thread.start()
        print(f"HTTP status page enabled on http://{self.host}:{self.http_port}/")

    def render_status_page(self) -> str:
        clients = self.get_clients_snapshot()
        threshold_summary = (
            f"Green: <= {self.healthy_threshold_ms} ms, "
            f"Yellow: <= {self.warning_threshold_ms} ms, "
            f"Red: > {self.warning_threshold_ms} ms"
        )

        rows = []
        for client in clients:
            age_ms = self.get_heartbeat_age_ms(client["last_heartbeat"])
            status = self.get_client_status(age_ms)
            max_gap_ms = max(client["max_gap_ms"], age_ms)
            rows.append(
                "<tr>"
                f"<td><span class=\"status-dot {status['css_class']}\"></span>{escape(status['label'])}</td>"
                f"<td>{escape(client['client_id'])}</td>"
                f"<td>{escape(client['address'])}</td>"
                f"<td>{escape(self.format_timestamp(client['last_heartbeat']))}</td>"
                f"<td>{age_ms} ms</td>"
                f"<td>{max_gap_ms} ms</td>"
                "</tr>"
            )

        if not rows:
            rows.append('<tr><td colspan="6">No heartbeats received yet.</td></tr>')

        table_rows = "\n".join(rows)

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Heartbeat Status</title>
  <meta http-equiv="refresh" content="1">
  <style>
    body {{ font-family: sans-serif; margin: 2rem; }}
    table {{ border-collapse: collapse; width: 100%; max-width: 56rem; }}
    th, td {{ border: 1px solid #ccc; padding: 0.75rem; text-align: left; }}
    th {{ background: #f3f3f3; }}
    .status-dot {{ display: inline-block; width: 0.85rem; height: 0.85rem; border-radius: 50%; margin-right: 0.5rem; vertical-align: middle; }}
    .status-healthy {{ background: #2f9e44; }}
    .status-warning {{ background: #f08c00; }}
    .status-stale {{ background: #e03131; }}
    .thresholds {{ margin-bottom: 1rem; color: #444; }}
  </style>
</head>
<body>
  <h1>Heartbeat Status</h1>
  <p class="thresholds">{escape(threshold_summary)}</p>
  <table>
    <thead>
      <tr>
        <th>Status</th>
        <th>Client ID</th>
        <th>Address</th>
        <th>Last Heartbeat</th>
        <th>Age</th>
        <th>Max Gap</th>
      </tr>
    </thead>
    <tbody>
      {table_rows}
    </tbody>
  </table>
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
