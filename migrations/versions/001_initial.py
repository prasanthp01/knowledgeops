"""Versioned documents, scoped sessions, durable runs and budget ledger."""

from alembic import op

revision = "001_initial"
down_revision = None


def upgrade():
    op.execute("""
    CREATE TABLE documents (
      tenant_id text NOT NULL, document_id text NOT NULL, version integer NOT NULL CHECK(version>0),
      title text NOT NULL, collection text NOT NULL, groups text[] NOT NULL CHECK(cardinality(groups)>0),
      classification text NOT NULL, revision text NOT NULL, lifecycle text NOT NULL,
      policy_key text NOT NULL, source_path text NOT NULL, checksum text NOT NULL,
      created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL, valid_until timestamptz NOT NULL,
      retention_until timestamptz NOT NULL, indexed_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(tenant_id, document_id, version),
      CHECK(revision IN ('current','previous','conflict')),
      CHECK(lifecycle IN ('active','revoked','expired','quarantined'))
    );
    CREATE INDEX document_scope ON documents(tenant_id, collection, lifecycle, revision);
    CREATE INDEX document_groups ON documents USING gin(groups);
    CREATE TABLE chunks (
      tenant_id text NOT NULL, document_id text NOT NULL, version integer NOT NULL,
      chunk_id text NOT NULL, section text NOT NULL, page integer NOT NULL CHECK(page>0),
      body text NOT NULL CHECK(length(body)<=1600),
      PRIMARY KEY(tenant_id, document_id, version, chunk_id),
      FOREIGN KEY(tenant_id,document_id,version) REFERENCES documents ON DELETE CASCADE
    );
    CREATE TABLE sessions (
      id text PRIMARY KEY, csrf_hash text NOT NULL, ip_hash text NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL
    );
    CREATE INDEX session_expiry ON sessions(expires_at);
    CREATE TABLE runs (
      id uuid PRIMARY KEY, session_id text NOT NULL REFERENCES sessions ON DELETE CASCADE,
      status text NOT NULL CHECK(status IN ('ACCEPTED','RUNNING','SUCCEEDED','FAILED','CANCELLED','EXPIRED')),
      mode text NOT NULL, input jsonb NOT NULL, input_hash text NOT NULL, idempotency_key text NOT NULL,
      result jsonb, error_code text, data_revision bigint, attempts integer NOT NULL DEFAULT 0,
      claim_epoch bigint NOT NULL DEFAULT 0, lease_until timestamptz,
      available_at timestamptz NOT NULL DEFAULT now(), created_at timestamptz NOT NULL DEFAULT now(),
      finished_at timestamptz, expires_at timestamptz NOT NULL,
      UNIQUE(session_id,idempotency_key)
    );
    CREATE INDEX run_queue ON runs(available_at,created_at) WHERE status IN ('ACCEPTED','RUNNING');
    CREATE INDEX run_owner ON runs(session_id,created_at);
    CREATE TABLE events (
      run_id uuid NOT NULL REFERENCES runs ON DELETE CASCADE, sequence integer NOT NULL CHECK(sequence>0),
      payload jsonb NOT NULL, PRIMARY KEY(run_id,sequence)
    );
    CREATE TABLE ingest_audit (
      id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, tenant_id text NOT NULL,
      document_id text NOT NULL, version integer NOT NULL, action text NOT NULL,
      checksum text, occurred_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE rate_buckets (
      key text NOT NULL, window_id bigint NOT NULL, count integer NOT NULL,
      PRIMARY KEY(key,window_id)
    );
    CREATE TABLE inference_ledger (
      operation_id text PRIMARY KEY, tenant_id text NOT NULL, input_hash text NOT NULL,
      reserved_cost numeric NOT NULL CHECK(reserved_cost>=0), result jsonb,
      created_at timestamptz NOT NULL DEFAULT now()
    );
    REVOKE ALL ON SCHEMA public FROM PUBLIC;
    """)


def downgrade():
    for table in [
        "inference_ledger",
        "rate_buckets",
        "ingest_audit",
        "events",
        "runs",
        "sessions",
        "chunks",
        "documents",
    ]:
        op.execute(f"DROP TABLE {table}")
