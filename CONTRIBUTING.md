# Contributing

Use an isolated branch and `_test` PostgreSQL database. Preserve versioned contracts and historical evidence. Keep business decisions outside UI code; enforce authorization in the query path and worker commit boundary.

For changed behavior, add focused tests for observable outcomes, permission leakage and failure recovery. Run lint, format, type, unit/integration/API/contract tests, and browser checks when the UI changes. Update source schemas, regenerate OpenAPI, and record evaluation/benchmark conditions. Never change tests merely to report green status. Mark unmeasured and unverified claims explicitly.

No public provider keys, external paid calls, production traffic tests or source uploads are part of a default development run. The GitHub workflow has read-only permissions and no publishing job. Real production/remote changes require the owner's scoped release.

