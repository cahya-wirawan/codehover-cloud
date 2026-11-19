"""Sandbox bootstrap: lock this process down, then run an allowlisted engine module (22 "Sandbox gate").

Launched by app.engine.sandbox with an empty environment and stdin/stdout pipes. Before any parser code
is imported it applies rlimits, PR_SET_NO_NEW_PRIVS and a seccomp BPF filter that refuses socket
creation and io_uring (no network, whatever the parser does), kills the process on a foreign syscall
ABI and refuses x32 syscalls. If the filter cannot be installed on Linux the process exits (fail closed).
On non-Linux development machines the parent must explicitly allow running unsandboxed.

Two profiles share the bootstrap. `parser` (the default) runs Python only: running programs is refused
and the filesystem is read-only Python, libraries and backend code. `indexer` runs the SCIP indexers
(app.engine.index_worker): it may start exactly one program, the pinned Node binary (Landlock grants
execute on that file and its ELF loader only), reads the indexer packages and the build's scratch
checkout, and writes only the checkout's `out` directory. The network stays refused in both.
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

ALLOWED_MODULES = frozenset(
    {"app.engine.parse_worker", "app.engine.sandbox_probe", "app.engine.index_worker"}
)
PROFILE_MODULES = {
    "parser": frozenset({"app.engine.parse_worker", "app.engine.sandbox_probe"}),
    "indexer": frozenset({"app.engine.index_worker", "app.engine.sandbox_probe"}),
}
LIMITS = {
    resource.RLIMIT_CPU: 120,
    resource.RLIMIT_AS: 2 * 1024**3,
    resource.RLIMIT_FSIZE: 16 * 1024**2,
    resource.RLIMIT_NOFILE: 64,
    resource.RLIMIT_CORE: 0,
}
# Node reserves far more address space than it uses (V8 code ranges and cages), so the indexer's real
# memory bound is the V8 heap flag plus the worker container's memory limit; RLIMIT_AS only stops runaways.
INDEXER_LIMITS = {
    resource.RLIMIT_CPU: 600,
    resource.RLIMIT_AS: 16 * 1024**3,
    resource.RLIMIT_FSIZE: 256 * 1024**2,
    resource.RLIMIT_NOFILE: 512,
    resource.RLIMIT_CORE: 0,
}
EXEC_SYSCALLS = frozenset({"execve", "execveat"})

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
FS_EXECUTE, FS_WRITE_FILE, FS_READ_FILE, FS_READ_DIR = 1, 2, 4, 8
FS_REMOVE_FILE, FS_MAKE_REG = 32, 256
FS_ALL_V1 = (1 << 13) - 1  # every filesystem right of ABI v1: nothing is allowed unless granted below
READ_EXEC = FS_READ_FILE | FS_READ_DIR | FS_EXECUTE
READ = FS_READ_FILE | FS_READ_DIR
READ_WRITE = READ | FS_WRITE_FILE | FS_MAKE_REG | FS_REMOVE_FILE
EXEC_FILE = FS_EXECUTE | FS_READ_FILE  # rules on a regular file accept file rights only


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


def install_seccomp(*, allow_exec: bool = False) -> None:
    arch = ARCHES.get(platform.machine())
    if arch is None:
        raise RuntimeError(f"no seccomp profile for {platform.machine()}")
    denied = sorted(nr for name, nr in arch[1].items() if not (allow_exec and name in EXEC_SYSCALLS))
    instructions = _program(arch[0], denied)
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


def elf_interpreter(binary: str) -> str | None:
    """The PT_INTERP path of a 64-bit little-endian ELF executable (its dynamic loader), if it has one."""
    with Path(binary).open("rb") as handle:
        header = handle.read(64)
        if len(header) < 64 or header[:4] != b"\x7fELF" or header[4] != 2 or header[5] != 1:
            return None
        phoff = int.from_bytes(header[32:40], "little")
        phentsize = int.from_bytes(header[54:56], "little")
        phnum = int.from_bytes(header[56:58], "little")
        for index in range(min(phnum, 64)):
            handle.seek(phoff + index * phentsize)
            entry = handle.read(56)
            if len(entry) < 56 or int.from_bytes(entry[0:4], "little") != 3:  # PT_INTERP
                continue
            offset = int.from_bytes(entry[8:16], "little")
            size = int.from_bytes(entry[32:40], "little")
            if not 0 < size <= 4096:
                return None
            handle.seek(offset)
            return handle.read(size).split(b"\0", 1)[0].decode()
    return None


def indexer_rules(node: str, tools: str, work: str) -> list[tuple[str, int]]:
    """Landlock grants of the indexer profile: read the runtimes, execute only Node and its loader, read
    the indexer packages and the checkout, write only `<work>/out`."""
    node_path = os.path.realpath(node)
    rules = [(root, READ) for root in readable_roots()]
    rules.append((node_path, EXEC_FILE))
    interpreter = elf_interpreter(node_path)
    if interpreter is not None:
        rules.append((os.path.realpath(interpreter), EXEC_FILE))
    rules += [(tools, READ), (work, READ), (str(Path(work) / "out"), READ_WRITE)]
    return rules


def install_landlock(roots: list[str] | list[tuple[str, int]]) -> None:
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
        for entry in roots:
            root, rights = (entry, READ_EXEC) if isinstance(entry, str) else entry
            fd = os.open(root, getattr(os, "O_PATH", 0) | os.O_CLOEXEC)  # O_PATH is Linux-only
            try:
                rule = _PathBeneathAttr(rights, fd)
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


def lock_down(profile: str = "parser") -> bool:
    """Returns True when network isolation is enforced."""
    indexer = profile == "indexer"
    if indexer:  # paths come from the parent (app.engine.sandbox), never from repository content
        node, tools, work = (
            os.environ.get(k, "") for k in ("CODEHOVER_NODE", "CODEHOVER_TOOLS", "CODEHOVER_WORK")
        )
        if not all(Path(path).is_absolute() for path in (node, tools, work)):
            raise RuntimeError("indexer paths missing")
    for limit, value in (INDEXER_LIMITS if indexer else LIMITS).items():
        if limit == resource.RLIMIT_AS and sys.platform != "linux":
            continue  # macOS cannot lower the address-space limit; production (Linux) always applies it
        _soft, hard = resource.getrlimit(limit)
        target = value if hard == resource.RLIM_INFINITY else min(value, hard)
        resource.setrlimit(limit, (target, target))
    if sys.platform == "linux":
        # Computed before the filesystem is locked.
        rules = indexer_rules(node, tools, work) if indexer else readable_roots()
        install_seccomp(allow_exec=indexer)  # also sets PR_SET_NO_NEW_PRIVS, which Landlock requires
        install_landlock(rules)
        return True
    if os.environ.get("CODEHOVER_ALLOW_UNSANDBOXED") != "1":
        raise RuntimeError("network isolation unavailable on this platform")
    return False


def main() -> int:
    profile = os.environ.get("CODEHOVER_SANDBOX_PROFILE", "parser")
    if (
        len(sys.argv) != 2
        or sys.argv[1] not in ALLOWED_MODULES
        or sys.argv[1] not in PROFILE_MODULES.get(profile, ())
    ):
        sys.stderr.write("module not allowed\n")
        return 2
    try:
        isolated = lock_down(profile)
    except Exception as exc:  # fail closed
        sys.stderr.write(f"sandbox setup failed: {type(exc).__name__}\n")
        return 3
    os.environ.clear()
    os.environ["CODEHOVER_SANDBOX_ISOLATED"] = "1" if isolated else "0"
    os.environ["CODEHOVER_SANDBOX_PROFILE"] = profile
    sys.argv = [sys.argv[1]]
    runpy.run_module(sys.argv[0], run_name="__main__", alter_sys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
