import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from knowledgeops.ingestion import revoke
from knowledgeops.jobs import JobError, claim, cleanup, heartbeat, process
from knowledgeops.worker import tick

P = "/api/projects/knowledgeops"


def start(client, **changes):
    body = {"input": {"question": "What is the hotel allowance for domestic travel?"}, **changes}
    return client.post(
        P + "/simulations", json=body, headers={"Idempotency-Key": str(uuid.uuid4())}
    )


def completed(client, db, settings, **changes):
    response = start(client, **changes)
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    assert tick(db, settings)
    result = client.get(P + f"/simulations/{run_id}/result")
    assert result.status_code == 200, result.text
    return run_id, result.json()


def test_full_journey_sse_poll_and_citations(client, db, settings):
    run_id, result = completed(client, db, settings)
    assert result["output"]["answer"]
    lifecycle = client.get(P + f"/simulations/{run_id}").json()
    assert lifecycle["event_count"] == 5 and lifecycle["completed_at"]
    assert lifecycle["fixtures_version"] and lifecycle["connector_kind"]
    assert result["cost"]["value"] == 0
    assert result["fixtures_version"] and result["connector_kind"]
    events = client.get(P + f"/simulations/{run_id}/events?format=json").json()["events"]
    assert [e["sequence"] for e in events] == [1, 2, 3, 4, 5]
    sse = client.get(P + f"/simulations/{run_id}/events", headers={"Last-Event-ID": "3"}).text
    assert "id: 4\n" in sse and "id: 1\n" not in sse and "event: done" in sse
    assert (
        client.get(P + f"/simulations/{run_id}/events?after=4&format=json").json()["events"][0][
            "sequence"
        ]
        == 5
    )


@pytest.mark.parametrize("suffix", ["", "/result", "/events?format=json", "/events"])
def test_cross_session_idor(client, app, db, settings, suffix):
    run_id, _ = completed(client, db, settings)
    with TestClient(app, headers={"Origin": "http://testserver"}) as stranger:
        stranger.post(P + "/session", json={})
        assert stranger.get(P + f"/simulations/{run_id}" + suffix).status_code == 404


def test_csrf_origin_and_private_boundaries(client):
    assert start(client, mode="LIVE_MODEL_AUTHENTICATED").status_code == 422
    assert (
        client.post(
            P + "/private/answers",
            json={"question": "hotel allowance", "operation_id": "operation-123456789"},
        ).status_code
        == 401
    )
    assert (
        client.post(
            P + "/session", json={}, headers={"Origin": "https://attacker.invalid"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            P + "/simulations",
            json={"input": {"question": "hotel allowance"}},
            headers={"X-CSRF-Token": "wrong", "Idempotency-Key": "attempt-12345"},
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "input",
    [
        {"question": "hi"},
        {"question": "x" * 501},
        {"question": "hotel allowance", "top_k": 50},
        {"question": "hotel allowance", "top_k": True},
        {"question": "hotel allowance", "role": "admin"},
        {"question": "hotel allowance", "url": "http://127.0.0.1"},
        {"question": "hotel allowance", "collections": []},
        {"question": "hotel allowance", "collections": ["people", "people"]},
    ],
)
def test_invalid_input(client, input):
    assert start(client, input=input).status_code == 422


def test_payload_errors_do_not_echo_input(client):
    response = start(
        client, input={"question": "hotel allowance", "token": "sensitive-marker-example"}
    )
    assert "sensitive-marker-example" not in response.text
    assert (
        client.post(
            P + "/session", content="x" * 17000, headers={"Content-Type": "application/json"}
        ).status_code
        == 413
    )
    assert (
        client.post(
            P + "/session", content="{}", headers={"Content-Type": "text/plain"}
        ).status_code
        == 415
    )


def test_idempotency(client):
    body = {"input": {"question": "hotel allowance"}}
    headers = {"Idempotency-Key": "same-operation-key"}
    one = client.post(P + "/simulations", json=body, headers=headers)
    two = client.post(P + "/simulations", json=body, headers=headers)
    assert one.json()["run_id"] == two.json()["run_id"]
    changed = client.post(
        P + "/simulations", json={"input": {"question": "invoice review"}}, headers=headers
    )
    assert changed.status_code == 409


def test_revocation_invalidates_result_and_trace(client, db, settings):
    run_id, _ = completed(client, db, settings)
    with db.connection() as conn:
        revoke(conn, "demo", "travel-policy")
    for suffix in ["/result", "/events?format=json", ""]:
        response = client.get(P + f"/simulations/{run_id}" + suffix)
        assert response.status_code == 409 and "140" not in response.text


def test_cancellation_prevents_stale_write(client, db, settings):
    run_id = start(client).json()["run_id"]
    with db.connection() as conn:
        job = claim(conn, settings)
    assert client.post(P + f"/simulations/{run_id}/cancel", json={}).json()["status"] == "CANCELLED"
    with pytest.raises(JobError, match="STALE_CLAIM"):
        process(db, job, settings)


def test_worker_recovery_fencing(client, db, settings):
    run_id = start(client).json()["run_id"]
    with db.connection() as conn:
        old = claim(conn, settings)
        conn.execute("UPDATE runs SET lease_until=now()-interval '1 second' WHERE id=%s", (run_id,))
    with db.connection() as conn:
        new = claim(conn, settings)
        assert new["claim_epoch"] > old["claim_epoch"]
        assert not heartbeat(conn, old, settings)
    with pytest.raises(JobError):
        process(db, old, settings)
    process(db, new, settings)
    assert client.get(P + f"/simulations/{run_id}/result").status_code == 200


def test_concurrent_claim_once(client, db, settings):
    start(client)

    def take(_):
        with db.connection() as conn:
            return claim(conn, settings)

    with ThreadPoolExecutor(max_workers=4) as executor:
        jobs = list(executor.map(take, range(4)))
    assert sum(job is not None for job in jobs) == 1


def test_limits_expiry_and_cleanup(client, db, settings):
    first = start(client).json()["run_id"]
    assert start(client).status_code == 202
    assert start(client).status_code == 429
    with db.connection() as conn:
        conn.execute("UPDATE runs SET expires_at=now()-interval '2 minutes'")
        cleanup(conn)
    assert client.get(P + f"/simulations/{first}").status_code == 404


def test_replay_requires_exact_owned_input(client, db, settings):
    source_id, original = completed(client, db, settings)
    replay_id, replay = completed(
        client, db, settings, mode="REPLAY_RECORDED", replay_run_id=source_id
    )
    assert replay["output"] == original["output"]
    assert replay["mode"] == "REPLAY_RECORDED" and replay["replayed_from"] == source_id
    changed = start(
        client,
        mode="REPLAY_RECORDED",
        replay_run_id=source_id,
        input={"question": "different question"},
    )
    assert changed.status_code == 409


def test_security_headers_and_path_assets(client):
    page = client.get("/projects/knowledgeops")
    assert page.status_code == 200 and "Every answer" in page.text
    assert "object-src 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-content-type-options"] == "nosniff"
    assert client.get("/projects/knowledgeops/assets/app.js").status_code == 200
    assert client.get("/api/projects/knowledgeops/ready").status_code == 200
