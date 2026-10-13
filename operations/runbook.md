# Operations runbook

## Start and verify

Follow the root README. Keep `.env.compose` private and backed up separately from the source; use a secret manager on production. Check `docker compose --env-file .env.compose ps`, `/api/projects/knowledgeops/ready`, a real keyless run and the ordered event stream. Inspect only this project's logs; public HTTP access logging is disabled and worker errors contain stable codes rather than document bodies.

Runtime API and worker are UID 10001, read-only, with dropped capabilities and no-new-privileges. Each is limited to 128 MiB and 0.2 CPU. PostgreSQL is limited to 256 MiB/0.2 CPU, 30 connections and 32 MiB shared buffers. The worker and DB use an internal network. The API additionally uses an ingress bridge to expose its port on loopback; Docker 29 did not publish the port from an internal-only bridge. Public APIs offer no outbound fetch capability. The default stack has no live model credentials.

## Ingestion and revocation

The CLI is operator-only, with DB credentials from the environment. Seed is idempotent and does not revive revoked versions. To ingest an approved local `.txt`, `.md` or text PDF, create metadata matching `Document` in `knowledgeops/models.py` and run:

```sh
python -m knowledgeops.cli enqueue --tenant demo --metadata approved-metadata.json --source approved.md
python -m knowledgeops.cli status --tenant demo
python -m knowledgeops.cli revoke --tenant demo --document-id approved-document
```

The parser runs in a separate bounded process. Limits: 256 KiB input, 20 PDF pages, 60,000 extracted characters, eight-second process timeout, four CPU seconds, and 256 MiB address space on Linux. Encrypted/active-content/image-only PDFs are rejected; there is no OCR. The ingestion queue has a 100-job cap. Completed job content is cleared; document retention remains authoritative. Use new document versions for policy/ACL changes.

## Recovery

Stop/start/restart only this Compose project. Restarting API, worker and database preserves completed synthetic results and documents; this is verified in `docs/evidence/compose-smoke.json`. A crashed worker's lease expires after 30 seconds; another worker increments its claim epoch, retries within the attempt limit and rejects stale commits. Expired runs and sessions are inaccessible even before cleanup runs. Uncertain external model costs remain reserved until operator reconciliation.

Back up with the database operator account to encrypted storage. Example local synthetic command (redirect output to private backup storage, never Git):

```sh
docker compose --env-file .env.compose exec -T db pg_dump -U postgres -Fc knowledgeops > knowledgeops.dump
```

A backup succeeds only after restoring it into a new disposable database with `pg_restore --no-owner --exit-on-error`, then checking document checksums, schema revision and an actual retrieval query. The local restore drill matched all document checksums. Source backups are not a substitute for database and secret backups. Encryption-at-rest/storage retention is deployment-specific and not claimed as configured on the VPS.

Migrations are forward in normal operation. For rollback, restore the prior verified image and its compatible database snapshot in a disposable preview first. Migration downgrade removes tables and must never be used against a populated production database. An empty throwaway database passed upgrade → downgrade → upgrade; a populated synthetic backup passed a separate restore drill. Keep those claims distinct.

## Deployment handoff

The owner-authorized deployment is recorded in `docs/DEPLOYMENT.md`; production uses `operations/compose.production.yaml` and the guarded Nginx fragment, not the older Caddy example. Preserve `/`, other projects and their databases. For a future release: recheck resource availability, choose a free loopback port, build the target architecture, rescan images, verify backup restoration, set the exact HTTPS origin and Secure cookies, and run staging proxy/SSE/deep-refresh checks. A Mac ARM image is not an x86_64 deployment artifact.

Maintain a before/after route map and rollback to the previous image/route without deleting volumes. Never use `down -v`, destructive seeding, host-wide Docker cleanup or unrelated database changes. The current HTTPS route has passed public API and browser verification. Future releases require fresh receipts.

## Troubleshooting

- Origin/Host rejected: use the exact configured origin, including port. Behind the proxy, retain the public Host header and set `KO_PUBLIC_ORIGIN` accordingly.
- Readiness fails: check migration exit status and synthetic seeding. API and worker must use the restricted runtime role, not the owner.
- Accepted run remains pending: check the worker process, DB availability and queue leases. Run admission is not a completed answer.
- EVIDENCE_CHANGED: permissions or versions changed; start a new run. Do not bypass validation to retrieve old text.
- Rate/capacity limit: respect Retry-After and let bounded work finish. Do not raise limits without measurement.
- Content model unavailable: expected by default. The current AgentOps contract has no document-content path. Do not point the experimental adapter at its synthetic inference endpoint.

