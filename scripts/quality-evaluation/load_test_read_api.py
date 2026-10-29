from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


async def run(args: argparse.Namespace) -> dict:
    parsed = urlparse(args.base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        if not args.allow_remote:
            raise SystemExit("Remote load requires the explicit --allow-remote flag.")
    origin = args.base_url.rstrip("/")
    async with httpx.AsyncClient(
        base_url=origin,
        timeout=httpx.Timeout(args.timeout_seconds),
        limits=httpx.Limits(
            max_connections=args.concurrency,
            max_keepalive_connections=args.concurrency,
        ),
    ) as client:
        auth = await client.post("/api/v1/auth/demo")
        if auth.status_code != 200:
            raise SystemExit(f"Demo authentication failed with HTTP {auth.status_code}.")
        token = auth.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        latencies: list[float] = []
        statuses: Counter[str] = Counter()
        errors: Counter[str] = Counter()
        next_index = 0
        lock = asyncio.Lock()
        started = time.perf_counter()

        async def worker() -> None:
            nonlocal next_index
            while True:
                async with lock:
                    index = next_index
                    next_index += 1
                if index >= args.requests:
                    return
                due = started + index / args.rate
                delay = due - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
                request_started = time.perf_counter()
                try:
                    response = await client.get(args.path, headers=headers)
                    statuses[str(response.status_code)] += 1
                except httpx.TimeoutException:
                    errors["timeout"] += 1
                except httpx.HTTPError as exc:
                    errors[type(exc).__name__] += 1
                finally:
                    latencies.append(time.perf_counter() - request_started)

        await asyncio.gather(*(worker() for _ in range(args.concurrency)))
        elapsed = time.perf_counter() - started
    return {
        "started_at": datetime.now(UTC).isoformat(),
        "base_url": origin,
        "path": args.path,
        "requests_target": args.requests,
        "offered_rate_rps": args.rate,
        "concurrency": args.concurrency,
        "elapsed_seconds": round(elapsed, 3),
        "completed_requests": len(latencies),
        "actual_throughput_rps": round(len(latencies) / elapsed, 2) if elapsed else 0,
        "http_status_counts": dict(statuses),
        "transport_error_counts": dict(errors),
        "latency_ms": {
            "p50": round(statistics.median(latencies) * 1000, 2) if latencies else None,
            "p95": round(percentile(latencies, 0.95) * 1000, 2)
            if latencies
            else None,
            "p99": round(percentile(latencies, 0.99) * 1000, 2)
            if latencies
            else None,
            "max": round(max(latencies) * 1000, 2) if latencies else None,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rate-controlled read-only load test for the authenticated briefing API."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--path", default="/api/v1/briefings?limit=30")
    parser.add_argument("--requests", type=int, default=3000)
    parser.add_argument("--rate", type=float, default=100)
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--timeout-seconds", type=float, default=10)
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.requests < 1 or args.rate <= 0 or not 1 <= args.concurrency <= 2000:
        parser.error("requests/rate must be positive and concurrency must be 1..2000")
    report = asyncio.run(run(args))
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return int(bool(report["transport_error_counts"]) or report["http_status_counts"].get("200") != args.requests)


if __name__ == "__main__":
    raise SystemExit(main())
