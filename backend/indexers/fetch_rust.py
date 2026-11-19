"""Install rust-analyzer and Rust's library sources for the Rust SCIP indexer (T23), checked by SHA-256.

    python3 fetch_rust.py <destination>

writes `<destination>/bin/rust-analyzer`, `<destination>/bin/VERSION` (the release name the worker records)
`<destination>/library` (rust-src's standard library sources, which rust-analyzer reads to resolve
std types) and `<destination>/licenses`. Both come from the Rust project's own release hosts, pinned here by release and digest, and
were published before the dependency cutoff (backend/pyproject.toml `exclude-newer`). Nothing else is
installed: no toolchain, no cargo, no proc-macro server. Used by deployment/Dockerfile.backend and CI.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import lzma
import platform
import sys
import tarfile
import urllib.request
from pathlib import Path, PurePosixPath

RUST_ANALYZER = "2026-11-02"
ASSETS = {
    "x86_64": (
        "rust-analyzer-x86_64-unknown-linux-gnu.gz",
        "5249da68c3f36bb77ea7d401824f5e8bd8d0a47a162daf5f72208eb7c1bf1500",
    ),
    "aarch64": (
        "rust-analyzer-aarch64-unknown-linux-gnu.gz",
        "4f8699eaee4231df5a5ffd477106ecae3bf6328918a60de742798912ecff6869",
    ),
}
RUST_SRC_URL = "https://static.rust-lang.org/dist/2026-10-01/rust-src-1.99.0.tar.xz"
RUST_SRC_SHA256 = "3f1f9b7ed48f4596fc87889b7b3c61747336a55c9c22db1ab0c697e0aadb77aa"
LIBRARY = "rust-src-1.99.0/rust-src/lib/rustlib/src/rust/library/"
# The Rust project's licence texts (MIT or Apache-2.0), which cover rust-analyzer too.
LICENSES = frozenset(
    {"rust-src-1.99.0/COPYRIGHT", "rust-src-1.99.0/LICENSE-APACHE", "rust-src-1.99.0/LICENSE-MIT"}
)
MAX_BYTES = 200 * 1024**2


def _download(url: str, sha256: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - fixed https URLs
        data: bytes = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise SystemExit(f"{url}: larger than expected")
    digest = hashlib.sha256(data).hexdigest()
    if digest != sha256:
        raise SystemExit(f"{url}: sha256 {digest} does not match the pinned {sha256}")
    return data


def main(destination: str) -> int:
    machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(platform.machine().lower(), platform.machine())
    if machine not in ASSETS:
        raise SystemExit(f"no rust-analyzer build pinned for {machine}")
    asset, sha256 = ASSETS[machine]
    url = f"https://github.com/rust-lang/rust-analyzer/releases/download/{RUST_ANALYZER}/{asset}"
    root = Path(destination)
    (root / "bin").mkdir(parents=True, exist_ok=True)
    binary = root / "bin" / "rust-analyzer"
    binary.write_bytes(gzip.decompress(_download(url, sha256)))
    binary.chmod(0o755)
    (root / "bin" / "VERSION").write_text(RUST_ANALYZER + "\n")  # read by the worker, beside the binary

    library = root / "library"
    with tarfile.open(
        fileobj=io.BytesIO(lzma.decompress(_download(RUST_SRC_URL, RUST_SRC_SHA256)))
    ) as archive:
        for member in archive.getmembers():
            if member.name in LICENSES and member.isfile():
                member.name = PurePosixPath(member.name).name
                archive.extract(member, root / "licenses", filter="data")
                continue
            if not member.name.startswith(LIBRARY) or not (member.isfile() or member.isdir()):
                continue
            member.name = member.name[len(LIBRARY) :]
            if member.name:
                archive.extract(member, library, filter="data")  # refuses absolute paths and `..`
    if not (library / "core" / "src" / "lib.rs").is_file():
        raise SystemExit("rust-src did not contain the standard library sources")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
