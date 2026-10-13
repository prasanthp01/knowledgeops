"""Opt-in AgentOps routing probe. Never accepts questions, passages or live-mode inputs."""

import json
import os
import re
import stat
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr, model_validator

from knowledgeops.contracts import REGISTRY, error_payload
from knowledgeops.vendor_contracts.validation import ContractError

PROFILE_ID = "agentops-canonical-synthetic-v1"
POLICY = "policy-2026-10-v1"
ID = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$"


class SyntheticProfile(BaseModel):
    """Server-owned configuration, independent of the unsupported private content proposal."""

    model_config = ConfigDict(extra="forbid", strict=True)
    profile_id: Literal["agentops-canonical-synthetic-v1"]
    enabled: bool
    base_url: str = Field(max_length=256)
    bearer: SecretStr = Field(min_length=43, max_length=128)
    application_id: Literal["knowledgeops"]
    audience: Literal["agentops"]
    environment: Literal["test", "development"]
    tenant_id: Literal["demo"]
    scopes: list[Literal["inference:request"]] = Field(min_length=1, max_length=1)
    synthetic_only: Literal[True]
    allow_loopback_http: bool = False
    routing_policy_version: Literal["policy-2026-10-v1"]
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def endpoint(self):
        parsed = urlsplit(self.base_url)
        local = (
            self.allow_loopback_http and parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
        )
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (parsed.scheme != "https" and not local)
            or parsed.path.rstrip("/") not in {"", "/api/projects/agentops"}
            or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", self.bearer.get_secret_value())
        ):
            raise ValueError("Invalid trusted synthetic profile")
        return self


@dataclass(frozen=True)
class RoutingOutcome:
    status: Literal["ROUTED", "UNAVAILABLE"]
    remote_state: Literal["NOT_CALLED", "DECISION_RECEIVED", "UNKNOWN"]
    fallback: Literal["DETERMINISTIC_RETRIEVAL"] | None
    decision: dict | None
    error: dict | None
    # On UNKNOWN, retry only the identical request and operation identity to reconcile.
    operation_id: str


def read_profile(path):
    if not path:
        return None
    with Path(path).open("rb") as stream:
        mode = os.fstat(stream.fileno()).st_mode
        if not stat.S_ISREG(mode) or stat.S_IMODE(mode) & 0o077:
            raise ValueError("Private profile permissions required")
        data = stream.read(16385)
    if len(data) > 16384:
        raise ValueError("Profile exceeds limit")
    return SyntheticProfile.model_validate_json(data)


def synthetic_request(profile, *, operation_id, correlation_id, trace_id):
    """The LOW/PUBLIC labels describe this empty-context fixture only, never caller data."""
    request = {
        "schema_version": "1.0",
        "application_id": "knowledgeops",
        "environment": profile.environment,
        "tenant_id": profile.tenant_id,
        "task_type": "extract",
        "risk_class": "LOW",
        "required_capabilities": ["TEXT", "STRUCTURED_OUTPUT"],
        "data_classification": "PUBLIC",
        "allowed_regions": ["eu"],
        "latency_deadline_ms": 2000,
        "max_input_tokens": 1024,
        "max_output_tokens": 128,
        "max_cost": 0.01,
        "currency": "USD",
        "routing_policy_version": profile.routing_policy_version,
        "correlation_id": correlation_id,
        "trace_id": trace_id,
        "operation_id": operation_id,
        "mode": "SANDBOX_DETERMINISTIC",
        "context_refs": [],
        "extensions": {},
    }
    REGISTRY.validate("inference-request", request)
    return request


