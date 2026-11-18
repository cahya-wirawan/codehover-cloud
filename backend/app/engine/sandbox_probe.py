"""Reports what the sandbox actually enforces; used by the worker's startup self-check and by tests."""

from __future__ import annotations

import json
import os
import resource
import socket
import sys
from pathlib import Path


def _can_open(family: int) -> bool:
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
    except OSError:
        return False
    sock.close()
    return True


def _can_read(path: str) -> bool:
    try:
        with Path(path).open("rb") as handle:
            handle.read(1)
    except OSError:
        return False
    return True


def _can_exec() -> bool:
    try:
        pid = os.fork()
    except OSError:
        return False
    if pid == 0:  # child: try to run a program; exit 0 only if exec somehow succeeded
        try:
            os.execv(sys.executable, [sys.executable, "-c", "pass"])  # noqa: S606 - probing that exec is blocked
        finally:
            os._exit(99)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status) == 0


def main() -> int:
    report = {
        "parent_environ": _can_read(f"/proc/{os.getppid()}/environ"),
        "etc": _can_read("/etc/hostname") or _can_read("/etc/passwd"),
        "secrets": _can_read("/run/secrets/github_app_key"),
        "exec": _can_exec(),
        "isolated": os.environ.get("CODEHOVER_SANDBOX_ISOLATED") == "1",
        "inet": _can_open(socket.AF_INET),
        "inet6": _can_open(socket.AF_INET6),
        "unix": _can_open(socket.AF_UNIX),
        "environment": sorted(k for k in os.environ if k != "CODEHOVER_SANDBOX_ISOLATED"),
        "cpu_limit": resource.getrlimit(resource.RLIMIT_CPU)[0],
        "nofile_limit": resource.getrlimit(resource.RLIMIT_NOFILE)[0],
    }
    sys.stdout.write(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
