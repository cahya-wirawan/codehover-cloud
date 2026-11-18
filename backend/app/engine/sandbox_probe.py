"""Reports what the sandbox actually enforces; used by the worker's startup self-check and by tests."""

from __future__ import annotations

import json
import os
import resource
import socket
import sys


def _can_open(family: int) -> bool:
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
    except OSError:
        return False
    sock.close()
    return True


def main() -> int:
    report = {
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
