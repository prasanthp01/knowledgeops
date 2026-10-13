# ADR 004 — Fail closed for incompatible live inference

Read-only inspection of the companion AgentOps OpenAPI on 2026-10-13 found `/api/projects/agentops/inferences`, schema 1.1. It is asynchronous, denominates budget in micro-USD, accepts a fixed synthetic task, and has no question/passage content fields. Its README says the worker uses mock providers and live requests return `LIVE_MODEL_NOT_CONFIGURED`.

Therefore KnowledgeOps must not send private passages to that endpoint or claim a working live integration. The public keyless path is independent and complete. Private content inference is disabled by default with `CONTENT_CONTRACT_NOT_CONFIGURED`. A separately named `knowledgeops-content-v1-proposal` adapter is exercised only against in-process mock transports. It is not compatible with the currently deployed AgentOps API, and not a published joint contract. Activation requires cross-project contract agreement, server authentication, policy/budget verification and live grounding evaluation. The existing gateway and this experimental content adapter must not be confused in a release report.

Free-form generated summaries remain deferred; the experimental adapter accepts only verbatim claims from current permitted citations. This prevents unsupported model prose from being presented as verified knowledge.

## 0.2.1 follow-up: distinct canonical synthetic caller

The pinned AgentOps Contracts v1 service at source `50bb507dafdd2c98d8d008c7307a9235439690dd` admits a fixed synthetic `extract` task with `TEXT` and `STRUCTURED_OUTPUT`, region `eu`, USD modeled cost, and empty `context_refs`. `knowledgeops.synthetic_peer` now exercises that service through an explicit trusted profile, independently of `private_ai.py`. Real localhost calls passed with simulated usage and zero provider calls. This is an advisory routing component, disabled by default and not invoked by retrieval or the default workflow. Its LOW/PUBLIC labels apply solely to the empty-context fixture; no private task is downgraded or relabeled. See peer handoff (historical receipt; not distributed) for exact evidence and limits. The private content/live inference decision above remains in force.
