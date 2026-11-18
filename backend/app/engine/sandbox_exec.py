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
from pathlib import Path

ALLOWED_MODULES = frozenset({"app.engine.parse_worker", "app.engine.sandbox_probe"})
LIMITS = {
    resource.RLIMIT_CPU: 120,
    resource.RLIMIT_AS: 2 * 1024**3,
    resource.RLIMIT_FSIZE: 16 * 1024**2,
    resource.RLIMIT_NOFILE: 64,
    resource.RLIMIT_CORE: 0,
}

# AUDIT_ARCH and the syscalls the parser never needs: network (socket, io_uring), inspecting or
# controlling other processes (ptrace, process_vm_readv/writev) and running programs (execve, execveat).
ARCHES = {
    "x86_64": (
        0xC000003E,
        {
            "socket": 41,
            "io_uring_setup": 425,
            "io_uring_enter": 426,
            "io_uring_register": 427,
            "ptrace": 101,
            "process_vm_readv": 310,
            "process_vm_writev": 311,
            "execve": 59,
            "execveat": 322,
        },
    ),
    "aarch64": (
        0xC00000B7,
        {
            "socket": 198,
            "io_uring_setup": 425,
            "io_uring_enter": 426,
            "io_uring_register": 427,
            "ptrace": 117,
            "process_vm_readv": 270,
            "process_vm_writev": 271,
            "execve": 221,
            "execveat": 281,
        },
    ),
}
PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2
RET_ALLOW = 0x7FFF0000
RET_KILL_PROCESS = 0x80000000
RET_ERRNO = 0x00050000
X32_SYSCALL_BIT = 0x40000000

# Landlock (Linux >= 5.13): syscall numbers are the same on x86_64 and aarch64.
LANDLOCK_CREATE_RULESET, LANDLOCK_ADD_RULE, LANDLOCK_RESTRICT_SELF = 444, 445, 446
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
FS_EXECUTE, FS_READ_FILE, FS_READ_DIR = 1, 4, 8
FS_ALL_V1 = (1 << 13) - 1  # every filesystem right of ABI v1: nothing is allowed unless granted below


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter))]


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _program(arch: int, denied: list[int]) -> list[tuple[int, int, int, int]]:
    ld, jeq, jge, ret = 0x20, 0x15, 0x35, 0x06
    # Layout: 0-4 header (arch check, kill, load nr, x32 check), 5..4+n denied checks, 5+n ALLOW, 6+n DENY.
    deny_index = 6 + len(denied)
    program = [
        (ld, 0, 0, 4),  # A = seccomp_data.arch
        (jeq, 1, 0, arch),  # native ABI? skip the kill
        (ret, 0, 0, RET_KILL_PROCESS),
        (ld, 0, 0, 0),  # A = syscall number
        (jge, deny_index - 5, 0, X32_SYSCALL_BIT),  # x32 -> deny
    ]
    for position, number in enumerate(denied):
        here = 5 + position
        program.append((jeq, deny_index - here - 1, 0, number))
    program.append((ret, 0, 0, RET_ALLOW))
    program.append((ret, 0, 0, RET_ERRNO | errno.EACCES))
    return program


def install_seccomp() -> None:
    arch = ARCHES.get(platform.machine())
    if arch is None:
        raise RuntimeError(f"no seccomp profile for {platform.machine()}")
    instructions = _program(arch[0], sorted(arch[1].values()))
    filters = (_SockFilter * len(instructions))(*[_SockFilter(c, t, f, k) for c, t, f, k in instructions])
    program = _SockFprog(len(instructions), filters)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS failed")
    if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(program), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "seccomp filter failed")


def readable_roots() -> list[str]:
    """Code and runtime the parser needs. Notably absent: /run (secrets), /proc, /etc, /tmp, /home."""
    import site
    import sysconfig

    candidates = {
        sys.prefix,
        sys.base_prefix,
        sys.exec_prefix,
        *site.getsitepackages(),
        sysconfig.get_paths()["stdlib"],
        str(Path.cwd()),
        "/lib",
        "/lib64",
        "/usr/lib",
        "/usr/lib64",
    }
    return sorted(path for path in candidates if path and Path(path).is_dir())


def install_landlock(roots: list[str]) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    syscall = libc.syscall
    syscall.restype = ctypes.c_long
    if syscall(LANDLOCK_CREATE_RULESET, None, 0, LANDLOCK_CREATE_RULESET_VERSION) < 1:
        raise OSError(ctypes.get_errno(), "landlock unavailable")
    attr = _RulesetAttr(FS_ALL_V1)
    ruleset = syscall(LANDLOCK_CREATE_RULESET, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if ruleset < 0:
        raise OSError(ctypes.get_errno(), "landlock ruleset failed")
    try:
        for root in roots:
            fd = os.open(root, getattr(os, "O_PATH", 0) | os.O_CLOEXEC)  # O_PATH is Linux-only
            try:
                rule = _PathBeneathAttr(FS_READ_FILE | FS_READ_DIR | FS_EXECUTE, fd)
                if (
                    syscall(LANDLOCK_ADD_RULE, ruleset, LANDLOCK_RULE_PATH_BENEATH, ctypes.byref(rule), 0)
                    != 0
                ):
                    raise OSError(ctypes.get_errno(), f"landlock rule failed for {root}")
            finally:
                os.close(fd)
        if syscall(LANDLOCK_RESTRICT_SELF, ruleset, 0) != 0:
            raise OSError(ctypes.get_errno(), "landlock restrict failed")
    finally:
        os.close(ruleset)


def lock_down() -> bool:
    """Returns True when network isolation is enforced."""
    for limit, value in LIMITS.items():
        if limit == resource.RLIMIT_AS and sys.platform != "linux":
            continue  # macOS cannot lower the address-space limit; production (Linux) always applies it
        _soft, hard = resource.getrlimit(limit)
        target = value if hard == resource.RLIM_INFINITY else min(value, hard)
        resource.setrlimit(limit, (target, target))
    if sys.platform == "linux":
        roots = readable_roots()  # computed before the filesystem is locked
        install_seccomp()  # also sets PR_SET_NO_NEW_PRIVS, which Landlock requires
        install_landlock(roots)
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
