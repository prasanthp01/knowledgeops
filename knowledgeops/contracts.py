"""Contracts 1.0.0 boundary; internal retrieval and durable jobs stay application-owned."""

import hashlib
import json
from datetime import UTC, datetime

from knowledgeops import __version__
from knowledgeops.config import ROOT
from knowledgeops.models import FIXTURES_VERSION, POLICY_VERSION, SearchInput
from knowledgeops.vendor_contracts.validation import SchemaRegistry

REGISTRY = SchemaRegistry()
PREFIX = "/api/projects/knowledgeops"
ACTIVE = {"ACCEPTED", "RUNNING"}
BODY_LIMIT = 16384
RETENTION = 120
INPUT_SCHEMA = SearchInput.model_json_schema()
del INPUT_SCHEMA["properties"]["seed"]
INPUT_SCHEMA["title"] = "KnowledgeOpsInput"
INPUT_SCHEMA["$id"] = "urn:knowledgeops:input:1.0"
INPUT_HASH = hashlib.sha256(
    json.dumps(INPUT_SCHEMA, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
).hexdigest()


def verified(name, value):
    REGISTRY.validate(name, value)
    return value


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def provenance(settings, mode="SANDBOX_DETERMINISTIC"):
    replay = mode == "REPLAY_RECORDED"
    return {
        "data_origin": "RECORDED_SYNTHETIC" if replay else "SYNTHETIC",
        "connector_kind": "REPLAY" if replay else "MOCK",
        "app_version": __version__,
        "fixtures_version": FIXTURES_VERSION,
        "rules_version": POLICY_VERSION,
        "workflow_version": "retrieval-1",
        "model_version": "none",
        "prompt_version": "none",
        "source_revision": settings.contracts_source_revision,
    }


def usage(identifier):
    return {
        "usage_id": identifier,
        "kind": "NONE",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_tokens": 0,
        "provider_calls": 0,
        "cost": {
            "kind": "NONE",
            "value": 0,
            "currency": "EUR",
            "pricing_version": "no-provider-calls",
        },
    }


def evidence(citation, passage=None):
    # Hash the exact returned passage, not a different source or a guessed file checksum.
    passage = citation["quote"] if passage is None else passage
    identity = f"{citation['document_id']}:v{citation['version']}:{citation['chunk_id']}"
    return {
        "evidence_id": "passage-" + hashlib.sha256(identity.encode()).hexdigest(),
        "origin": "FIXTURE",
        "reference": f"{citation['source_path']}#v{citation['version']}:{citation['chunk_id']}:chars0-{len(passage)}",
        "sha256": hashlib.sha256(passage.encode()).hexdigest(),
    }


ERRORS = {
    "UNAUTHENTICATED": (401, "Authentication is required."),
    "FORBIDDEN": (403, "This operation is not permitted."),
    "NOT_FOUND": (404, "Resource not found."),
    "CONFLICT": (409, "The request conflicts with the current state."),
    "RUN_EXPIRED": (410, "This run has expired."),
    "PAYLOAD_TOO_LARGE": (413, "The request exceeds the byte limit."),
    "VALIDATION_ERROR": (422, "The request does not match the supported schema."),
    "RATE_LIMITED": (429, "The request rate or concurrency limit was reached."),
    "SERVICE_UNAVAILABLE": (503, "The service could not complete the operation."),
    "INVALID_CURSOR": (409, "The event cursor is invalid."),
    "AI_UNAVAILABLE": (503, "Synthetic routing is unavailable; retain deterministic retrieval."),
    "POLICY_BLOCKED": (403, "The synthetic routing policy does not permit this operation."),
    "BUDGET_EXCEEDED": (429, "The simulated routing budget was exceeded."),
}


def error_payload(code, correlation_id):
    status, message = ERRORS[code]
    value = {
        "schema_version": "1.0",
        "code": code,
        "message": message,
        "retryable": status in {429, 503},
        "correlation_id": correlation_id,
        "http_status": status,
    }
    if status == 429:
        value["retry_after_seconds"] = 60
    return verified("error", value)


def schema(settings):
    return verified(
        "simulation-schema",
        {
            "schema_version": "1.0",
            "project_id": "knowledgeops",
            "input_schema_id": INPUT_SCHEMA["$id"],
            "input_schema_sha256": INPUT_HASH,
            "input_schema": INPUT_SCHEMA,
            "supported_modes": ["SANDBOX_DETERMINISTIC", "REPLAY_RECORDED"],
            "limits": {
                "max_body_bytes": BODY_LIMIT,
                "max_steps": 25,
                "max_run_seconds": RETENTION,
                "retention_seconds": RETENTION,
                "max_concurrent_per_session": settings.max_session_runs,
                "max_concurrent_global": settings.max_active_runs,
            },
        },
    )


def presets():
    items = json.loads((ROOT / "data/presets.json").read_text())
    return verified(
        "simulation-presets",
        {
            "schema_version": "1.0",
            "project_id": "knowledgeops",
            "presets": [
                {
                    "schema_version": "1.0",
                    "project_id": "knowledgeops",
                    "preset_id": item["id"],
                    "title": item["title"],
                    "description": item["title"] + " using original synthetic documents.",
                    "mode": "SANDBOX_DETERMINISTIC",
                    "seed": 42,
                    "input": item["input"],
                    "fixtures_version": FIXTURES_VERSION,
                }
                for item in items
            ],
        },
    )


def run_payload(run):
    terminal = run["status"] not in ACTIVE
    count = run.get("event_count", 0)
    return verified(
        "simulation-run",
        {
            "schema_version": "1.0",
            "project_id": "knowledgeops",
            "run_id": str(run["id"]),
            "mode": run["mode"],
            "status": run["status"],
            "created_at": utc(run["created_at"]),
            "completed_at": utc(run["finished_at"]) if terminal else None,
            "expires_at": utc(run["expires_at"]),
            "event_count": count,
            "last_sequence": count,
            "result_ref": f"{PREFIX}/simulations/{run['id']}/result" if terminal else None,
            "provenance": run["provenance"],
        },
    )


def event_payload(event, run):
    component, technical = event["component"], event["technical"]
    decisions = {
        "validate": ["Input accepted by the bounded application schema."],
        "authorize": [
            "Tenant, role, collection, lifecycle and retention filters applied before ranking."
        ],
        "retrieve": [
            f"Algorithm: {technical.get('algorithm')}; authorized matches: {technical.get('ranked_matches')}."
        ]
        if component == "retrieve"
        else [],
        "cite": ["Citations preserve source version, chunk and exact passage."],
        "result": [
            "Extractive output only; no provider calls.",
            "Evidence insufficient or conflicting; abstained.",
        ]
        if technical.get("abstention")
        else ["Extractive output only; no provider calls."],
    }
    return verified(
        "simulation-event",
        {
            **{
                key: event[key]
                for key in [
                    "schema_version",
                    "run_id",
                    "project_id",
                    "event_id",
                    "sequence",
                    "step_id",
                    "component",
                    "status",
                    "mode",
                    "title",
                    "overview",
                    "duration_ms",
                    "trace_id",
                ]
            },
            "occurred_at": utc(event["occurred_at"]),
            "step_type": {
                "validate": "VALIDATION",
                "authorize": "POLICY",
                "retrieve": "RETRIEVAL",
                "cite": "VALIDATION",
                "result": "SUMMARY",
            }[component],
            "technical": {
                "action": component,
                "input_schema_version": "1.0",
                "output_schema_version": "1.0",
                "input_ref": f"{run['id']}:input",
                "output_ref": event["event_id"],
                "decisions": decisions[component],
                "evidence": [evidence(c) for c in technical.get("citations", [])],
                "adapter_kind": "REPLAY" if run["mode"] == "REPLAY_RECORDED" else "DETERMINISTIC",
                "attempt": technical.get("_attempt", 1),
                "fallback_reason": None,
            },
            "usage": usage(event["event_id"]),
            "provenance": run["provenance"],
        },
    )


def result_payload(run):
    legacy = run["result"] or {}
    output = legacy.get("output", {}) if run["status"] == "SUCCEEDED" else {}
    accounting = usage(f"{run['id']}:result")
    elapsed = (run["finished_at"] - run["created_at"]).total_seconds() * 1000
    return verified(
        "simulation-result",
        {
            "schema_version": "1.0",
            "project_id": "knowledgeops",
            "run_id": str(run["id"]),
            "mode": run["mode"],
            "status": run["status"],
            "app_version": run["provenance"]["app_version"],
            "fixtures_version": run["provenance"]["fixtures_version"],
            "provenance": run["provenance"],
            "output_schema_version": "knowledgeops-extractive-1",
            "output": output,
            "safety_status": "SAFE" if run["status"] == "SUCCEEDED" else "BLOCKED",
            "evidence": [evidence(c) for c in output.get("citations", [])],
            "errors": [
                error_payload("SERVICE_UNAVAILABLE", run["request"]["client_correlation_id"])
            ]
            if run["status"] == "FAILED"
            else [],
            "warnings": output.get("warnings", [])
            + (
                ["Run cancelled; no final answer is available."]
                if run["status"] == "CANCELLED"
                else []
            ),
            "limitations": [
                "Lexical matching is not semantic entailment.",
                "Original synthetic corpus and fictional demo roles.",
                "No external provider called; hosting cost is unallocated.",
            ],
            "duration_ms": legacy.get("duration_ms", round(elapsed, 3)),
            "usage": accounting,
            "cost": accounting["cost"],
        },
    )
