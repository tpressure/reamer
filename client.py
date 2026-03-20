#!/usr/bin/env python3

import argparse
import json
import socket
import time
from datetime import datetime, timezone

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 12345
DEFAULT_INTERVAL = 5.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simple heartbeat TCP client")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Server host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Server port")
    parser.add_argument(
        "--client-id",
        default=socket.gethostname(),
        help="Identifier sent with each heartbeat",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help="Seconds between heartbeat messages",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    with socket.create_connection((args.host, args.port)) as sock:
        print(f"Connected to {args.host}:{args.port} as {args.client_id}")

        while True:
            payload = {
                "type": "heartbeat",
                "client_id": args.client_id,
                "sent_at": datetime.now(timezone.utc).isoformat(),
            }
            sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            print(f"Heartbeat sent at {payload['sent_at']}")
            time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nClient stopped.")
