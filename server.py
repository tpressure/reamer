#!/usr/bin/env python3

import argparse
import json
import socket
import threading
from datetime import datetime, timezone

DEFAULT_PORT = 12345


class HeartbeatServer:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.clients = {}
        self.lock = threading.Lock()

    def serve_forever(self) -> None:
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
            last_seen = client["last_heartbeat"].astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
            print(
                f"- {client_id} ({client['address']}) last heartbeat: {last_seen}"
            )
        print()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simple heartbeat TCP server")
    parser.add_argument("--host", default="0.0.0.0", help="Host/interface to bind to")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to listen on")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    server = HeartbeatServer(args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
