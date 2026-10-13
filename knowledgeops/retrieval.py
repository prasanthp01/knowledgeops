import math
import re
from collections import Counter

from knowledgeops.models import POLICY_VERSION, ROLES, SearchInput

STOP = set(
    "a an the is are was were be to of for in on at from and or do does how what when where who which can should must i we our it this that with my please tell me about need have has as much many under will would could".split()
)
SYNONYMS = {
    "hotels": "hotel",
    "limits": "limit",
    "costs": "cost",
    "purchases": "purchase",
    "approvals": "approval",
    "incidents": "incident",
    "logs": "log",
    "backups": "backup",
    "employees": "employee",
    "expenses": "expense",
    "booking": "book",
    "bookings": "book",
    "revoked": "revoked",
}


def tokenize(text: str) -> list[str]:
    return [
        SYNONYMS.get(word, word)
        for word in re.findall(r"[a-z0-9]+", text.lower())
        if word not in STOP
    ]


def data_revision(conn, tenant: str) -> int:
    return conn.execute(
        "SELECT coalesce(max(id),0) AS value FROM ingest_audit WHERE tenant_id=%s", (tenant,)
    ).fetchone()["value"]


def permitted_passages(conn, query: SearchInput, tenant: str = "demo") -> list[dict]:
    if query.role not in ROLES:
        raise ValueError("INVALID_ROLE")
    # Query-level authorization happens BEFORE ranking, candidate counts or model context.
    # Previous/conflicting records are explicitly opt-in, safe fictional policy scenarios.
    return conn.execute(
        """
       SELECT d.document_id,d.version,d.title,d.collection,d.classification,d.revision,
              d.policy_key,d.source_path,d.checksum,d.updated_at,c.chunk_id,c.section,c.page,c.body
       FROM documents d JOIN chunks c USING(tenant_id,document_id,version)
       WHERE d.tenant_id=%s AND d.groups && %s::text[] AND d.collection=ANY(%s::text[])
         AND d.lifecycle='active' AND d.valid_until>now() AND d.retention_until>now()
         AND (d.revision='current' OR d.revision=%s)
         AND (%s='' OR position(lower(%s) in lower(c.body))>0)
       ORDER BY d.document_id,d.version,c.chunk_id
       LIMIT 2001
    """,
        (
            tenant,
            ROLES[query.role],
            query.collections,
            query.revision,
            query.keyword_filter,
            query.keyword_filter,
        ),
    ).fetchall()


def retrieve(conn, query: SearchInput, tenant: str = "demo") -> dict:
    passages = permitted_passages(conn, query, tenant)
    if len(passages) > 2000:
        raise ValueError("CORPUS_LIMIT")
    if query.revision == "previous":
        # Use historical version only for documents with an authorized historical alternative.
        historical = {p["document_id"] for p in passages if p["revision"] == "previous"}
        passages = [
            p for p in passages if p["document_id"] not in historical or p["revision"] == "previous"
        ]
    query_terms = set(tokenize(query.question))
    bags = [Counter(tokenize(p["body"] + " " + p["title"] + " " + p["section"])) for p in passages]
    avg_length = sum(sum(b.values()) for b in bags) / max(len(bags), 1)
    document_frequency = Counter(term for bag in bags for term in bag)
    ranked = []
    for passage, bag in zip(passages, bags, strict=True):
        coverage = len(query_terms & bag.keys()) / max(1, len(query_terms))
        if coverage < 0.6 or not query_terms:
            continue
        score = 0.0
        for term in query_terms:
            tf = bag[term]
            if query.algorithm == "keyword":
                score += min(tf, 1)
            elif tf:
                df = document_frequency[term]
                idf = math.log(1 + (len(passages) - df + 0.5) / (df + 0.5))
                score += (
                    idf
                    * tf
                    * 2.2
                    / (tf + 1.2 * (0.25 + 0.75 * sum(bag.values()) / max(avg_length, 1)))
                )
        if score:
            ranked.append(
                {**passage, "score": round(score, 6), "query_coverage": round(coverage, 3)}
            )
    ranked.sort(key=lambda p: (-p["score"], p["document_id"], -p["version"], p["chunk_id"]))
    # Detect competing revisions BEFORE top-k truncation can hide a contradiction.
    policies: dict[str, set[tuple]] = {}
    for passage in ranked:
        policies.setdefault(passage["policy_key"], set()).add(
            (passage["document_id"], passage["version"])
        )
    conflict = query.revision == "conflict" and any(
        len(versions) > 1 for versions in policies.values()
    )
    chosen = ranked[: query.top_k]
    citations = [
        {
            "id": f"{p['document_id']}@{p['version']}:{p['chunk_id']}",
            "document_id": p["document_id"],
            "version": p["version"],
            "title": p["title"],
            "chunk_id": p["chunk_id"],
            "section": p["section"],
            "page": p["page"],
            "source_path": p["source_path"],
            "source_checksum": p["checksum"],
            "updated_at": p["updated_at"].isoformat(),
            "revision": p["revision"],
            "quote": p["body"],
            "score": p["score"],
            "query_coverage": p["query_coverage"],
        }
        for p in chosen
    ]
    abstention = (
        "CONFLICTING_REVISIONS" if conflict else ("NOT_ENOUGH_EVIDENCE" if not citations else None)
    )
    return {
        "answer_type": "EXTRACTIVE_NON_GENERATIVE",
        "answer": []
        if abstention
        else [{"text": c["quote"], "citation_id": c["id"]} for c in citations],
        "abstention": abstention,
        "citations": citations,
        "permission": {
            "policy_version": POLICY_VERSION,
            "role": query.role,
            "filter_applied": True,
            "exclusions": "Not disclosed",
            "scope": "Fictional demo roles" if tenant == "demo" else "Server-authorized principal",
        },
        "retrieval": {
            "algorithm": query.algorithm,
            "top_k": query.top_k,
            "revision": query.revision,
            "authorized_passages_considered": len(passages),
            "ranked_matches": len(ranked),
            "coverage_rule": "At least 60% of non-stopword query terms; lexical relevance is not semantic certainty",
        },
        "warnings": (
            ["Historical policy preview; not current guidance."]
            if query.revision == "previous"
            else []
        ),
    }