def validate_decision(value, request):
    REGISTRY.validate("route-decision", value)
    for key in ["operation_id", "correlation_id", "trace_id", "mode", "routing_policy_version"]:
        if value[key] != request[key]:
            raise ContractError("Peer response identity mismatch")
    selected = value["selected_deployment"]
    if selected and (
        selected["region"] not in request["allowed_regions"]
        or not set(request["required_capabilities"]) <= set(selected["capabilities"])
    ):
        raise ContractError("Peer response eligibility mismatch")
    attempts = value["attempts"]
    if [a["attempt"] for a in attempts] != list(range(1, len(attempts) + 1)):
        raise ContractError("Invalid attempt order")
    if value["outcome"] == "SUCCEEDED" and not any(
        a["outcome"] == "SUCCEEDED" and a["deployment_id"] == selected["deployment_id"]
        for a in attempts
    ):
        raise ContractError("Selected deployment has no successful attempt")
    usages = [value["usage"], *(a["usage"] for a in attempts)]
    for usage in usages:
        cost = usage["cost"]
        if (
            usage["kind"] not in {"NONE", "SIMULATED"}
            or usage["provider_calls"] != 0
            or cost["kind"] != usage["kind"]
            or cost["currency"] != "USD"
            or Decimal(str(cost["value"])) > Decimal(str(request["max_cost"]))
        ):
            raise ContractError("Peer cannot perform or bill actual provider calls")
    if any(
        a["usage"]["input_tokens"] > request["max_input_tokens"]
        or a["usage"]["output_tokens"] > request["max_output_tokens"]
        for a in attempts
    ):
        raise ContractError("Peer exceeded token limits")
    for key in ["input_tokens", "output_tokens", "cache_tokens", "provider_calls"]:
        if value["usage"][key] != sum(a["usage"][key] for a in attempts):
            raise ContractError("Peer aggregate usage mismatch")
    summed = sum((Decimal(str(a["usage"]["cost"]["value"])) for a in attempts), Decimal(0))
    if Decimal(str(value["usage"]["cost"]["value"])) != summed:
        raise ContractError("Peer aggregate cost mismatch")


def route_synthetic(settings, *, operation_id, correlation_id, trace_id, transport=None):
    """Advisory client for a fixed fixture. No retries, direct providers, or answer mutation."""
    if not all(
        isinstance(value, str) and re.fullmatch(ID, value)
        for value in [operation_id, correlation_id, trace_id]
    ):
        raise ValueError("Invalid synthetic operation identity")

    def unavailable(code="AI_UNAVAILABLE", remote="NOT_CALLED", decision=None):
        return RoutingOutcome(
            "UNAVAILABLE",
            remote,
            "DETERMINISTIC_RETRIEVAL",
            decision,
            error_payload(code, correlation_id),
            operation_id,
        )

    try:
        profile = read_profile(settings.agentops_synthetic_profile_file)
    except (OSError, ValueError, TypeError):
        return unavailable("POLICY_BLOCKED")
    if profile is None or not profile.enabled:
        return unavailable()
    if profile.expires_at <= datetime.now(UTC):
        return unavailable("POLICY_BLOCKED")
    request = synthetic_request(
        profile, operation_id=operation_id, correlation_id=correlation_id, trace_id=trace_id
    )
    try:
        deadline = time.monotonic() + 2
        with httpx.Client(
            timeout=2, transport=transport, follow_redirects=False, trust_env=False
        ) as client:
            with client.stream(
                "POST",
                profile.base_url.rstrip("/") + "/inference",
                headers={"Authorization": "Bearer " + profile.bearer.get_secret_value()},
                json=request,
            ) as response:
                content = bytearray()
                for part in response.iter_bytes(chunk_size=8192):
                    content.extend(part)
                    if len(content) > 262144 or time.monotonic() > deadline:
                        raise ContractError("Peer response exceeds limits")
                if response.headers.get("content-type", "").split(";")[0] != "application/json":
                    raise ContractError("Peer response is not JSON")
                value = json.loads(content)
                if response.status_code != 200:
                    REGISTRY.validate("error", value)
                    if value["http_status"] != response.status_code:
                        raise ContractError("Peer error status mismatch")
                    code = (
                        value["code"]
                        if value["code"]
                        in {
                            "UNAUTHENTICATED",
                            "FORBIDDEN",
                            "POLICY_BLOCKED",
                            "BUDGET_EXCEEDED",
                            "RATE_LIMITED",
                        }
                        else "AI_UNAVAILABLE"
                    )
                    # Never forward untrusted peer error messages or diagnostics.
                    return unavailable(code, "UNKNOWN")
                validate_decision(value, request)
        if value["outcome"] != "SUCCEEDED":
            return unavailable(value["outcome"], "DECISION_RECEIVED", value)
        return RoutingOutcome("ROUTED", "DECISION_RECEIVED", None, value, None, operation_id)
    except (httpx.HTTPError, ValueError, TypeError, KeyError, RecursionError):
        return unavailable(remote="UNKNOWN")
