"""Restore a backup into an explicitly supplied EMPTY *_restore_test database."""

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from knowledgeops.config import ROOT, Settings
from knowledgeops.models import SearchInput
from knowledgeops.retrieval import retrieve


def pg_env(url):
    values = conninfo_to_dict(url)
    env = os.environ.copy()
    for key, name in {
        "host": "PGHOST",
        "port": "PGPORT",
        "user": "PGUSER",
        "password": "PGPASSWORD",
        "dbname": "PGDATABASE",
    }.items():
        if key in values:
            env[name] = values[key]
    return env


def run():
    source = os.environ.get("KO_BACKUP_SOURCE_URL", Settings().database_url)
    target = os.environ["KO_RESTORE_TARGET_URL"]
    if not conninfo_to_dict(target)["dbname"].endswith("_restore_test"):
        raise SystemExit("Refusing a target without the _restore_test suffix")
    with psycopg.connect(target, row_factory=dict_row) as conn:
        if conn.execute("SELECT 1 FROM pg_tables WHERE schemaname='public'").fetchone():
            raise SystemExit("Target must be empty; existing data is preserved")
    directory = ROOT / "work"
    directory.mkdir(exist_ok=True)
    backup = directory / ("restore-drill-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + ".dump")
    subprocess.run(
        ["pg_dump", "-Fc", "--no-owner", "--no-privileges", "-f", str(backup)],
        env=pg_env(source),
        check=True,
        capture_output=True,
    )
    backup.chmod(0o600)
    subprocess.run(
        [
            "pg_restore",
            "--no-owner",
            "--no-privileges",
            "--exit-on-error",
            "-d",
            conninfo_to_dict(target)["dbname"],
            str(backup),
        ],
        env=pg_env(target),
        check=True,
        capture_output=True,
    )

    def checksums(url):
        with psycopg.connect(url, row_factory=dict_row) as conn:
            rows = conn.execute(
                "SELECT tenant_id,document_id,version,checksum FROM documents ORDER BY tenant_id,document_id,version"
            ).fetchall()
            return rows

    assert checksums(source) == checksums(target)
    with psycopg.connect(target, row_factory=dict_row) as conn:
        result = retrieve(conn, SearchInput(question="hotel allowance domestic travel"))
        assert result["answer"]
        counts = {
            table: conn.execute(f"SELECT count(*) AS count FROM {table}").fetchone()["count"]
            for table in ["documents", "chunks", "runs"]
        }
    receipt = {
        "status": "PASS",
        "verified_at": datetime.now(UTC).isoformat(),
        "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
        "restored_counts": counts,
        "all_document_checksums_match": True,
        "restored_retrieval_verified": True,
        "scope": "Explicitly configured disposable restore database",
    }
    (ROOT / "docs/evidence/restore-drill.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    run()
