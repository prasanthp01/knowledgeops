"""Ground-truth questions are not indexed or supplied to the retrieval function."""

import hashlib
import json
import os
import platform
import resource
import statistics
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from knowledgeops.config import ROOT, Settings
from knowledgeops.db import Database
from knowledgeops.models import SearchInput
from knowledgeops.retrieval import retrieve


def run():
    settings = Settings()
    db = Database(settings.database_url)
    db.open()
    dataset = ROOT / "data/evaluation.json"
    cases = json.loads(dataset.read_text())
    results = []
    recalls, precisions = [], []
    citations_valid, citations_total, abstention_correct, abstention_total = 0, 0, 0, 0
    for i, case in enumerate(cases):
        query = SearchInput(
            question=case["question"], role=case["role"], revision=case.get("revision", "current")
        )
        with db.connection() as conn:
            output = retrieve(conn, query)
            actual = {c["id"] for c in output["citations"]}
            expected = set(case["relevant"])
            if expected:
                recalls.append(len(actual & expected) / len(expected))
                precisions.append(len(actual & expected) / max(len(actual), 1))
            if case.get("abstention"):
                abstention_total += 1
                abstention_correct += output["abstention"] == case["abstention"]
            for citation in output["citations"]:
                citations_total += 1
                chunk = conn.execute(
                    "SELECT body FROM chunks WHERE tenant_id='demo' AND document_id=%s AND version=%s AND chunk_id=%s",
                    (citation["document_id"], citation["version"], citation["chunk_id"]),
                ).fetchone()
                citations_valid += bool(chunk and citation["quote"] == chunk["body"])
        passed = expected <= actual and (
            not case.get("abstention") or output["abstention"] == case["abstention"]
        )
        results.append(
            {
                "case": i + 1,
                "question": case["question"],
                "role": case["role"],
                "expected": sorted(expected),
                "actual": sorted(actual),
                "abstention": output["abstention"],
                "pass": passed,
            }
        )
    report = {
        "name": "retrieval_evaluation",
        "status": "PASS" if all(r["pass"] for r in results) else "FAIL",
        "question_count": len(cases),
        "relevant_question_count": len(recalls),
        "recall_at_k_percent": round(statistics.mean(recalls) * 100, 2),
        "precision_at_k_percent": round(statistics.mean(precisions) * 100, 2),
        "citation_validity_percent": round(citations_valid / max(citations_total, 1) * 100, 2),
        "abstention_correct": abstention_correct,
        "abstention_total": abstention_total,
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "top_k": 3,
        "method": "Hand-authored synthetic questions held out from search corpus and runtime prompts. Same author as corpus; not an independent enterprise benchmark.",
        "cases": results,
    }
    queries = [SearchInput(question=c["question"], role=c["role"]) for c in cases[:20]]
    for query in queries:
        with db.connection() as conn:
            retrieve(conn, query)

    def timed(i):
        start = time.perf_counter()
        with db.connection() as conn:
            retrieve(conn, queries[i % len(queries)])
        return (time.perf_counter() - start) * 1000

    cpu_start = time.process_time()
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=4) as executor:
        durations = list(executor.map(timed, range(400)))
    duration = time.perf_counter() - start
    cpu_seconds = time.process_time() - cpu_start
    durations.sort()
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    benchmark = {
        "name": "retrieval_local",
        "sample_count": len(durations),
        "concurrency": 4,
        "p50_ms": round(statistics.median(durations), 3),
        "p95_ms": round(durations[int(len(durations) * 0.95) - 1], 3),
        "duration_seconds": round(duration, 3),
        "throughput_per_second": round(len(durations) / duration, 2),
        "warmup_requests": 20,
        "errors": 0,
        "cpu_seconds": round(cpu_seconds, 3),
        "peak_driver_rss_mib": round(
            rss / (1024 * 1024 if platform.system() == "Darwin" else 1024), 2
        ),
        "host": {
            "os": platform.system(),
            "architecture": platform.machine(),
            "logical_cpus": os.cpu_count(),
        },
        "scope": "Local PostgreSQL connection acquisition + permission-filtered retrieval + Python ranking. Excludes HTTP and queue; not sustained VPS capacity.",
    }
    db.close()
    try:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except subprocess.CalledProcessError:
        revision = "UNCOMMITTED"
    receipt = {
        "verified_at": datetime.now(UTC).isoformat(),
        "git_revision": revision,
        "evaluation": report,
        "benchmark": benchmark,
    }
    (ROOT / "docs/evidence/retrieval-evaluation.json").write_text(
        json.dumps(receipt, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "evaluation": {k: v for k, v in report.items() if k != "cases"},
                "benchmark": benchmark,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    run()
