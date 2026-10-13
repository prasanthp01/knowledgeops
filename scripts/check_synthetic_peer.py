"""Exercise KnowledgeOps's fixed synthetic caller against a real local AgentOps service."""

import argparse
import json
import subprocess
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from knowledgeops.config import Settings
from knowledgeops.synthetic_peer import read_profile, route_synthetic, synthetic_request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--agentops-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    profile = read_profile(args.profile)
    if profile is None or profile.base_url != "http://127.0.0.1:8058/api/projects/agentops":
        raise SystemExit("This probe requires the isolated AgentOps loopback profile on port 8058")
    settings = Settings(agentops_synthetic_profile_file=str(args.profile))
    ids = {
        "operation_id": "knowledgeops-probe:" + str(uuid.uuid4()),
        "correlation_id": "knowledgeops-probe-correlation",
        "trace_id": "knowledgeops-probe-trace",
    }
    first = route_synthetic(settings, **ids)
    assert first.status == "ROUTED", asdict(first)
    repeated = route_synthetic(settings, **ids)
    assert repeated.decision == first.decision
    assert first.decision["usage"]["kind"] == "SIMULATED"
    assert first.decision["usage"]["provider_calls"] == 0
    # Disabled profile is a local typed unavailable result with no peer call.
    disabled = route_synthetic(Settings(agentops_synthetic_profile_file=""), **ids)
    assert disabled.status == "UNAVAILABLE" and disabled.remote_state == "NOT_CALLED"
    assert (
        disabled.error["code"] == "AI_UNAVAILABLE"
        and disabled.fallback == "DETERMINISTIC_RETRIEVAL"
    )
    report = {
        "status": "PASS",
        "executed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "agentops_source_revision": (args.agentops_source / "source-revision.txt")
        .read_text()
        .strip(),
        "scope": "REAL_KNOWLEDGEOPS_CALLER_TO_AGENTOPS_SYNTHETIC_HTTP",
        "request": synthetic_request(profile, **ids),
        "response": asdict(first),
        "identical_operation_reconciles_same_decision": True,
        "disabled_fallback": asdict(disabled),
        "default_workflow_connected": False,
        "private_content_transmitted": False,
        "live_or_paid_inference_enabled": False,
        "production_modified": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        "KnowledgeOps caller to AgentOps: canonical synthetic route, duplicate reconciliation, zero provider calls and disabled fallback PASS."
    )


if __name__ == "__main__":
    main()
