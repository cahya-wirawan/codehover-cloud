"""`python -m app.ops.loadcheck`: synthetic load and SLO check against a published site (01 NFR-003/005,
12 "synthetic probe", 13 "performance").

Concurrent virtual readers walk a published site the way the viewer does: site info, tree, a file, a
definition card and searches. Each endpoint's p50/p95/max and error rate are reported, and the run fails
(exit 1) when a target is missed: reads and definition cards p95 under 500 ms (NFR-003), search p95
under 700 ms on the 20k-symbol fixture (NFR-005), and no errors. Readers pause 0.5-1.5 s between
actions, like people reading; `--think-ms 0 0` turns the run into a closed-loop stress test that finds
the throughput limit. `--users 1 --seconds 10` is the hourly synthetic probe; `--users 50` the beta load
check. Only the published site's public endpoints are
used, so it needs no credentials; point it at a synthetic fixture site only (12), never at tenant data.

    python -m app.ops.loadcheck --base https://example --site synthetic-probe --users 50 --seconds 60
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from collections import defaultdict
from typing import Any

import httpx

# A card fetched from the server is a read (NFR-003). NFR-005's "cached card opens <100 ms" is a card the
# viewer already holds in memory (TanStack Query), which never reaches the server and is not measured here.
TARGETS_MS = {"read": 500.0, "search": 700.0, "card": 500.0}
QUERIES = ("handler", "handler_1", "module", "value", "case", "handle")


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


async def reader(
    http: httpx.AsyncClient,
    site: str,
    files: list[str],
    symbols: list[str],
    stop: float,
    samples: dict[str, list[float]],
    errors: dict[str, int],
    think: tuple[float, float] = (0.0, 0.0),
) -> None:
    rng = random.Random()  # noqa: S311 (load pattern, not security)
    while time.monotonic() < stop:
        kind, path, params = rng.choice(
            [
                ("read", f"/api/v1/published/{site}", {}),
                ("read", f"/api/v1/published/{site}/tree", {"path": ""}),
                ("read", f"/api/v1/published/{site}/files/{rng.choice(files)}", {}),
                ("card", f"/api/v1/published/{site}/symbols/{rng.choice(symbols)}", {}),
                ("search", f"/api/v1/published/{site}/search", {"q": rng.choice(QUERIES)}),
            ]
        )
        started = time.perf_counter()
        try:
            response = await http.get(path, params=params)
            ok = response.status_code == 200
        except httpx.HTTPError:
            ok = False
        elapsed = (time.perf_counter() - started) * 1000
        samples[kind].append(elapsed)
        if not ok:
            errors[kind] += 1
        if think[1]:
            await asyncio.sleep(rng.uniform(*think) / 1000)  # a reader pauses between actions


async def discover(http: httpx.AsyncClient, site: str) -> tuple[list[str], list[str]]:
    files: list[str] = []
    pending = [""]
    while pending and len(files) < 500:
        listing = (await http.get(f"/api/v1/published/{site}/tree", params={"path": pending.pop()})).json()
        for entry in listing["items"]:
            if entry["type"] == "dir":
                pending.append(entry["path"])
            elif entry.get("status") == "selected":
                files.append(entry["file_id"])
    symbols: list[str] = []
    for file_id in files[:50]:
        view = (await http.get(f"/api/v1/published/{site}/files/{file_id}")).json()
        symbols.extend(s["id"] for s in view.get("symbols", []))
    if not files or not symbols:
        raise SystemExit("the site has no readable files or definitions")
    return files, symbols


async def run(
    base: str, site: str, users: int, seconds: float, think_ms: tuple[float, float] = (500.0, 1500.0)
) -> dict[str, Any]:
    limits = httpx.Limits(max_connections=users, max_keepalive_connections=users)
    async with httpx.AsyncClient(base_url=base, timeout=30, limits=limits) as http:
        files, symbols = await discover(http, site)
        for symbol in symbols[:20]:  # warm the card cache like a reader who already hovered once
            await http.get(f"/api/v1/published/{site}/symbols/{symbol}")
        samples: dict[str, list[float]] = defaultdict(list)
        errors: dict[str, int] = defaultdict(int)
        stop = time.monotonic() + seconds
        await asyncio.gather(
            *(reader(http, site, files, symbols, stop, samples, errors, think_ms) for _ in range(users))
        )
    report: dict[str, Any] = {
        "think_ms": list(think_ms),
        "users": users,
        "seconds": seconds,
        "files": len(files),
        "symbols_sampled": len(symbols),
    }
    failed = []
    for kind, target in TARGETS_MS.items():
        values = samples[kind]
        p95 = percentile(values, 0.95)
        report[kind] = {
            "requests": len(values),
            "errors": errors[kind],
            "p50_ms": round(statistics.median(values), 1) if values else 0,
            "p95_ms": round(p95, 1),
            "max_ms": round(max(values), 1) if values else 0,
            "target_p95_ms": target,
        }
        if errors[kind] or p95 > target or not values:
            failed.append(kind)
    total = sum(len(v) for v in samples.values())
    report["throughput_rps"] = round(total / seconds, 1)
    report["passed"] = not failed
    report["failed"] = failed
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", required=True)
    parser.add_argument("--site", required=True)
    parser.add_argument("--users", type=int, default=50)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument(
        "--think-ms",
        type=float,
        nargs=2,
        default=(500.0, 1500.0),
        help="pause between a reader's requests (beta users read; 0 0 is a closed-loop stress test)",
    )
    args = parser.parse_args()
    report = asyncio.run(run(args.base, args.site, args.users, args.seconds, tuple(args.think_ms)))
    print(json.dumps(report, indent=1))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
