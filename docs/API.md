# API and contracts

Prefix: `/api/projects/knowledgeops`. Preserve it through the reverse proxy.

| Method/path | Purpose |
|---|---|
| GET /health, /ready | Process health / schema and corpus readiness |
| POST /session | Create an anonymous session; exact Origin and JSON body required |
| GET /simulation/schema, /simulation/presets | Strict input contract and synthetic examples |
| POST /simulations | Durable admission; session cookie, CSRF token, Idempotency-Key required |
| GET /simulations/{id} | Scoped lifecycle status |
| GET /simulations/{id}/events | SSE with Last-Event-ID or `?format=json&after=N` polling |
| GET /simulations/{id}/result | Scoped result, revalidated against current permission state |
| POST /simulations/{id}/cancel | Fenced cancellation of accepted/running work |
| POST /private/answers | Disabled unless an operator token is configured; server-bound role/tenant |
| GET /openapi.json | Generated API description |

All mutations require `Content-Type: application/json`; public mutations additionally require exact Origin. The session endpoint returns a CSRF value for the `X-CSRF-Token` header; the cookie is HttpOnly, SameSite=Strict, scoped to this API and Secure for HTTPS configuration. Maximum body is 16 KiB. Requests reject unknown fields and invalid role/collection/scenario selections. Run identifiers are not authorization credentials.

Public modes: `SANDBOX_DETERMINISTIC` and same-session `REPLAY_RECORDED`. Replay requires an original successful run and identical normalized input. It is labeled in every event and result. Anonymous `LIVE_MODEL_AUTHENTICATED` is rejected.

Input supports question, role, corpus, collections, BM25/keyword, top_k 1–5, revision current/previous/conflict, literal keyword filter and deterministic seed. Results disclose no denied identifiers. Source references are inert text, not arbitrary URLs. Scores and query coverage are lexical relevance indicators, not calibrated confidence.

JSON Schemas are in `schemas/`; original baseline schemas remain in the private brief. Extended envelopes add mandatory fixture/connector/policy provenance. `scripts/export_contracts.py` regenerates them and OpenAPI; tests validate actual event/result payloads. The proposal-only inference content adapter is documented separately and is not advertised as a compatible AgentOps v1.1 client.

Stable errors include SESSION_REQUIRED, CSRF_REJECTED, ORIGIN_REJECTED, INVALID_INPUT, RATE_LIMIT, CAPACITY_LIMIT, RUN_NOT_FOUND, EVIDENCE_CHANGED, RESULT_NOT_READY and IDEMPOTENCY_CONFLICT. Validation and database errors never echo submitted secret-like values or raw SQL. 429 includes Retry-After; clients should honor it. Invalid or unavailable scope returns the same not-found response.

