# Contributing

Use an isolated branch and `_test` PostgreSQL database. Preserve versioned contracts and historical evidence. Keep business decisions outside UI code; enforce authorization in the query path and worker commit boundary.

For changed behavior, add focused tests for observable outcomes, permission leakage and failure recovery. Run lint, format, type, unit/integration/API/contract tests, and browser checks when the UI changes. Update source schemas, regenerate OpenAPI, and record evaluation/benchmark conditions. Never change tests merely to report green status. Mark unmeasured and unverified claims explicitly.

No public provider keys, external paid calls, production traffic tests or source uploads are part of a default development run. The GitHub workflow has read-only permissions and no publishing job. Real production/remote changes require the owner's scoped release.

## Local test database

Follow README's **Verify and contribute** block. `scripts/configure_tests.py` creates ignored `.env.test` with generated credentials and a loopback-only connection. `compose.test.yaml` starts a separate PostgreSQL service; it does not reuse the demo database or its secrets. Tests apply migrations, grant the restricted runtime role its application permissions, and seed the synthetic corpus.

Use `uv run --env-file .env.test pytest -q` so both `KO_TEST_DATABASE_URL` and `KO_TEST_MIGRATION_URL` are loaded. Plain `uv run pytest` needs those two variables already configured. No administrator connection is used for application tests.

The default test port is 55446. To select another port before the first run, use `uv run python scripts/configure_tests.py --port 55447`. Re-running the helper preserves an existing file. Keep the configuration and volume together when stopping/restarting; deleting or regenerating only the credentials will break authentication against an existing volume. `docker compose --env-file .env.test -f compose.test.yaml stop` preserves both.
