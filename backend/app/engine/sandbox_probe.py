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


def _can_exec(program: str, *args: str) -> bool:
    read, write = os.pipe()  # the program's output must not mix into this report on stdout
    try:
        pid = os.fork()
    except OSError:
        os.close(read)
        os.close(write)
        return False
    if pid == 0:  # child: try to run a program; exit 0 only if exec somehow succeeded
        try:
            os.close(read)
            os.dup2(write, 1)
            os.dup2(write, 2)
            os.execv(program, [program, *args])  # noqa: S606 - probing which programs may run
        finally:
            os._exit(99)
    os.close(write)
    with os.fdopen(read, "rb") as output:
        while output.read(65536):
            pass
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status) == 0


def _can_write(directory: str) -> bool:
    target = Path(directory) / ".codehover-probe"
    try:
        target.write_bytes(b"x")
    except OSError:
        return False
    target.unlink(missing_ok=True)
    return True


def main() -> int:
    request = json.loads(sys.stdin.read() or "{}")  # indexer profile: {"program", "args", "work"}
    report = {
        "profile": os.environ.get("CODEHOVER_SANDBOX_PROFILE", "parser"),
        "parent_environ": _can_read(f"/proc/{os.getppid()}/environ"),
        "etc": _can_read("/etc/hostname") or _can_read("/etc/passwd"),
        "secrets": _can_read("/run/secrets/github_app_key"),
        "exec": _can_exec(sys.executable, "-c", "pass") or _can_exec("/bin/sh", "-c", "true"),
        "write_code": _can_write(str(Path.cwd())),
        "write_tmp": _can_write("/tmp"),  # noqa: S108 - probing that /tmp is not writable
        "isolated": os.environ.get("CODEHOVER_SANDBOX_ISOLATED") == "1",
        "inet": _can_open(socket.AF_INET),
        "inet6": _can_open(socket.AF_INET6),
        "unix": _can_open(socket.AF_UNIX),
        "environment": sorted(k for k in os.environ if not k.startswith("CODEHOVER_SANDBOX_")),
        "cpu_limit": resource.getrlimit(resource.RLIMIT_CPU)[0],
        "nofile_limit": resource.getrlimit(resource.RLIMIT_NOFILE)[0],
    }
    if request.get("program"):  # the indexer's own program, with the worker's probe arguments
        report["exec_program"] = _can_exec(request["program"], *map(str, request.get("args", [])))
    if request.get("work"):
        report["write_out"] = _can_write(str(Path(request["work"]) / "out"))
        report["write_work"] = _can_write(request["work"])
    sys.stdout.write(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
