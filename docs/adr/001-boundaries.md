# ADR 001 — Public synthetic retrieval and private context

Accepted 2026-10-13. The public API hardcodes the `demo` tenant and accepts only four fictional roles. The private endpoint binds tenant and role to server configuration and an operator bearer token. These are separate data paths; the role picker is not enterprise identity management. Production SSO and tenant provisioning are out of scope.

Filter tenant, ACL, lifecycle, collection, retention and document revision in PostgreSQL before returning any passage to the ranker. BM25 document frequencies use only permitted passages. Do not expose denied titles, identifiers, counts or raw parser errors. A result is revalidated before each read so later revocation cannot serve a cached passage.

The public UI uses static HTML, CSS and browser JavaScript, served under the project prefix. This avoids an always-on JavaScript server on the resource-constrained target. A separate portfolio can embed or serve these assets through a separately configured integration.

Runs and queue state share one durable PostgreSQL row. Claim via SKIP LOCKED, use expiring leases and monotonic fencing, bound attempts, and commit results only for a current claim. Cancellation and session TTL invalidate access. No external side effects occur in public mode.

References: https://www.postgresql.org/docs/18/sql-select.html and https://www.psycopg.org/psycopg3/docs/advanced/pool.html .

# ADR 002 — Package schema reconciliation

Keep baseline contracts immutable. Generate application schemas in `schemas/` from strict Pydantic models and extend the supplied result/event envelopes compatibly. Make app, fixture, connector and policy provenance explicit. Place inference content in a separate envelope beside routing metadata; the package inference schema only defines routing metadata. Export environment, verification time and nullable verified deployment URL in the extended evidence schema. Reject unknown request fields. Version extensions as 1.0 with documented additional properties; any future incompatible change requires a new schema version.

# ADR 003 — Evidence-constrained optional AI

Public visitors cannot select live inference or provide keys. Optional private AgentOps configuration must be trusted HTTPS, have an unexpired policy and enforce an atomic local daily reservation. Calls have a fixed deadline and no local provider fallback; outages return deterministic cited passages. Only exact supported claims are accepted from the optional model. This deliberately defers free-form summarization until a semantic groundedness evaluation justifies it. API failures reserve the worst-case cost until operator reconciliation; reservations are not invoices.

