"""Bounded synthetic end-to-end HTTP probe: 4 sessions x 8 runs, no provider calls."""

import argparse
import json
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import httpx

from knowledgeops.config import ROOT


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", default="http://127.0.0.1:8045")
    args = parser.parse_args()
    if args.origin not in {"http://127.0.0.1:8045", "http://127.0.0.1:8044"}:
        raise SystemExit("This benchmark is intentionally restricted to local preview ports")
    prefix = "/api/projects/knowledgeops"
    queries = [
        ("hotel allowance domestic travel", "staff"),
        ("treasury reserve target", "finance"),
        ("first response production incident", "engineering"),
        ("retain application logs", "staff"),
    ]

    def client_work(number):
        durations = []
        with httpx.Client(
            base_url=args.origin, headers={"Origin": args.origin}, timeout=10
        ) as client:
            session = client.post(prefix + "/session", json={})
            session.raise_for_status()
            client.headers["X-CSRF-Token"] = session.json()["csrf_token"]
            question, role = queries[number]
            for _ in range(8):
                started = time.perf_counter()
                accepted = client.post(
                    prefix + "/simulations",
                    json={"input": {"question": question, "role": role}},
                    headers={"Idempotency-Key": str(uuid.uuid4())},
                )
                accepted.raise_for_status()
                run_id = accepted.json()["run_id"]
                for _ in range(200):
                    result = client.get(prefix + f"/simulations/{run_id}/result")
                    if result.status_code == 200:
                        break
                    if result.status_code != 409:
                        result.raise_for_status()
                    time.sleep(0.05)
                result.raise_for_status()
                assert result.json()["output"]["answer"]
                durations.append((time.perf_counter() - started) * 1000)
        return durations

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(client_work, range(4)))
    duration = time.perf_counter() - started
    values = sorted(v for result in results for v in result)
    receipt = {
        "name": "http_workflow_local",
        "status": "PASS",
        "verified_at": datetime.now(UTC).isoformat(),
        "sample_count": len(values),
        "concurrency": 4,
        "warmup_requests": 0,
        "errors": 0,
        "p50_ms": round(statistics.median(values), 2),
        "p95_ms": round(values[int(len(values) * 0.95) - 1], 2),
        "duration_seconds": round(duration, 3),
        "throughput_per_second": round(len(values) / duration, 2),
        "scope": "Local HTTP submission through durable queue to successful cited result, including 50ms polling granularity; 4 sessions x 8 runs; not sustainable VPS capacity.",
        "provider_calls": 0,
    }
    (ROOT / "docs/evidence/http-benchmark.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    run()
