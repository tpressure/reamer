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


class HeartbeatServer:
    def __init__(
        self,
        host: str,
        port: int,
        enable_http: bool = False,
        http_port: int = DEFAULT_HTTP_PORT,
    ) -> None:
        self.host = host
        self.port = port
        self.enable_http = enable_http
        self.http_port = http_port
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
                    self.clients[client_id] = {
                        "address": peer,
                        "last_heartbeat": timestamp,
                    }
                    self.print_clients_locked()

        print(f"Client disconnected: {peer}")

    def print_clients_locked(self) -> None:
        print("\nKnown clients:")
        for client_id in sorted(self.clients):
            client = self.clients[client_id]
            print(f"- {client_id} ({client['address']}) last heartbeat: {self.format_timestamp(client['last_heartbeat'])}")
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

        rows = []
        for client in clients:
            rows.append(
                "<tr>"
                f"<td>{escape(client['client_id'])}</td>"
                f"<td>{escape(client['address'])}</td>"
                f"<td>{escape(self.format_timestamp(client['last_heartbeat']))}</td>"
                "</tr>"
            )

        if not rows:
            rows.append('<tr><td colspan="3">No heartbeats received yet.</td></tr>')

        table_rows = "\n".join(rows)

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Heartbeat Status</title>
  <style>
    body {{ font-family: sans-serif; margin: 2rem; }}
    table {{ border-collapse: collapse; width: 100%; max-width: 56rem; }}
    th, td {{ border: 1px solid #ccc; padding: 0.75rem; text-align: left; }}
    th {{ background: #f3f3f3; }}
  </style>
</head>
<body>
  <h1>Heartbeat Status</h1>
  <table>
    <thead>
      <tr>
        <th>Client ID</th>
        <th>Address</th>
        <th>Last Heartbeat</th>
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
                }
                for client_id, client in sorted(self.clients.items())
            ]

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
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    server = HeartbeatServer(
        args.host,
        args.port,
        enable_http=args.enable_http,
        http_port=args.http_port,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
