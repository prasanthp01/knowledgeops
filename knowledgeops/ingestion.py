"""Operator-only ingestion. No upload endpoint or arbitrary network fetch exists."""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

from knowledgeops.models import Document

INJECTION = re.compile(
    r"ignore\s+(?:all\s+|previous\s+|prior\s+)*(?:instructions|rules)|"
    r"system\s+prompt|exfiltrat|reveal\s+(?:secrets|credentials)|<script|javascript:",
    re.I,
)


def chunks(document: Document):
    for section_no, section in enumerate(document.sections, 1):
        text = section.text.replace("\r\n", "\n").replace("\x00", "").strip()
        if INJECTION.search(text):
            raise ValueError("UNTRUSTED_DOCUMENT_INSTRUCTION")
        # Fixed-size, whitespace-aware chunks preserve source passage identity.
        offset, part = 0, 1
        while offset < len(text):
            end = min(offset + 1400, len(text))
            if end < len(text):
                split = text.rfind(" ", offset + 700, end)
                if split != -1:
                    end = split
            yield {
                "chunk_id": f"s{section_no}-p{part}",
                "section": section.heading,
                "page": section.page,
                "body": text[offset:end].strip(),
            }
            offset, part = end, part + 1


def ingest(conn, tenant: str, document: Document) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,40}", tenant):
        raise ValueError("INVALID_TENANT")
    checksum = hashlib.sha256(document.model_dump_json().encode()).hexdigest()
    # Serialize versions of one document, so concurrent ingest cannot create duplicate current versions.
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"{tenant}:{document.id}",)
    )
    existing = conn.execute(
        "SELECT checksum FROM documents WHERE tenant_id=%s AND document_id=%s AND version=%s",
        (tenant, document.id, document.version),
    ).fetchone()
    if existing:
        if existing["checksum"] != checksum:
            raise ValueError("IMMUTABLE_VERSION_CONFLICT")
        return "UNCHANGED"
    try:
        passages = list(chunks(document))
        lifecycle = document.lifecycle
    except ValueError:
        lifecycle, passages = "quarantined", []
    if document.revision == "current":
        current = conn.execute(
            "SELECT max(version) AS version FROM documents WHERE tenant_id=%s AND document_id=%s AND revision='current'",
            (tenant, document.id),
        ).fetchone()["version"]
        if current and current > document.version:
            raise ValueError("OLDER_CURRENT_VERSION")
        conn.execute(
            "UPDATE documents SET revision='previous' WHERE tenant_id=%s AND document_id=%s AND revision='current'",
            (tenant, document.id),
        )
    retention = datetime.now(UTC) + timedelta(days=document.retention_days)
    conn.execute(
        """INSERT INTO documents
        (tenant_id,document_id,version,title,collection,groups,classification,revision,lifecycle,
         policy_key,source_path,checksum,created_at,updated_at,valid_until,retention_until)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            tenant,
            document.id,
            document.version,
            document.title,
            document.collection,
            document.groups,
            document.classification,
            document.revision,
            lifecycle,
            document.policy_key,
            document.source_path,
            checksum,
            document.created_at,
            document.updated_at,
            document.valid_until,
            retention,
        ),
    )
    with conn.cursor() as cursor:
        cursor.executemany(
            """INSERT INTO chunks (tenant_id,document_id,version,chunk_id,section,page,body)
            VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            [
                (
                    tenant,
                    document.id,
                    document.version,
                    p["chunk_id"],
                    p["section"],
                    p["page"],
                    p["body"],
                )
                for p in passages
            ],
        )
    conn.execute(
        "INSERT INTO ingest_audit(tenant_id,document_id,version,action,checksum) VALUES (%s,%s,%s,%s,%s)",
        (
            tenant,
            document.id,
            document.version,
            "QUARANTINED" if lifecycle == "quarantined" else "INDEXED",
            checksum,
        ),
    )
    return lifecycle.upper()


def revoke(conn, tenant: str, document_id: str) -> int:
    rows = conn.execute(
        """UPDATE documents SET lifecycle='revoked' WHERE tenant_id=%s AND document_id=%s
        AND lifecycle!='revoked' RETURNING version""",
        (tenant, document_id),
    ).fetchall()
    for row in rows:
        conn.execute(
            "INSERT INTO ingest_audit(tenant_id,document_id,version,action) VALUES (%s,%s,%s,'REVOKED')",
            (tenant, document_id, row["version"]),
        )
    return len(rows)


def expire_documents(conn) -> int:
    rows = conn.execute("""UPDATE documents SET lifecycle='expired' WHERE lifecycle='active'
        AND (valid_until<=now() OR retention_until<=now()) RETURNING tenant_id,document_id,version""").fetchall()
    for row in rows:
        conn.execute(
            "INSERT INTO ingest_audit(tenant_id,document_id,version,action) VALUES (%s,%s,%s,'EXPIRED')",
            tuple(row.values()),
        )
    # Content deletion after retention applies to all lifecycle states.
    conn.execute(
        "DELETE FROM chunks c USING documents d WHERE c.tenant_id=d.tenant_id AND c.document_id=d.document_id AND c.version=d.version AND d.retention_until<=now()"
    )
    return len(rows)


def load_corpus(conn, corpus_path, tenant: str = "demo") -> dict:
    data = json.loads(corpus_path.read_text())
    if not isinstance(data, list) or not 1 <= len(data) <= 100:
        raise ValueError("CORPUS_SIZE")
    counts: dict[str, int] = {}
    for item in data:
        status = ingest(conn, tenant, Document.model_validate(item))
        counts[status] = counts.get(status, 0) + 1
    return counts
