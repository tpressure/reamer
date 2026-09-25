#!/usr/bin/env python3

import argparse
import json
import os
import signal
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 12345
DEFAULT_INTERVAL = 5.0
DEFAULT_RETRY_DELAY = 2.0
STRESS_KINDS = ("cpu", "memory")


def available_vcpus() -> int:
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


class StressManager:
    """Own stress processes across server disconnects; never execute supplied commands."""

    def __init__(self) -> None:
        self.processes = {}
        self.states = {kind: {"state": "idle", "last_command": "", "error": ""} for kind in STRESS_KINDS}

    @staticmethod
    def command(kind: str) -> list[str]:
        cpus = available_vcpus()
        if kind == "cpu":
            return ["stress-ng", "--cpu", str(cpus), "--cpu-load", "100", "--timeout", "0"]
        if kind != "memory":
            raise ValueError("Unknown stress test")
        workers = max(1, cpus // 2)
        fields = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        total_kib = int(fields["MemTotal"].split()[0])
        per_worker_mib = total_kib // (2 * workers * 1024)
        if per_worker_mib < 1:
            raise ValueError("Not enough memory for the requested test")
        return ["memtouch", "--num_threads", str(workers), "--thread_mem", str(per_worker_mib), "--rw_ratio", "50"]

    def snapshot(self) -> dict:
        for kind, process in list(self.processes.items()):
            code = process.poll()
            if code is not None:
                # Clean up any remaining workers if their supervisor exited.
                self.signal_group(process, signal.SIGKILL)
                del self.processes[kind]
                self.states[kind].update(state="idle" if code == 0 else "error", error="" if code == 0 else f"Process exited with code {code}")
        return {kind: dict(state) for kind, state in self.states.items()}

    @staticmethod
    def signal_group(process: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass

    def stop(self, kind: str) -> None:
        process = self.processes.get(kind)
        if process is not None:
            self.signal_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.signal_group(process, signal.SIGKILL)
                process.wait(timeout=2)
            # A supervisor may exit before all of its workers do.
            self.signal_group(process, signal.SIGKILL)
            del self.processes[kind]
        self.states[kind].update(state="idle", error="")

    def apply(self, command: object) -> None:
        if not isinstance(command, dict):
            return
        kind, action, command_id = (command.get(key) for key in ("kind", "action", "id"))
        if kind not in STRESS_KINDS or action not in ("start", "stop") or not isinstance(command_id, str) or not command_id:
            return
        if self.states[kind]["last_command"] == command_id:
            return
        self.snapshot()
        try:
            if action == "stop":
                self.stop(kind)
            elif kind not in self.processes:
                process = subprocess.Popen(self.command(kind), start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
                self.processes[kind] = process
                self.states[kind].update(state="running", error="")
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
            self.states[kind].update(state="running" if kind in self.processes else "error", error=str(exc)[:200])
        self.states[kind]["last_command"] = command_id

    def close(self) -> None:
        for kind in STRESS_KINDS:
            self.stop(kind)


class SystemMetrics:
    """Sample Linux host usage without adding runtime dependencies."""

    def __init__(self, proc_root: Path = Path("/proc")) -> None:
        self.proc_root = proc_root
        self.previous_cpu = None

    def sample(self) -> dict[str, float | int | None]:
        cpu_percent = None
        memory_percent = None
        memory_total_bytes = None
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
            if total > 0:
                memory_total_bytes = total * 1024
            available = memory["MemAvailable"]
            if total > 0 and 0 <= available <= total:
                memory_percent = round(100 * (total - available) / total, 1)
        except (OSError, ValueError, KeyError, IndexError):
            pass

        return {
            "cpu_percent": cpu_percent,
            "memory_percent": memory_percent,
            "vcpu_count": available_vcpus(),
            "memory_total_bytes": memory_total_bytes,
        }


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

    if args.interval <= 0 or args.retry_delay < 0:
        raise SystemExit("Heartbeat interval must be positive and retry delay non-negative.")
    stress = StressManager()
    try:
        while True:
            try:
                with socket.create_connection((args.host, args.port), timeout=10) as sock:
                    sock.settimeout(max(10, args.interval * 3))
                    print(f"Connected to {args.host}:{args.port} as {args.client_id}", flush=True)
                    metrics = SystemMetrics()
                    with sock.makefile("r", encoding="utf-8") as reader:
                        while True:
                            payload = {
                                "type": "heartbeat",
                                "client_id": args.client_id,
                                "sent_at": datetime.now(timezone.utc).isoformat(),
                                "metrics": metrics.sample(),
                                "control_protocol": 1,
                                "stress": stress.snapshot(),
                            }
                            sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
                            reply = reader.readline(65537)
                            if not reply or len(reply) > 65536:
                                raise ConnectionError("Server closed the connection or sent an oversized reply")
                            response = json.loads(reply)
                            if not isinstance(response, dict) or response.get("type") != "heartbeat_ack":
                                raise ValueError("Invalid heartbeat acknowledgement")
                            commands = response.get("commands", [])
                            if isinstance(commands, list):
                                for command in commands[:2]:
                                    stress.apply(command)
                            time.sleep(args.interval)
            except (OSError, ValueError) as exc:
                print(f"Connection to {args.host}:{args.port} failed: {exc}. Retrying in {args.retry_delay} seconds.", flush=True)
                time.sleep(args.retry_delay)
    finally:
        stress.close()


if __name__ == "__main__":
    def terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    try:
        main()
    except KeyboardInterrupt:
        print("\nClient stopped.")
