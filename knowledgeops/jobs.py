import hashlib
import json
import time
import uuid
from datetime import UTC, datetime

from psycopg.types.json import Jsonb

from knowledgeops import __version__
from knowledgeops.ingestion import expire_documents
from knowledgeops.models import FIXTURES_VERSION, POLICY_VERSION, ROLES, SimulationStart
from knowledgeops.retrieval import data_revision, retrieve


class JobError(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status


def fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def rate_limit(conn, key: str, limit: int):
    window = int(time.time()) // 60
    count = conn.execute(
        """INSERT INTO rate_buckets(key,window_id,count) VALUES (%s,%s,1)
        ON CONFLICT(key,window_id) DO UPDATE SET count=rate_buckets.count+1 RETURNING count""",
        (key, window),
    ).fetchone()["count"]
    if count > limit:
        raise JobError("RATE_LIMIT", 429)


def enqueue(conn, session_id: str, request: SimulationStart, key: str, settings) -> dict:
    # One inexpensive global admission lock makes limits strict across API processes.
    conn.execute("SELECT pg_advisory_xact_lock(704401)")
    payload = request.model_dump(mode="json")
    digest = fingerprint(payload)
    existing = conn.execute(
        "SELECT r.*,(SELECT count(*) FROM events e WHERE e.run_id=r.id) AS event_count FROM runs r WHERE session_id=%s AND idempotency_key=%s",
        (session_id, key),
    ).fetchone()
    if existing:
        if existing["input_hash"] != digest:
            raise JobError("IDEMPOTENCY_CONFLICT")
        return existing
    rate_limit(conn, f"run:{session_id}", settings.runs_per_minute)
    active = conn.execute(
        "SELECT count(*) AS count FROM runs WHERE status IN ('ACCEPTED','RUNNING') AND expires_at>now()"
    ).fetchone()["count"]
    owned = conn.execute(
        "SELECT count(*) AS count FROM runs WHERE session_id=%s AND status IN ('ACCEPTED','RUNNING') AND expires_at>now()",
        (session_id,),
    ).fetchone()["count"]
    if active >= settings.max_active_runs or owned >= settings.max_session_runs:
        raise JobError("CAPACITY_LIMIT", 429)
    if request.mode == "REPLAY_RECORDED":
        source = get_run(conn, session_id, request.replay_run_id)
        if source["status"] != "SUCCEEDED" or source["input"]["input"] != payload["input"]:
            raise JobError("REPLAY_INPUT_MISMATCH")
    run_id = str(uuid.uuid4())
    return conn.execute(
        """INSERT INTO runs
        (id,session_id,status,mode,input,input_hash,idempotency_key,expires_at)
        VALUES (%s,%s,'ACCEPTED',%s,%s,%s,%s,now()+(%s * interval '1 second')) RETURNING *""",
        (run_id, session_id, request.mode, Jsonb(payload), digest, key, settings.run_ttl_seconds),
    ).fetchone()


def get_run(conn, session_id: str, run_id) -> dict:
    try:
        parsed = uuid.UUID(str(run_id))
    except ValueError as exc:
        raise JobError("RUN_NOT_FOUND", 404) from exc
    row = conn.execute(
        """SELECT r.*,(SELECT count(*) FROM events e WHERE e.run_id=r.id) AS event_count FROM runs r JOIN sessions s ON s.id=r.session_id
        WHERE r.id=%s AND r.session_id=%s AND r.expires_at>now() AND s.expires_at>now()""",
        (parsed, session_id),
    ).fetchone()
    if not row:
        raise JobError("RUN_NOT_FOUND", 404)
    # Expiry is checked on reads even if the cleanup worker is unavailable.
    if row["data_revision"] is not None:
        invalid = row["data_revision"] != data_revision(conn, "demo")
        if row["result"]:
            for citation in row["result"]["output"]["citations"]:
                valid = conn.execute(
                    """SELECT 1 FROM documents WHERE tenant_id='demo' AND document_id=%s AND version=%s
                    AND lifecycle='active' AND valid_until>now() AND retention_until>now() AND groups && %s::text[]""",
                    (
                        citation["document_id"],
                        citation["version"],
                        ROLES[row["input"]["input"]["role"]],
                    ),
                ).fetchone()
                invalid = invalid or not valid
        if invalid:
            raise JobError("EVIDENCE_CHANGED")
    return row


def claim(conn, settings) -> dict | None:
    conn.execute(
        """UPDATE runs SET status='FAILED',error_code='ATTEMPTS_EXHAUSTED',finished_at=now(),claim_epoch=claim_epoch+1
        WHERE status='RUNNING' AND lease_until<=now() AND attempts>=%s""",
        (settings.max_attempts,),
    )
    return conn.execute(
        """UPDATE runs SET status='RUNNING',attempts=attempts+1,claim_epoch=claim_epoch+1,
        lease_until=now()+(%s * interval '1 second'), data_revision=(SELECT coalesce(max(id),0) FROM ingest_audit WHERE tenant_id='demo')
        WHERE id=(SELECT id FROM runs WHERE expires_at>now() AND attempts<%s
          AND ((status='ACCEPTED' AND available_at<=now()) OR (status='RUNNING' AND lease_until<=now()))
          ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *""",
        (settings.lease_seconds, settings.max_attempts),
    ).fetchone()


def lock_claim(conn, run):
    row = conn.execute(
        """SELECT * FROM runs WHERE id=%s AND claim_epoch=%s AND status='RUNNING'
        AND lease_until>now() AND expires_at>now() FOR UPDATE""",
        (run["id"], run["claim_epoch"]),
    ).fetchone()
    if not row:
        raise JobError("STALE_CLAIM")
    if row["data_revision"] != data_revision(conn, "demo"):
        raise JobError("EVIDENCE_CHANGED")
    return row


def emit(conn, run, title: str, overview: str, technical: dict, component: str, duration_ms=None):
    lock_claim(conn, run)
    seq = conn.execute(
        "SELECT coalesce(max(sequence),0)+1 AS seq FROM events WHERE run_id=%s", (run["id"],)
    ).fetchone()["seq"]
    event = {
        "schema_version": "1.0",
        "run_id": str(run["id"]),
        "project_id": "knowledgeops",
        "event_id": f"{run['id']}:{seq}",
        "sequence": seq,
        "occurred_at": datetime.now(UTC).isoformat(),
        "step_id": component,
        "step_type": component,
        "component": component,
        "status": "SUCCEEDED",
        "mode": run["mode"],
        "title": title,
        "overview": overview,
        "technical": {**technical, "_attempt": run["attempts"]},
        "duration_ms": duration_ms,
        "trace_id": str(run["id"]),
        "usage": {"kind": "NONE"},
    }
    conn.execute(
        "INSERT INTO events(run_id,sequence,payload) VALUES (%s,%s,%s)",
        (run["id"], seq, Jsonb(event)),
    )
    return event


def heartbeat(conn, run, settings) -> bool:
    return (
        conn.execute(
            """UPDATE runs SET lease_until=now()+(%s * interval '1 second')
        WHERE id=%s AND claim_epoch=%s AND status='RUNNING' AND lease_until>now() AND expires_at>now()
        RETURNING id""",
            (settings.lease_seconds, run["id"], run["claim_epoch"]),
        ).fetchone()
        is not None
    )


def process(database, run, settings):
    started = time.perf_counter()
    request = SimulationStart.model_validate(run["input"])
    with database.connection() as conn:
        metadata = conn.execute(
            "SELECT provenance FROM contract_runs WHERE run_id=%s", (run["id"],)
        ).fetchone()
        if metadata and (
            not settings.contracts_source_revision
            or (
                request.mode != "REPLAY_RECORDED"
                and metadata["provenance"]["source_revision"] != settings.contracts_source_revision
            )
        ):
            raise JobError("WORKER_REVISION_MISMATCH")
        emit(
            conn,
            run,
            "Validate the question",
            "The input is bounded and belongs to this private demo session.",
            {
                "question_length": len(request.input.question),
                "seed": request.input.seed,
                "schema_version": "1.0",
            },
            "validate",
        )
    with database.connection() as conn:
        lock_claim(conn, run)
        if request.mode == "REPLAY_RECORDED":
            source = get_run(conn, run["session_id"], request.replay_run_id)
            output = source["result"]["output"]
        else:
            output = retrieve(conn, request.input)
        emit(
            conn,
            run,
            "Apply permission policy",
            "Only authorized, active source passages enter the search.",
            output["permission"],
            "authorize",
        )
    with database.connection() as conn:
        heartbeat(conn, run, settings)
        emit(
            conn,
            run,
            "Rank permitted passages",
            "Search scores are calculated only from this role's permitted documents.",
            output["retrieval"],
            "retrieve",
        )
    with database.connection() as conn:
        emit(
            conn,
            run,
            "Validate source citations",
            "Every citation points to an exact version and passage.",
            {"citations": output["citations"], "answer_type": output["answer_type"]},
            "cite",
        )
    with database.connection() as conn:
        lock_claim(conn, run)
        result = {
            "schema_version": "1.0",
            "project_id": "knowledgeops",
            "run_id": str(run["id"]),
            "mode": run["mode"],
            "status": "SUCCEEDED",
            "app_version": __version__,
            "fixtures_version": FIXTURES_VERSION,
            "connector_kind": "synthetic_postgresql",
            "data_provenance": "Original fictional Atlas corpus; no real organization data",
            "policy_version": POLICY_VERSION,
            "output": output,
            "evidence": output["citations"],
            "errors": [],
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "cost": {
                "kind": "ACTUAL",
                "value": 0,
                "currency": "EUR",
                "scope": "external_llm_spend",
                "hosting": "Not allocated",
            },
            "usage": {
                "kind": "NONE",
                "provider_calls": 0,
                "input_tokens": None,
                "output_tokens": None,
            },
            "replayed_from": request.replay_run_id if request.mode == "REPLAY_RECORDED" else None,
            "limitations": [
                "Lexical matching is not semantic entailment",
                "Fictional demo roles; no production SSO",
                "No external provider called",
            ],
        }
        emit(
            conn,
            run,
            "Finish with evidence",
            "The result is ready for source inspection."
            if not output["abstention"]
            else "The system abstained because the evidence could not support one answer.",
            {
                "answer_type": output["answer_type"],
                "abstention": output["abstention"],
                "provider_calls": 0,
            },
            "result",
        )
        conn.execute(
            "UPDATE runs SET status='SUCCEEDED',result=%s,error_code=NULL,finished_at=now(),lease_until=NULL WHERE id=%s AND claim_epoch=%s",
            (Jsonb(result), run["id"], run["claim_epoch"]),
        )


def fail(conn, run, settings, code="PROCESSING_ERROR"):
    # Fencing still applies after a lost lease; a stale worker cannot reset a newer claim.
    conn.execute(
        """UPDATE runs SET status=CASE WHEN attempts>=%s THEN 'FAILED' ELSE 'ACCEPTED' END,
        error_code=%s, available_at=now()+(least(30,power(2,attempts)) * interval '1 second'),lease_until=NULL,
        finished_at=CASE WHEN attempts>=%s THEN now() ELSE NULL END
        WHERE id=%s AND claim_epoch=%s AND status='RUNNING' AND lease_until>now()""",
        (settings.max_attempts, code, settings.max_attempts, run["id"], run["claim_epoch"]),
    )


def cleanup(conn):
    expire_documents(conn)
    conn.execute(
        "UPDATE runs SET status='EXPIRED',result=NULL,claim_epoch=claim_epoch+1 WHERE expires_at<=now() AND status!='EXPIRED'"
    )
    conn.execute(
        "DELETE FROM events USING runs WHERE events.run_id=runs.id AND runs.status='EXPIRED'"
    )
    conn.execute("DELETE FROM runs WHERE expires_at<now()-interval '1 minute'")
    conn.execute("DELETE FROM sessions WHERE expires_at<=now()")
    conn.execute("DELETE FROM rate_buckets WHERE window_id<%s", (int(time.time()) // 60 - 2,))
