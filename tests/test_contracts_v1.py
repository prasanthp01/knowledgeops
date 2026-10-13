import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from knowledgeops import contracts as wire
from knowledgeops.config import ROOT
from knowledgeops.contracts_api import create_app, digest
from knowledgeops.jobs import claim, fail, process
from knowledgeops.vendor_contracts.validation import ContractError, validate_stream
from knowledgeops.worker import tick

SOURCE = "1" * 40  # Explicit fixture revision; live HTTP checks use the actual Git SHA.
CANARY = "SYNTHETIC_REVIEW_SECRET_CANARY_123456"
FIXTURES = ROOT / "tests/fixtures/contracts-v1"


@pytest.mark.parametrize("name", sorted(p.stem for p in (FIXTURES / "valid").glob("*.json")))
def test_frozen_valid_and_invalid_fixtures(name):
    wire.REGISTRY.validate(name, json.loads((FIXTURES / "valid" / f"{name}.json").read_text()))
    with pytest.raises(ContractError):
        wire.REGISTRY.validate(
            name, json.loads((FIXTURES / "invalid" / f"{name}.json").read_text())
        )


def test_vendor_hashes():
    lock = json.loads((ROOT / "contracts.lock.json").read_text())
    assert lock["release_commit"] == "b55d1a9dc5d2bab23817b984b9801ccff7304f04"
    for path, expected in lock["files"].items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected, path


@pytest.fixture
def v1settings(settings, tmp_path):
    return settings.model_copy(
        update={
            "contracts_source_revision": SOURCE,
            "knowledge_credentials_file": str(tmp_path / "credentials.json"),
        }
    )


@pytest.fixture
def v1app(v1settings, db):
    return create_app(v1settings, db)


@pytest.fixture
def v1(v1app):
    with TestClient(v1app, headers={"Origin": "http://testserver"}) as client:
        response = client.post("/simulation/session", json={})
        assert response.status_code == 201
        client.headers["Authorization"] = "Bearer " + response.json()["session_token"]
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        yield client


def payload(question="What is the hotel allowance for domestic travel?", role="staff"):
    return {
        "schema_version": "1.0",
        "project_id": "knowledgeops",
        "mode": "SANDBOX_DETERMINISTIC",
        "seed": 42,
        "client_correlation_id": "test:canonical.1",
        "input": {"question": question, "role": role},
    }


def start(client, body=None, key=None):
    response = client.post(
        "/simulations",
        json=body or payload(),
        headers={"Idempotency-Key": key or str(uuid.uuid4())},
    )
    assert response.status_code == 202, response.text
    wire.REGISTRY.validate("simulation-run", response.json())
    return response.json()["run_id"]


def completed(client, db, settings, body=None):
    run_id = start(client, body)
    assert tick(db, settings)
    response = client.get(f"/simulations/{run_id}/result")
    assert response.status_code == 200, response.text
    wire.REGISTRY.validate("simulation-result", response.json())
    return run_id, response.json()


def assert_error(response, code):
    wire.REGISTRY.validate("error", response.json())
    assert response.json()["code"] == code, response.text
    assert response.status_code == response.json()["http_status"]
    assert CANARY not in response.text


