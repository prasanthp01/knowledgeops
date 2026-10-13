"""Durable bounded operator ingestion queue."""

from alembic import op

revision = "002_ingestion_jobs"
down_revision = "001_initial"


def upgrade():
    op.execute("""CREATE TABLE ingestion_jobs (
        id uuid PRIMARY KEY, tenant_id text NOT NULL, payload jsonb NOT NULL,
        checksum text NOT NULL, status text NOT NULL DEFAULT 'ACCEPTED'
        CHECK(status IN ('ACCEPTED','RUNNING','SUCCEEDED','FAILED')),
        attempts integer NOT NULL DEFAULT 0, claim_epoch bigint NOT NULL DEFAULT 0,
        lease_until timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
        finished_at timestamptz, result text, UNIQUE(tenant_id,checksum)
    );
    CREATE INDEX ingest_queue ON ingestion_jobs(created_at) WHERE status IN ('ACCEPTED','RUNNING');
    """)


def downgrade():
    op.execute("DROP TABLE ingestion_jobs")
