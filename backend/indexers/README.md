# SCIP indexers (T23)

The worker runs these (and rust-analyzer, below) in the sandbox's `indexer` profile (`app/engine/sandbox_exec.py`, spec 22) to resolve references by type. The backend image installs them in a Node stage and copies only `node`, its license and the installed packages.

| Package | Version | License |
|---|---|---|
| `@sourcegraph/scip-python` | 0.6.8 | MIT |
| `@sourcegraph/scip-typescript` | 0.6.0 | Apache-2.0 |
| Node.js (image `node:24.22.3-slim`) | 24.22.3 | MIT and bundled licenses (`/opt/codehover/node/LICENSE`) |

**License review (2026-11-19).** The lockfile holds 64 packages:
- 47 MIT, 9 ISC, 4 Apache-2.0 and 2 BSD-3-Clause;
- 2 `google-protobuf` (BSD-3-Clause AND Apache-2.0).

All are permissive. No package needs an install script: the only lifecycle scripts are `prepare`, which npm never runs for registry installs. Install scripts are disabled anyway.

**Updating.**
1. Move `before` in `.npmrc` forward deliberately.
2. Change the exact versions in `package.json` and run `npm install --package-lock-only --ignore-scripts` here.
3. Review the licenses of everything new in `package-lock.json`.
4. Bump the parser bundle version (`app/engine/__init__.py`, spec 23).
5. Run the Linux tests in `tests/unit/test_scip.py` with `SCIP_NODE_PATH` and `SCIP_TOOLS_DIR` set.

`node_modules/` is never committed.

## Rust

`fetch_rust.py` installs Rust's indexer. The backend image runs it in its own stage, and so does CI. It downloads two files, both checked against SHA-256 digests pinned in the script:

| Download | Version | License |
|---|---|---|
| `rust-analyzer` (Linux GNU build, GitHub release of rust-lang/rust-analyzer) | 2026-11-02 | MIT OR Apache-2.0 |
| Rust's library sources (`rust-src` from static.rust-lang.org) | 1.99.0 (2026-10-01) | MIT OR Apache-2.0 (`/opt/codehover/rust/licenses`) |

Both were published before the dependency cutoff. Nothing else is installed: no toolchain, no cargo and no proc-macro server.

**How a Rust repository is indexed.** rust-analyzer never sees a `Cargo.toml`. The sandboxed worker (`app/engine/rust_project.py`) turns the repository's manifests into a `rust-project.json`:
- crates follow Cargo's target rules;
- path and workspace dependencies between the repository's own packages are linked;
- every declared feature and `cfg(test)` are enabled.

Two consequences:
- **Nothing of the repository runs.** No build script, proc macro or cargo command runs. rust-analyzer's own attempts to run `rustc` and `cargo` are refused by the sandbox.
- **crates.io dependencies are absent.** References into them stay unresolved.

**Updating.**
1. Choose releases published before the cutoff.
2. Change `RUST_ANALYZER`, `ASSETS` and the `rust-src` URL and digest in `fetch_rust.py`. Release digests come from the GitHub API (`assets[].digest`); the `xz_hash` of `rust-src` comes from `channel-rust-<version>.toml`.
3. Bump the parser bundle version.
4. Run `tests/unit/test_rust.py` on Linux with `SCIP_RUST_ANALYZER_PATH` and `SCIP_RUST_SRC_DIR` set.