def test_metadata_prefix_digest_openapi(v1):
    for route, name in [
        ("/identity", "project-identity"),
        ("/simulation/schema", "simulation-schema"),
        ("/simulation/presets", "simulation-presets"),
    ]:
        value = v1.get(route).json()
        wire.REGISTRY.validate(name, value)
        assert v1.get(wire.PREFIX + route).json() == value
    schema = v1.get("/simulation/schema").json()
    assert (
        hashlib.sha256(
            json.dumps(
                schema["input_schema"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        == schema["input_schema_sha256"]
    )
    assert "seed" not in schema["input_schema"]["properties"]
    openapi = v1.get("/openapi.json").json()
    assert "/knowledge/query" in openapi["paths"]
    assert "/inference" not in openapi["paths"] and "/events" not in openapi["paths"]


def test_real_jobs_repeatability_changed_role_and_seed(v1, db, v1settings):
    question = "What is the treasury reserve target?"
    _, denied = completed(v1, db, v1settings, payload(question, "staff"))
    _, allowed = completed(v1, db, v1settings, payload(question, "finance"))
    _, repeat = completed(v1, db, v1settings, payload(question, "finance"))
    assert denied["output"]["citations"] == []
    assert denied["output"]["abstention"] == "NOT_ENOUGH_EVIDENCE"
    assert allowed["output"]["citations"]
    assert allowed["output"] == repeat["output"]
    assert allowed["provenance"]["source_revision"] == SOURCE
    assert allowed["usage"]["provider_calls"] == 0 and allowed["cost"]["kind"] == "NONE"
    high_seed = payload()
    high_seed["seed"] = 4294967295
    completed(v1, db, v1settings, high_seed)


def test_event_polling_sse_cursors_and_reconnection(v1, db, v1settings):
    run_id, _ = completed(v1, db, v1settings)
    url = f"/simulations/{run_id}/events"
    batch = v1.get(url, headers={"Accept": "application/json"}).json()
    wire.REGISTRY.validate("simulation-events", batch)
    assert batch["terminal"] and batch["next_sequence"] == 5
    validate_stream(batch["events"], run_id=run_id, project_id="knowledgeops")
    run = v1.get(f"/simulations/{run_id}").json()
    wire.REGISTRY.validate("simulation-run", run)
    assert run["event_count"] == run["last_sequence"] == 5
    response = v1.get(url, headers={"Accept": "text/event-stream", "Last-Event-ID": "2"})
    assert response.headers["x-accel-buffering"] == "no"
    steps = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: {") and '"event_id"' in line
    ]
    validate_stream(steps, run_id=run_id, project_id="knowledgeops", after=2)
    assert [s["sequence"] for s in steps] == [3, 4, 5]
    assert 'event: end\ndata: {"last_sequence": 5}' in response.text
    for cursor in ["-1", "nonsense", "6", "10000"]:
        assert_error(v1.get(url, params={"after": cursor}), "INVALID_CURSOR")
    assert_error(v1.get(url + "?after=2", headers={"Last-Event-ID": "3"}), "INVALID_CURSOR")
    empty = v1.get(url + "?after=5").json()
    assert empty["events"] == [] and empty["next_sequence"] == 5


def test_ownership_expiry_and_sessions(v1, v1app, db):
    run_id = start(v1)
    with TestClient(v1app, headers={"Origin": "http://testserver"}) as stranger:
        token = stranger.post("/simulation/session", json={}).json()["session_token"]
        stranger.headers["Authorization"] = "Bearer " + token
        for suffix in ["", "/events", "/result"]:
            assert_error(stranger.get(f"/simulations/{run_id}{suffix}"), "NOT_FOUND")
        with db.connection() as conn:
            conn.execute(
                "UPDATE runs SET expires_at=now()-interval '1 second' WHERE id=%s", (run_id,)
            )
        assert_error(v1.get(f"/simulations/{run_id}"), "RUN_EXPIRED")
        assert_error(stranger.get(f"/simulations/{run_id}"), "NOT_FOUND")
    with db.connection() as conn:
        conn.execute("UPDATE sessions SET expires_at=now()-interval '1 second'")
    assert_error(v1.get(f"/simulations/{run_id}"), "UNAUTHENTICATED")


def test_cookie_csrf_origin_and_bearer_bootstrap(v1, v1app):
    with TestClient(v1app, headers={"Origin": "http://testserver"}) as client:
        response = client.post(wire.PREFIX + "/simulation/session", json={})
        assert response.status_code == 201
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "Path=" + wire.PREFIX in response.headers["set-cookie"]
        headers = {"Idempotency-Key": "cookie-test"}
        assert_error(
            client.post(wire.PREFIX + "/simulations", json=payload(), headers=headers), "FORBIDDEN"
        )
        headers["X-CSRF-Token"] = response.json()["csrf_token"]
        assert (
            client.post(wire.PREFIX + "/simulations", json=payload(), headers=headers).status_code
            == 202
        )
        assert_error(
            client.post(
                wire.PREFIX + "/simulations",
                json=payload(),
                headers={**headers, "Origin": "https://evil.invalid"},
            ),
            "FORBIDDEN",
        )
    v1.headers.pop("Origin")
    start(v1)  # Server bearer clients need no browser Origin.
    assert_error(v1.post("/simulation/session", json={}), "FORBIDDEN")


def test_pending_cancel_failure_and_idempotency(v1, db, v1settings):
    run_id = start(v1, key="canonical-idempotency")
    assert start(v1, key="canonical-idempotency") == run_id
    changed = payload()
    changed["client_correlation_id"] = "another-correlation"
    assert_error(
        v1.post("/simulations", json=changed, headers={"Idempotency-Key": "canonical-idempotency"}),
        "CONFLICT",
    )
    assert_error(v1.get(f"/simulations/{run_id}/result"), "CONFLICT")
    cancelled = v1.post(f"/simulations/{run_id}/cancel", json={}).json()
    wire.REGISTRY.validate("simulation-run", cancelled)
    assert cancelled["status"] == "CANCELLED"
    result = v1.get(f"/simulations/{run_id}/result").json()
    wire.REGISTRY.validate("simulation-result", result)
    assert result["status"] == "CANCELLED" and result["output"] == {}
    run_id = start(v1)
    failed_settings = v1settings.model_copy(update={"max_attempts": 1})
    with db.connection() as conn:
        job = claim(conn, failed_settings)
        fail(conn, job, failed_settings, CANARY)
    failed = v1.get(f"/simulations/{run_id}/result")
    assert failed.status_code == 200 and CANARY not in failed.text
    wire.REGISTRY.validate("simulation-result", failed.json())
    assert failed.json()["status"] == "FAILED" and failed.json()["errors"]
    assert v1.post(f"/simulations/{run_id}/cancel", json={}).json()["status"] == "FAILED"


def test_replay_requires_same_owned_input_and_seed(v1, db, v1settings):
    source, original = completed(v1, db, v1settings)
    replay = payload()
    replay.update(mode="REPLAY_RECORDED", extensions={"x_replay_run_id": source})
    _, result = completed(v1, db, v1settings, replay)
    assert result["output"] == original["output"]
    assert result["provenance"]["data_origin"] == "RECORDED_SYNTHETIC"
    replay["seed"] = 43
    assert_error(
        v1.post("/simulations", json=replay, headers={"Idempotency-Key": "wrong-replay"}),
        "CONFLICT",
    )


@pytest.mark.parametrize(
    "mutation", ["seed", "correlation", "unknown", "nested", "array", "nested-seed", "mode"]
)
def test_strict_input_and_redaction(v1, mutation):
    body = payload()
    if mutation == "seed":
        del body["seed"]
    elif mutation == "correlation":
        del body["client_correlation_id"]
    elif mutation == "unknown":
        body[CANARY] = CANARY
    elif mutation == "nested":
        body["input"][CANARY] = {CANARY: CANARY}
    elif mutation == "array":
        body["input"]["collections"] = [{CANARY: CANARY}]
    elif mutation == "nested-seed":
        body["input"]["seed"] = 42
    else:
        body["mode"] = "LIVE_MODEL_AUTHENTICATED"
    response = v1.post("/simulations", json=body, headers={"Idempotency-Key": "bad-request"})
    assert_error(response, "FORBIDDEN" if mutation == "mode" else "VALIDATION_ERROR")


def test_payload_bounds_duplicates_and_service_errors(v1, db, monkeypatch):
    assert_error(
        v1.post(
            "/simulations",
            content='{"x":"' + CANARY * 700 + '"}',
            headers={"Content-Type": "application/json"},
        ),
        "PAYLOAD_TOO_LARGE",
    )
    assert_error(
        v1.post(
            "/simulations",
            content='{"seed":1,"seed":2}',
            headers={"Content-Type": "application/json"},
        ),
        "VALIDATION_ERROR",
    )
    nested = payload()
    branch = nested["input"]
    for _ in range(15):
        branch["child"] = {}
        branch = branch["child"]
    assert_error(v1.post("/simulations", json=nested), "VALIDATION_ERROR")
    from psycopg_pool import PoolTimeout

    def unavailable():
        raise PoolTimeout(CANARY)

    monkeypatch.setattr(db, "connection", unavailable)
    assert_error(v1.get("/ready"), "SERVICE_UNAVAILABLE")


def test_capacity_429_and_worker_revision_fencing(v1, db, v1settings):
    start(v1)
    start(v1)
    limited = v1.post("/simulations", json=payload(), headers={"Idempotency-Key": "capacity-test"})
    assert_error(limited, "RATE_LIMITED")
    assert limited.headers["retry-after"] == "60"
    with db.connection() as conn:
        job = claim(conn, v1settings)
    with pytest.raises(Exception, match="WORKER_REVISION_MISMATCH"):
        process(db, job, v1settings.model_copy(update={"contracts_source_revision": "2" * 40}))


def test_revocation_blocks_cached_run_events_and_results(v1, db, v1settings):
    run_id, result = completed(v1, db, v1settings)
    doc = result["output"]["citations"][0]
    with db.connection() as conn:
        conn.execute(
            "UPDATE documents SET lifecycle='revoked' WHERE document_id=%s", (doc["document_id"],)
        )
    for suffix in ["", "/events", "/result"]:
        response = v1.get(f"/simulations/{run_id}{suffix}")
        assert_error(response, "CONFLICT")
        assert doc["title"] not in response.text


def credentials(settings, **changes):
    from pathlib import Path

    token = secrets.token_urlsafe(32)
    row = {
        "token_sha256": digest(token),
        "application_id": "resolveai",
        "tenant_id": "demo",
        "environment": "development",
        "audience": "knowledgeops",
        "role": "staff",
        "collection_ids": ["people", "finance", "operations", "security"],
        "scopes": ["knowledge:read"],
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "synthetic_only": True,
    }
    row.update(changes)
    path = Path(settings.knowledge_credentials_file)
    path.write_text(json.dumps([row]))
    path.chmod(0o600)
    return {"Authorization": "Bearer " + token}


def query(**changes):
    body = {
        "schema_version": "1.0",
        "application_id": "resolveai",
        "tenant_id": "demo",
        "query": "What is the hotel allowance for domestic travel?",
        "collection_ids": ["people"],
        "max_results": 3,
        "max_excerpt_chars": 200,
        "correlation_id": "lookup:1",
        "mode": "SANDBOX_DETERMINISTIC",
    }
    body.update(changes)
    return body


def test_knowledge_normal_missing_acl_and_exact_excerpt(v1, v1settings):
    auth = credentials(v1settings)
    response = v1.post("/knowledge/query", json=query(), headers=auth)
    assert response.status_code == 200, response.text
    answer = response.json()
    wire.REGISTRY.validate("knowledge-response", answer)
    assert answer["status"] == "OK"
    assert answer["answer"] == "\n".join(c["passage"] for c in answer["citations"])
    for citation in answer["citations"]:
        assert len(citation["passage"]) <= 200
        assert (
            hashlib.sha256(citation["passage"].encode()).hexdigest()
            == citation["evidence"]["sha256"]
        )
    for text in ["lunar relocation allowance", "treasury reserve target"]:
        response = v1.post(
            "/knowledge/query",
            json=query(query=text, collection_ids=["finance", "people"]),
            headers=auth,
        )
        wire.REGISTRY.validate("knowledge-response", response.json())
        assert response.json()["status"] == "NO_RESULTS"
        assert response.json()["answer"] is None and response.json()["citations"] == []
        assert "treasury" not in response.text.lower() and "count" not in response.text
    auth = credentials(v1settings, role="finance")
    answer = v1.post(
        "/knowledge/query",
        json=query(query="treasury reserve target", collection_ids=["finance"]),
        headers=auth,
    ).json()
    assert answer["status"] == "OK" and answer["citations"]


@pytest.mark.parametrize(
    "grant,request_changes",
    [
        ({"tenant_id": "other-tenant"}, {}),
        ({"application_id": "flowops"}, {}),
        ({"environment": "staging"}, {}),
        ({"audience": "agentops"}, {}),
        ({"scopes": []}, {}),
        ({"collection_ids": ["operations"]}, {}),
        ({}, {"collection_ids": ["unknown-secret-collection"]}),
        ({}, {"mode": "LIVE_MODEL_AUTHENTICATED"}),
    ],
)
def test_knowledge_denials_never_retrieve_or_disclose(
    v1, v1settings, monkeypatch, grant, request_changes
):
    auth = credentials(v1settings, **grant)

    def forbidden(*_):
        pytest.fail("Denied query reached retrieval")

    monkeypatch.setattr("knowledgeops.contracts_api.retrieve", forbidden)
    response = v1.post("/knowledge/query", json=query(**request_changes), headers=auth)
    assert response.status_code == 200
    body = response.json()
    wire.REGISTRY.validate("knowledge-response", body)
    assert body["status"] == "DENIED" and body["answer"] is None and body["citations"] == []
    for key in ["source_id", "title", "passage", "count", "tenant", "unknown-secret-collection"]:
        assert key not in response.text


def test_knowledge_tenant_filter_revocation_expiry_and_bad_auth(v1, v1settings):
    auth = credentials(v1settings, tenant_id="empty-synthetic")
    response = v1.post("/knowledge/query", json=query(tenant_id="empty-synthetic"), headers=auth)
    assert response.json()["status"] == "NO_RESULTS"
    credentials(v1settings)  # Rotating the allowlist removes the old credential immediately.
    assert_error(v1.post("/knowledge/query", json=query(), headers=auth), "UNAUTHENTICATED")
    auth = credentials(
        v1settings, expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    )
    assert_error(v1.post("/knowledge/query", json=query(), headers=auth), "UNAUTHENTICATED")
    assert_error(
        v1.post("/knowledge/query", json=query(), headers={"Authorization": "Bearer " + CANARY}),
        "UNAUTHENTICATED",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {CANARY: CANARY},
        {"max_results": 6},
        {"query": "x" * 501},
        {"collection_ids": ["people", "people"]},
        {"role": "finance"},
    ],
)
def test_knowledge_narrow_input_and_redaction(v1, v1settings, changes):
    auth = credentials(v1settings)
    assert_error(
        v1.post("/knowledge/query", json=query(**changes), headers=auth), "VALIDATION_ERROR"
    )


def test_iq04_legacy_paths_preserve_known_field_errors(client):
    for extra in [
        {CANARY: CANARY},
        {"collections": [{CANARY: CANARY}]},
        {"question": {CANARY: CANARY}},
    ]:
        body = {"input": {"question": "hotel allowance", **extra}}
        response = client.post(
            wire.PREFIX + "/simulations", json=body, headers={"Idempotency-Key": "iq04-regression"}
        )
        assert response.status_code == 422 and CANARY not in response.text
    response = client.post(
        wire.PREFIX + "/simulations",
        json={"input": {"question": "x"}},
        headers={"Idempotency-Key": "iq04-known-field"},
    )
    assert response.json()["error"]["fields"] == ["body.input.question"]


def test_legacy_runs_are_not_relabelled_and_recorded_provenance_is_immutable(v1, db, v1settings):
    from knowledgeops.jobs import enqueue
    from knowledgeops.models import SimulationStart

    sid = digest(v1.headers["Authorization"][7:])
    with db.connection() as conn:
        legacy = enqueue(
            conn, sid, SimulationStart(input=payload()["input"]), "legacy-key", v1settings
        )
    assert_error(
        v1.post("/simulations", json=payload(), headers={"Idempotency-Key": "legacy-key"}),
        "CONFLICT",
    )
    assert_error(v1.get(f"/simulations/{legacy['id']}"), "NOT_FOUND")
    assert tick(db, v1settings)
    run_id, result = completed(v1, db, v1settings)
    changed = v1settings.model_copy(update={"contracts_source_revision": "2" * 40})
    with TestClient(
        create_app(changed, db), headers={"Authorization": v1.headers["Authorization"]}
    ) as updated:
        value = updated.get(f"/simulations/{run_id}/result").json()
        assert value["provenance"] == result["provenance"]


def test_active_stream_heartbeat_and_cookie_auth(v1, db, v1settings):
    run_id = start(v1)
    with db.connection() as conn:
        job = claim(conn, v1settings)
    import threading
    import time

    def finish():
        time.sleep(0.4)
        process(db, job, v1settings)

    worker = threading.Thread(target=finish)
    worker.start()
    auth = v1.headers.pop("Authorization")
    try:
        response = v1.get(
            wire.PREFIX + f"/simulations/{run_id}/events", headers={"Accept": "text/event-stream"}
        )
    finally:
        worker.join()
        v1.headers["Authorization"] = auth
    assert response.status_code == 200 and ": heartbeat" in response.text
    assert "event: end" in response.text
