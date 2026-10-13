import copy
import json
import secrets
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from knowledgeops import contracts as wire
from knowledgeops.config import ROOT
from knowledgeops.synthetic_peer import PROFILE_ID, route_synthetic
from tests.test_contracts_v1 import credentials, query, v1, v1app, v1settings  # noqa: F401

IDS = {
    "operation_id": "synthetic-op:1",
    "correlation_id": "synthetic-correlation:1",
    "trace_id": "synthetic-trace:1",
}
CANARY = "SYNTHETIC_UNTRUSTED_PEER_SECRET"


@pytest.fixture
def profile(settings, tmp_path):
    value = {
        "profile_id": PROFILE_ID,
        "enabled": True,
        "base_url": "http://127.0.0.1:8058/api/projects/agentops",
        "bearer": secrets.token_urlsafe(32),
        "application_id": "knowledgeops",
        "audience": "agentops",
        "environment": "development",
        "tenant_id": "demo",
        "scopes": ["inference:request"],
        "synthetic_only": True,
        "allow_loopback_http": True,
        "routing_policy_version": "policy-2026-10-v1",
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    path = tmp_path / "synthetic-profile.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return settings.model_copy(update={"agentops_synthetic_profile_file": str(path)}), path, value


def decision(request):
    value = json.loads((ROOT / "tests/fixtures/contracts-v1/valid/route-decision.json").read_text())
    value.update(
        {
            k: request[k]
            for k in [
                "operation_id",
                "correlation_id",
                "trace_id",
                "routing_policy_version",
                "mode",
            ]
        }
    )
    value.update(
        outcome="SUCCEEDED",
        rejected_candidates=[],
        fallback_kind="NONE",
        selected_deployment={
            "deployment_id": "cedar-eu-fixture",
            "model_version": "synthetic-catalog-1.0.0",
            "provider": "Cedar",
            "region": "eu",
            "capabilities": ["TEXT", "STRUCTURED_OUTPUT"],
        },
    )
    usage = value["usage"]
    usage.update(input_tokens=1024, output_tokens=128)
    usage["cost"]["value"] = 0.0001
    value["attempts"] = [
        {
            "deployment_id": "cedar-eu-fixture",
            "attempt": 1,
            "outcome": "SUCCEEDED",
            "duration_ms": 100,
            "fallback_reason": None,
            "usage": copy.deepcopy(usage),
        }
    ]
    return value


def test_synthetic_profile_emits_only_admitted_empty_context(profile):
    settings, _, config = profile
    calls = []

    def service(request):
        body = json.loads(request.content)
        calls.append(body)
        wire.REGISTRY.validate("inference-request", body)
        assert request.headers["authorization"] == "Bearer " + config["bearer"]
        assert request.url.path == "/api/projects/agentops/inference"
        assert body["application_id"] == "knowledgeops" and body["environment"] == "development"
        assert body["required_capabilities"] == ["TEXT", "STRUCTURED_OUTPUT"]
        assert body["task_type"] == "extract" and body["risk_class"] == "LOW"
        assert body["context_refs"] == [] and body["extensions"] == {}
        assert body["mode"] == "SANDBOX_DETERMINISTIC" and body["currency"] == "USD"
        assert not any(k in body for k in ["question", "passages", "prompt", "content"])
        return httpx.Response(200, json=decision(body))

    outcome = route_synthetic(settings, **IDS, transport=httpx.MockTransport(service))
    assert outcome.status == "ROUTED" and outcome.error is None
    wire.REGISTRY.validate("route-decision", outcome.decision)
    assert len(calls) == 1
    assert config["bearer"] not in json.dumps(asdict(outcome))


@pytest.mark.parametrize(
    "change",
    [
        {"enabled": False},
        {"environment": "production"},
        {"tenant_id": "private"},
        {"application_id": "flowops"},
        {"audience": "other-service"},
        {"synthetic_only": False},
        {"scopes": []},
        {"mode": "LIVE_MODEL_AUTHENTICATED"},
        {"context_refs": [CANARY]},
        {"allow_loopback_http": False},
        {"base_url": "http://169.254.169.254/inference"},
        {"base_url": "http://127.0.0.1:8058/private/content"},
        {"expires_at": "2020-01-01T00:00:00Z"},
        {"routing_policy_version": "expired-policy"},
    ],
)
def test_invalid_or_disabled_profiles_never_call_peer(profile, change):
    settings, path, value = profile
    path.write_text(json.dumps({**value, **change}))

    def no_call(_):
        pytest.fail("Invalid profile sent a request")

    outcome = route_synthetic(settings, **IDS, transport=httpx.MockTransport(no_call))
    assert outcome.status == "UNAVAILABLE" and outcome.remote_state == "NOT_CALLED"
    assert outcome.fallback == "DETERMINISTIC_RETRIEVAL" and outcome.decision is None
    wire.REGISTRY.validate("error", outcome.error)
    assert CANARY not in json.dumps(asdict(outcome))


def test_profile_missing_permissions_and_revocation(profile):
    settings, path, _ = profile
    path.chmod(0o644)
    assert route_synthetic(settings, **IDS).remote_state == "NOT_CALLED"
    path.unlink()
    assert route_synthetic(settings, **IDS).remote_state == "NOT_CALLED"
    assert (
        route_synthetic(
            settings.model_copy(update={"agentops_synthetic_profile_file": ""}), **IDS
        ).status
        == "UNAVAILABLE"
    )


@pytest.mark.parametrize(
    "fault",
    [
        "timeout",
        "malformed",
        "oversize",
        "redirect",
        "error-reflection",
        "wrong-operation",
        "wrong-mode",
        "paid",
        "region",
        "capability",
        "over-budget",
        "aggregate",
        "no-successful-attempt",
    ],
)
def test_faults_return_typed_fallback_without_retry_or_downgrade(profile, fault):
    settings, _, _ = profile
    calls = []

    def service(request):
        calls.append(request)
        if fault == "timeout":
            raise httpx.ReadTimeout(CANARY)
        if fault == "malformed":
            return httpx.Response(200, content=CANARY, headers={"content-type": "application/json"})
        if fault == "oversize":
            return httpx.Response(
                200, content="x" * 262145, headers={"content-type": "application/json"}
            )
        if fault == "redirect":
            return httpx.Response(302, headers={"Location": "https://example.invalid/" + CANARY})
        if fault == "error-reflection":
            error = wire.error_payload("FORBIDDEN", "synthetic-error")
            error["message"] = CANARY
            return httpx.Response(403, json=error)
        value = decision(json.loads(request.content))
        if fault == "wrong-operation":
            value["operation_id"] = CANARY
        elif fault == "wrong-mode":
            value["mode"] = "LIVE_MODEL_AUTHENTICATED"
        elif fault == "paid":
            value["attempts"][0]["usage"]["provider_calls"] = 1
        elif fault == "region":
            value["selected_deployment"]["region"] = "us"
        elif fault == "capability":
            value["selected_deployment"]["capabilities"] = ["TEXT"]
        elif fault == "over-budget":
            value["usage"]["cost"]["value"] = 1
        elif fault == "aggregate":
            value["usage"]["input_tokens"] += 1
        elif fault == "no-successful-attempt":
            value["attempts"][0]["outcome"] = "TIMEOUT"
        return httpx.Response(200, json=value)

    outcome = route_synthetic(settings, **IDS, transport=httpx.MockTransport(service))
    assert len(calls) == 1
    assert outcome.status == "UNAVAILABLE" and outcome.remote_state == "UNKNOWN"
    assert outcome.decision is None and outcome.fallback == "DETERMINISTIC_RETRIEVAL"
    wire.REGISTRY.validate("error", outcome.error)
    assert CANARY not in json.dumps(asdict(outcome))


@pytest.mark.parametrize("status", ["AI_UNAVAILABLE", "POLICY_BLOCKED", "BUDGET_EXCEEDED"])
def test_canonical_negative_decisions_preserve_modeled_accounting(profile, status):
    settings, _, _ = profile

    def service(request):
        value = decision(json.loads(request.content))
        value.update(outcome=status, selected_deployment=None, fallback_kind="HUMAN_REVIEW")
        value["attempts"][0]["outcome"] = "TIMEOUT"
        return httpx.Response(200, json=value)

    outcome = route_synthetic(settings, **IDS, transport=httpx.MockTransport(service))
    assert outcome.status == "UNAVAILABLE" and outcome.remote_state == "DECISION_RECEIVED"
    assert outcome.decision["usage"]["cost"]["value"] == 0.0001
    assert outcome.error["code"] == status


@pytest.mark.parametrize(
    "revision,threshold,days", [("2026-01", "10000", "30"), ("2026-02", "5000", "14")]
)
def test_flowops_positive_revision_specific_fixtures(v1, v1settings, revision, threshold, days):  # noqa: F811
    auth = credentials(v1settings, application_id="flowops", collection_ids=["operations"])
    request = query(
        application_id="flowops",
        query="Synthetic refund policy " + revision,
        collection_ids=["operations"],
        max_excerpt_chars=300,
    )
    response = v1.post("/knowledge/query", json=request, headers=auth)
    assert response.status_code == 200
    value = response.json()
    wire.REGISTRY.validate("knowledge-response", value)
    assert value["status"] == "OK" and len(value["citations"]) == 1
    citation = value["citations"][0]
    assert citation["source_id"] == "flowops-refund-" + revision
    assert threshold + " minor" in citation["passage"] and days + " days" in citation["passage"]
    assert "not a refund approval" in citation["passage"]
    assert len(citation["passage"]) <= 300


@pytest.mark.parametrize("revision", ["unknown", "2026-03", "2026-0"])
def test_flowops_unknown_revision_abstains(v1, v1settings, revision):  # noqa: F811
    auth = credentials(v1settings, application_id="flowops", collection_ids=["operations"])
    response = v1.post(
        "/knowledge/query",
        json=query(
            application_id="flowops",
            query="Synthetic refund policy " + revision,
            collection_ids=["operations"],
        ),
        headers=auth,
    )
    assert response.json()["status"] == "NO_RESULTS"
    assert response.json()["answer"] is None and response.json()["citations"] == []


def test_refund_fixture_grants_and_document_revocation_still_apply(v1, v1settings, db):  # noqa: F811
    auth = credentials(v1settings, application_id="flowops", collection_ids=["people"])
    request = query(
        application_id="flowops",
        query="Synthetic refund policy 2026-01",
        collection_ids=["operations"],
    )
    assert v1.post("/knowledge/query", json=request, headers=auth).json()["status"] == "DENIED"
    auth = credentials(v1settings, application_id="flowops", collection_ids=["operations"])
    with db.connection() as conn:
        conn.execute(
            "UPDATE documents SET lifecycle='revoked' WHERE document_id='flowops-refund-2026-01'"
        )
    value = v1.post("/knowledge/query", json=request, headers=auth).json()
    assert value["status"] == "NO_RESULTS" and value["citations"] == []


def test_existing_held_out_corpus_expectations(db):
    from knowledgeops.models import SearchInput
    from knowledgeops.retrieval import retrieve

    cases = json.loads((ROOT / "data/evaluation.json").read_text())
    for case in cases:
        with db.connection() as conn:
            result = retrieve(
                conn,
                SearchInput(
                    question=case["question"],
                    role=case["role"],
                    revision=case.get("revision", "current"),
                ),
            )
        assert set(case["relevant"]) <= {c["id"] for c in result["citations"]}
        if case.get("abstention"):
            assert result["abstention"] == case["abstention"]
