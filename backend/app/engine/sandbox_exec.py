"""Sandbox bootstrap: lock this process down, then run an allowlisted engine module (22 "Sandbox gate").

Launched by app.engine.sandbox with an empty environment and stdin/stdout pipes. Before any parser code
is imported it applies rlimits, PR_SET_NO_NEW_PRIVS and a seccomp BPF filter that refuses socket
creation and io_uring (no network, whatever the parser does), kills the process on a foreign syscall
ABI and refuses x32 syscalls. If the filter cannot be installed on Linux the process exits (fail closed).
On non-Linux development machines the parent must explicitly allow running unsandboxed.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import resource
import runpy
import sys

ALLOWED_MODULES = frozenset({"app.engine.parse_worker", "app.engine.sandbox_probe"})
LIMITS = {
    resource.RLIMIT_CPU: 120,
    resource.RLIMIT_AS: 2 * 1024**3,
    resource.RLIMIT_FSIZE: 16 * 1024**2,
    resource.RLIMIT_NOFILE: 64,
    resource.RLIMIT_CORE: 0,
}

# (AUDIT_ARCH, socket, io_uring_setup, io_uring_enter, io_uring_register)
ARCHES = {
    "x86_64": (0xC000003E, 41, 425, 426, 427),
    "aarch64": (0xC00000B7, 198, 425, 426, 427),
}
PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2
RET_ALLOW = 0x7FFF0000
RET_KILL_PROCESS = 0x80000000
RET_ERRNO = 0x00050000
X32_SYSCALL_BIT = 0x40000000


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter))]


def _program(arch: int, socket_nr: int, uring: tuple[int, int, int]) -> list[tuple[int, int, int, int]]:
    ld, jeq, jge, ret = 0x20, 0x15, 0x35, 0x06
    deny = RET_ERRNO | errno.EACCES
    return [
        (ld, 0, 0, 4),  # 0: A = seccomp_data.arch
        (jeq, 1, 0, arch),  # 1: native ABI? skip the kill
        (ret, 0, 0, RET_KILL_PROCESS),  # 2
        (ld, 0, 0, 0),  # 3: A = syscall number
        (jge, 5, 0, X32_SYSCALL_BIT),  # 4: x32 -> deny (index 10)
        (jeq, 4, 0, socket_nr),  # 5: socket -> deny
        (jeq, 3, 0, uring[0]),  # 6: io_uring_setup -> deny
        (jeq, 2, 0, uring[1]),  # 7: io_uring_enter -> deny
        (jeq, 1, 0, uring[2]),  # 8: io_uring_register -> deny
        (ret, 0, 0, RET_ALLOW),  # 9
        (ret, 0, 0, deny),  # 10
    ]


def install_seccomp() -> None:
    arch = ARCHES.get(platform.machine())
    if arch is None:
        raise RuntimeError(f"no seccomp profile for {platform.machine()}")
    instructions = _program(arch[0], arch[1], arch[2:])
    filters = (_SockFilter * len(instructions))(*[_SockFilter(c, t, f, k) for c, t, f, k in instructions])
    program = _SockFprog(len(instructions), filters)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS failed")
    if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(program), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "seccomp filter failed")


def lock_down() -> bool:
    """Returns True when network isolation is enforced."""
    for limit, value in LIMITS.items():
        if limit == resource.RLIMIT_AS and sys.platform != "linux":
            continue  # macOS cannot lower the address-space limit; production (Linux) always applies it
        _soft, hard = resource.getrlimit(limit)
        target = value if hard == resource.RLIM_INFINITY else min(value, hard)
        resource.setrlimit(limit, (target, target))
    if sys.platform == "linux":
        install_seccomp()
        return True
    if os.environ.get("CODEHOVER_ALLOW_UNSANDBOXED") != "1":
        raise RuntimeError("network isolation unavailable on this platform")
    return False


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ALLOWED_MODULES:
        sys.stderr.write("module not allowed\n")
        return 2
    try:
        isolated = lock_down()
    except Exception as exc:  # fail closed
        sys.stderr.write(f"sandbox setup failed: {type(exc).__name__}\n")
        return 3
    os.environ.clear()
    os.environ["CODEHOVER_SANDBOX_ISOLATED"] = "1" if isolated else "0"
    sys.argv = [sys.argv[1]]
    runpy.run_module(sys.argv[0], run_name="__main__", alter_sys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
