# Corpus provenance

The 27 document versions in `corpus.json` were created for KnowledgeOps on 2026-10-13. All organization names, monetary amounts and business rules are original fiction. No employer, customer, CV, mailbox, proprietary document or external dataset was imported. Source strings such as `synthetic/travel-policy.md` are logical references into this corpus.

`scripts/generate_corpus.py` reproduces the fixtures and presets. It includes current/historical travel rules, a conflicting regional policy, role-restricted treasury notes, revoked and expired policies, and a deliberately injected note that ingestion quarantines. Source text and metadata use the repository MIT license. The original supplied planning brief is not part of this corpus license.

Fixture release `synthetic-2026-10-13.2` adds `flowops-refund-2026-01` and `flowops-refund-2026-02` in the operations collection, under the existing staff ACL. The original 25 versions remain unchanged. These new, fictional references describe review thresholds of 10000/5000 minor currency units and return windows of 30/14 days, matching the pinned FlowOps synthetic policy revisions. Each explicitly says it is not refund approval; the calling workflow retains its independent authorization, evidence, risk, eligibility and operator-approval checks. The January and February references are separate exact-version lookups, not a supersession of one another.

`evaluation.json` is held out of indexing and runtime prompts. Its author also created the corpus; this is a small internal regression dataset, not independently collected evidence. Evaluation exports contain the dataset SHA-256 and every question's expected/actual citation IDs. Do not tune the held-out questions silently; any change needs a new version and report.

