"""Add contract metadata without rewriting or relabeling legacy runs."""

from alembic import op

revision = "003_contracts_v1"
down_revision = "002_ingestion_jobs"


def upgrade():
    op.execute("""
    CREATE TABLE contract_sessions (
      session_id text PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE
    );
    CREATE TABLE contract_runs (
      run_id uuid PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
      request jsonb NOT NULL,
      provenance jsonb NOT NULL
    );
    """)


def downgrade():
    op.execute("DROP TABLE contract_runs")
    op.execute("DROP TABLE contract_sessions")
