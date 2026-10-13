-- Run as the database owner after migration; runtime never owns the schema.
GRANT USAGE ON SCHEMA public TO knowledgeops_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON documents, chunks, sessions, runs, events,
    ingest_audit, rate_buckets, inference_ledger, ingestion_jobs, contract_sessions,
    contract_runs TO knowledgeops_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO knowledgeops_app;
REVOKE CREATE ON SCHEMA public FROM knowledgeops_app;

