"""Real FlowOps PeerClient to KnowledgeOps. Run with the pinned FlowOps Python environment."""

import argparse
import copy
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flowops-source", type=Path, required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--tokens", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    url = urlsplit(args.origin)
    if url.scheme != "http" or url.hostname != "127.0.0.1" or url.port != 8047:
        raise SystemExit("Only the isolated KnowledgeOps loopback service is permitted")
    sys.path.insert(0, str(args.flowops_source.resolve()))
    from flowops import interop
    from flowops.db import Run, now

    assert Path(interop.__file__).resolve().is_relative_to(args.flowops_source.resolve())
    token = json.loads(args.tokens.read_text())["flowops_staff"]
    client = interop.PeerClient(
        args.origin + "/api/projects/knowledgeops", token, allow_local_test=True
    )
    run = Run(
        id="knowledgeops-peer-fixture",
        environment="local",
        status="RUNNING",
        deadline_at=now() + timedelta(seconds=120),
        input={"policy_revision": "2026-01", "currency": "EUR"},
        context={
            "contracts_v1": {"tenant_id": "demo", "client_correlation_id": "refund-fixture-check"}
        },
    )
    grant = interop.PeerGrant(
        run.id, "demo", frozenset({"knowledge:read"}), frozenset({"operations"})
    )
    results = {}
    for revision in ["2026-01", "2026-02", "unknown"]:
        run.input = {"policy_revision": revision, "currency": "EUR"}
        response = client.knowledge(run, grant, ["operations"])
        if revision == "unknown":
            assert (
                response["status"] == "NO_RESULTS"
                and response["answer"] is None
                and response["citations"] == []
            )
        else:
            # The canonical success status is OK, not ANSWERED.
            assert response["status"] == "OK" and len(response["citations"]) == 1
            citation = response["citations"][0]
            assert citation["source_id"] == "flowops-refund-" + revision
            assert (
                hashlib.sha256(citation["passage"].encode()).hexdigest()
                == citation["evidence"]["sha256"]
            )
            assert len(citation["passage"]) <= 300
        results[revision] = response
    wrong = copy.copy(run)
    wrong.context = {
        "contracts_v1": {
            "tenant_id": "another-tenant",
            "client_correlation_id": "refund-denied-check",
        }
    }
    wrong_grant = interop.PeerGrant(
        run.id, "another-tenant", frozenset({"knowledge:read"}), frozenset({"operations"})
    )
    denied = client.knowledge(wrong, wrong_grant, ["operations"])
    assert denied["status"] == "DENIED" and denied["answer"] is None and denied["citations"] == []
    assert "refund" not in json.dumps(denied["limitations"])
    results["wrong-tenant"] = denied
    try:
        client.knowledge(run, interop.PeerGrant(run.id, "demo", frozenset()), ["operations"])
    except PermissionError:
        denied_locally = True
    else:
        raise AssertionError("Peer caller bypassed scope")
    import httpx

    health = httpx.get(args.origin + "/health", timeout=3).json()
    report = {
        "status": "PASS",
        "executed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source_revision": health["source_revision"],
        "flowops_source_revision": (args.flowops_source / "source-revision.txt")
        .read_text()
        .strip(),
        "scope": "REAL_PEER_LIBRARY_TO_SERVICE_WITH_SYNTHETIC_UNPERSISTED_CONTEXT",
        "default_workflow_connected": False,
        "production_modified": False,
        "local_scope_denial": denied_locally,
        "responses": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        "FlowOps PeerClient: two positive exact-revision lookups, unknown revision, wrong tenant and missing scope PASS."
    )


if __name__ == "__main__":
    main()
