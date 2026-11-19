# SCIP indexers (T23)

The worker runs these in the sandbox's `indexer` profile (`app/engine/sandbox_exec.py`, spec 22) to resolve references by type. The backend image installs them in a Node stage and copies only `node`, its license and the installed packages.

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
