import io
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pypdf import PdfWriter

from knowledgeops.config import ROOT
from knowledgeops.ingest_worker import enqueue_document, tick_ingestion
from knowledgeops.ingestion import load_corpus
from knowledgeops.models import Document, PrivateInput
from knowledgeops.parsers import parse_bytes, parse_local
from knowledgeops.private_ai import answer, supported_claims


def test_durable_ingestion_idempotency(db):
    document = Document.model_validate(
        json.loads((ROOT / "data/corpus.json").read_text())[0]
    ).model_copy(update={"id": "worker-travel-copy"})
    with db.connection() as conn:
        first = enqueue_document(conn, "private", document)
        second = enqueue_document(conn, "private", document)
        assert first["id"] == second["id"]
    assert tick_ingestion(db)
    assert not tick_ingestion(db)
    with db.connection() as conn:
        job = conn.execute(
            "SELECT status,payload FROM ingestion_jobs WHERE id=%s", (first["id"],)
        ).fetchone()
        assert job == {"status": "SUCCEEDED", "payload": {}}
        assert conn.execute(
            "SELECT 1 FROM chunks WHERE tenant_id='private' AND document_id='worker-travel-copy'"
        ).fetchone()


def test_text_parser_process_limits(tmp_path):
    source = tmp_path / "approved.md"
    source.write_text("# Approved synthetic source\nHotel allowance is EUR 140.")
    sections = parse_local(source)
    assert sections[0]["page"] == 1 and "140" in sections[0]["text"]
    with pytest.raises(ValueError):
        parse_bytes(b"x" * 262145, ".txt")
    with pytest.raises(UnicodeDecodeError):
        parse_bytes(b"\xff\xff", ".txt")
    with pytest.raises(ValueError):
        parse_bytes(b"anything", ".exe")
    source.write_bytes(b"\x00")
    with pytest.raises(ValueError):
        parse_local(source)


def test_pdf_rejects_encrypted_and_active_content():
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("synthetic-test-only")
    stream = io.BytesIO()
    writer.write(stream)
    with pytest.raises(ValueError):
        parse_bytes(stream.getvalue(), ".pdf")
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_js("app.alert('synthetic test')")
    stream = io.BytesIO()
    writer.write(stream)
    with pytest.raises(ValueError, match="ACTIVE_PDF_REJECTED"):
        parse_bytes(stream.getvalue(), ".pdf")


def private_settings(settings):
    return settings.model_copy(
        update={
            "agentops_content_contract": "knowledgeops-content-v1-proposal",
            "private_token": "synthetic-token-" + "a" * 32,
            "agentops_url": "https://agentops.example.invalid",
            "agentops_token": "synthetic-test-token",
            "agentops_policy_expires": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "agentops_policy_version": "test-policy-1",
            "private_request_budget": 0.01,
            "private_daily_budget": 0.02,
        }
    )


def prepare_private(db):
    with db.connection() as conn:
        load_corpus(conn, ROOT / "data/corpus.json", "private")


def test_private_model_supported_claims_and_budget(db, settings):
    prepare_private(db)
    config = private_settings(settings)
    calls = []

    def provider(request):
        body = json.loads(request.content)
        calls.append(body)
        passage = body["passages"][0]
        return httpx.Response(
            200, json={"claims": [{"text": passage["text"], "citation_id": passage["id"]}]}
        )

    transport = httpx.MockTransport(provider)
    request = PrivateInput(
        question="hotel allowance domestic travel", operation_id="private-operation-123"
    )
    result = answer(db, config, request, transport)
    assert result["model_status"] == "SUPPORTED_CLAIMS"
    assert result["mode"] == "LIVE_MODEL_AUTHENTICATED"
    assert calls[0]["routing"]["data_classification"] == "RESTRICTED"
    assert answer(db, config, request, transport)["model_status"] == "OPERATION_ALREADY_RESERVED"
    answer(
        db, config, request.model_copy(update={"operation_id": "private-operation-456"}), transport
    )
    blocked = answer(
        db, config, request.model_copy(update={"operation_id": "private-operation-789"}), transport
    )
    assert blocked["model_status"] == "BUDGET_BLOCKED" and len(calls) == 2


@pytest.mark.parametrize("status", [429, 500, 503])
def test_provider_failure_fallback(db, settings, status):
    prepare_private(db)
    result = answer(
        db,
        private_settings(settings),
        PrivateInput(
            question="hotel allowance domestic travel", operation_id="provider-failure-test"
        ),
        httpx.MockTransport(lambda _: httpx.Response(status)),
    )
    assert result["mode"] == "DETERMINISTIC_PRIVATE"
    assert result["output"]["answer"] and result["model_status"] == "AI_UNAVAILABLE_OR_UNSUPPORTED"
    assert result["cost"]["kind"] == "ESTIMATED"


def test_stale_policy_and_hallucination(db, settings):
    prepare_private(db)
    request = PrivateInput(
        question="hotel allowance domestic travel", operation_id="model-schema-test-1"
    )
    config = private_settings(settings)
    stale = config.model_copy(update={"agentops_policy_expires": "2020-01-01T00:00:00Z"})
    assert answer(db, stale, request)["model_status"] == "POLICY_BLOCKED"
    provider = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={
                "claims": [
                    {"text": "Invented policy 123456", "citation_id": "travel-policy@2:s1-p1"}
                ]
            },
        )
    )
    result = answer(db, config, request, provider)
    assert result["mode"] == "DETERMINISTIC_PRIVATE"
    assert "123456" not in json.dumps(result)


def test_invalid_citation_rejected():
    with pytest.raises(ValueError):
        supported_claims({"claims": [{"text": "secret", "citation_id": "other-tenant:1"}]}, [])
