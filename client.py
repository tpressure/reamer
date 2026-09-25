#!/usr/bin/env python3

import argparse
import json
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 12345
DEFAULT_INTERVAL = 5.0
DEFAULT_RETRY_DELAY = 2.0


class SystemMetrics:
    """Sample Linux host usage without adding runtime dependencies."""

    def __init__(self, proc_root: Path = Path("/proc")) -> None:
        self.proc_root = proc_root
        self.previous_cpu = None

    def sample(self) -> dict[str, float | None]:
        cpu_percent = None
        memory_percent = None
        try:
            with (self.proc_root / "stat").open() as stat:
                fields = stat.readline().split()
            if fields[0] != "cpu" or len(fields) < 5:
                raise ValueError("Missing aggregate CPU counters")
            # Guest counters are already included in user/nice; do not count twice.
            counters = [int(value) for value in fields[1:9]]
            total = sum(counters)
            idle = counters[3] + (counters[4] if len(counters) > 4 else 0)
            if self.previous_cpu is not None:
                previous_total, previous_idle = self.previous_cpu
                elapsed = total - previous_total
                idle_elapsed = idle - previous_idle
                if elapsed > 0 and 0 <= idle_elapsed <= elapsed:
                    cpu_percent = round(100 * (elapsed - idle_elapsed) / elapsed, 1)
            self.previous_cpu = (total, idle)
        except (OSError, ValueError, IndexError):
            self.previous_cpu = None

        try:
            memory = {}
            for line in (self.proc_root / "meminfo").read_text().splitlines():
                key, value = line.split(":", 1)
                if key in ("MemTotal", "MemAvailable"):
                    memory[key] = int(value.split()[0])
            total = memory["MemTotal"]
            available = memory["MemAvailable"]
            if total > 0 and 0 <= available <= total:
                memory_percent = round(100 * (total - available) / total, 1)
        except (OSError, ValueError, KeyError, IndexError):
            pass

        return {"cpu_percent": cpu_percent, "memory_percent": memory_percent}


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
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=DEFAULT_RETRY_DELAY,
        help="Seconds to wait before retrying after a connection failure",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    while True:
        try:
            with socket.create_connection((args.host, args.port)) as sock:
                print(f"Connected to {args.host}:{args.port} as {args.client_id}")
                metrics = SystemMetrics()

                while True:
                    payload = {
                        "type": "heartbeat",
                        "client_id": args.client_id,
                        "sent_at": datetime.now(timezone.utc).isoformat(),
                        "metrics": metrics.sample(),
                    }
                    sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
                    print(f"Heartbeat sent at {payload['sent_at']}")
                    time.sleep(args.interval)
        except OSError as exc:
            print(
                f"Connection to {args.host}:{args.port} failed: {exc}. "
                f"Retrying in {args.retry_delay} seconds."
            )
            time.sleep(args.retry_delay)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nClient stopped.")
