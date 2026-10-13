import uuid

import httpx
import pytest

from knowledgeops.jobs import claim, fail
from knowledgeops.models import PrivateInput
from knowledgeops.private_ai import answer
from tests.test_api_jobs import P, completed, start
from tests.test_ingestion_private import prepare_private, private_settings


def test_host_rebinding_rejected(client):
    assert client.get(P + "/health", headers={"Host": "attacker.invalid"}).status_code == 400


def test_readtime_acl_revalidation(client, db, settings):
    run_id, _ = completed(client, db, settings)
    with db.connection() as conn:
        conn.execute(
            "UPDATE documents SET groups=ARRAY['finance'] WHERE document_id='travel-policy'"
        )
    assert client.get(P + f"/simulations/{run_id}/result").status_code == 409
    assert client.get(P + f"/simulations/{run_id}/events?format=json").status_code == 409


def test_session_expiry(client, db, settings):
    run_id, _ = completed(client, db, settings)
    with db.connection() as conn:
        conn.execute("UPDATE sessions SET expires_at=now()-interval '1 second'")
    assert client.get(P + f"/simulations/{run_id}/result").status_code == 401


def test_retry_backoff_and_exhaustion(client, db, settings):
    start(client)
    with db.connection() as conn:
        job = claim(conn, settings)
        fail(conn, job, settings)
        assert claim(conn, settings) is None
        conn.execute("UPDATE runs SET available_at=now(),attempts=2")
    with db.connection() as conn:
        job = claim(conn, settings)
        fail(conn, job, settings)
        row = conn.execute(
            "SELECT status,error_code FROM runs WHERE id=%s", (job["id"],)
        ).fetchone()
    assert row == {"status": "FAILED", "error_code": "PROCESSING_ERROR"}


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"claims": []},
        {"claims": [{"text": "x", "citation_id": []}]},
        {"claims": [{"text": "x" * 40000, "citation_id": "x"}]},
    ],
)
def test_malformed_model_fails_safe(db, settings, payload):
    prepare_private(db)
    result = answer(
        db,
        private_settings(settings),
        PrivateInput(question="hotel allowance domestic travel", operation_id=str(uuid.uuid4())),
        httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    )
    assert result["mode"] == "DETERMINISTIC_PRIVATE"
    assert result["output"]["answer"]


def test_default_content_contract_prevents_network_call(db, settings):
    prepare_private(db)

    def forbidden(_):
        raise AssertionError("No network call permitted")

    configured = private_settings(settings).model_copy(
        update={"agentops_content_contract": "disabled"}
    )
    result = answer(
        db,
        configured,
        PrivateInput(
            question="hotel allowance domestic travel", operation_id="not-allowed-operation"
        ),
        httpx.MockTransport(forbidden),
    )
    assert result["model_status"] == "CONTENT_CONTRACT_NOT_CONFIGURED"
