import uuid

from psycopg.types.json import Jsonb

from knowledgeops.ingestion import ingest
from knowledgeops.jobs import fingerprint
from knowledgeops.models import Document


def enqueue_document(conn, tenant: str, document: Document):
    payload = document.model_dump(mode="json")
    if len(document.model_dump_json()) > 100000:
        raise ValueError("DOCUMENT_SIZE_LIMIT")
    conn.execute("SELECT pg_advisory_xact_lock(704403)")
    count = conn.execute(
        "SELECT count(*) AS count FROM ingestion_jobs WHERE status IN ('ACCEPTED','RUNNING')"
    ).fetchone()["count"]
    if count >= 100:
        raise ValueError("INGESTION_QUEUE_FULL")
    return conn.execute(
        """INSERT INTO ingestion_jobs(id,tenant_id,payload,checksum) VALUES (%s,%s,%s,%s)
        ON CONFLICT(tenant_id,checksum) DO UPDATE SET checksum=excluded.checksum RETURNING id,status""",
        (uuid.uuid4(), tenant, Jsonb(payload), fingerprint(payload)),
    ).fetchone()


def tick_ingestion(db):
    with db.connection() as conn:
        conn.execute(
            "UPDATE ingestion_jobs SET status='FAILED',result='ATTEMPTS_EXHAUSTED',payload='{}' WHERE status='RUNNING' AND lease_until<=now() AND attempts>=3"
        )
        job = conn.execute("""UPDATE ingestion_jobs SET status='RUNNING',claim_epoch=claim_epoch+1,attempts=attempts+1,lease_until=now()+interval '30 seconds'
            WHERE id=(SELECT id FROM ingestion_jobs WHERE attempts<3 AND (status='ACCEPTED' OR (status='RUNNING' AND lease_until<=now()))
            ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *""").fetchone()
    if not job:
        return False
    try:
        document = Document.model_validate(job["payload"])
        with db.connection() as conn:
            current = conn.execute(
                "SELECT 1 FROM ingestion_jobs WHERE id=%s AND claim_epoch=%s AND lease_until>now() AND status='RUNNING' FOR UPDATE",
                (job["id"], job["claim_epoch"]),
            ).fetchone()
            if not current:
                return True
            status = ingest(conn, job["tenant_id"], document)
            conn.execute(
                "UPDATE ingestion_jobs SET status='SUCCEEDED',result=%s,finished_at=now(),payload='{}' WHERE id=%s AND claim_epoch=%s",
                (status, job["id"], job["claim_epoch"]),
            )
    except Exception:
        with db.connection() as conn:
            conn.execute(
                "UPDATE ingestion_jobs SET status='FAILED',result='INGESTION_REJECTED',payload='{}',finished_at=now() WHERE id=%s AND claim_epoch=%s AND lease_until>now()",
                (job["id"], job["claim_epoch"]),
            )
    return True
