"""Run engine code in the sandbox (app.engine.sandbox_exec) from the worker.

The child gets no inherited environment (so no database URLs, tokens or keys), a fixed working
directory with the read-only code, input on stdin and bounded output on stdout, and a wall-clock
timeout on top of its CPU rlimit.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TIMEOUT_SECONDS = 180
MAX_OUTPUT_BYTES = 64 * 1024 * 1024


class SandboxError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def run(module: str, payload: dict[str, Any], *, allow_unsandboxed: bool = False) -> dict[str, Any]:
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    if allow_unsandboxed and sys.platform != "linux":
        env["CODEHOVER_ALLOW_UNSANDBOXED"] = "1"  # development machines only; never on Linux
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-E",
        "-s",
        "-m",
        "app.engine.sandbox_exec",
        module,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=BACKEND_ROOT,
        env=env,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(
            process.communicate(json.dumps(payload).encode()), timeout=TIMEOUT_SECONDS
        )
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise SandboxError("timeout") from exc
    if process.returncode != 0:
        raise SandboxError(f"exit_{process.returncode}")
    if len(stdout) > MAX_OUTPUT_BYTES:
        raise SandboxError("output_too_large")
    try:
        result: dict[str, Any] = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise SandboxError("invalid_output") from exc
    return result


async def self_check(*, allow_unsandboxed: bool = False) -> dict[str, Any]:
    """Raise unless the sandbox blocks network sockets (required on Linux)."""
    report = await run("app.engine.sandbox_probe", {}, allow_unsandboxed=allow_unsandboxed)
    if report.get("inet") or report.get("inet6") or (sys.platform == "linux" and not report.get("isolated")):
        raise SandboxError("network_not_isolated")
    if sys.platform == "linux" and any(report.get(k) for k in ("parent_environ", "etc", "secrets", "exec")):
        raise SandboxError("filesystem_or_process_not_isolated")
    return report
