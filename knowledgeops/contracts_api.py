"""Opt-in canonical API: uvicorn knowledgeops.contracts_api:create_app --factory.

Keep this service and its synthetic database private until coordinated integration.
The deployed legacy API and frontend do not implicitly switch wire formats.
"""

import asyncio
import hashlib
import json
import re
import secrets
import stat
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from psycopg import Error as DatabaseError
from psycopg.types.json import Jsonb
from psycopg_pool import PoolTimeout
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from starlette.exceptions import HTTPException

from knowledgeops import __version__
from knowledgeops import contracts as wire
from knowledgeops.config import ROOT, Settings
from knowledgeops.db import Database
from knowledgeops.jobs import JobError, enqueue, get_run, rate_limit
from knowledgeops.models import FIXTURES_VERSION, Collection, Role, SearchInput, SimulationStart
from knowledgeops.retrieval import retrieve
from knowledgeops.vendor_contracts.validation import ContractError


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class ApiError(Exception):
    def __init__(self, code):
        self.code = code


class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    token_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    application_id: Literal["resolveai", "flowops", "knowledgeops", "agentops"]
    tenant_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
    environment: Literal["development", "staging", "production"]
    audience: str = Field(min_length=1, max_length=96)
    role: Role
    collection_ids: list[Collection] = Field(min_length=1, max_length=4)
    scopes: list[Literal["knowledge:read"]] = Field(max_length=1)
    expires_at: AwareDatetime
    synthetic_only: Literal[True]


def service_principal(request, settings):
    authorization = request.headers.get("authorization", "")
    if not re.fullmatch(r"Bearer [A-Za-z0-9_-]{43,128}", authorization):
        raise ApiError("UNAUTHENTICATED")
    if not settings.knowledge_credentials_file:
        raise ApiError("UNAUTHENTICATED")
    try:
        path = Path(settings.knowledge_credentials_file)
        # Reopen on every request: removing or replacing a grant revokes it immediately.
        with path.open("rb") as source:
            import os

            mode = os.fstat(source.fileno()).st_mode
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) & 0o077:
                raise ValueError("Credential file permissions")
            data = source.read(32769)
        if len(data) > 32768:
            raise ValueError("Credential file size")
        rows = json.loads(data)
        if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
            raise ValueError("Credential file shape")
        principals = [Principal.model_validate_json(json.dumps(row)) for row in rows]
        if len({p.token_sha256 for p in principals}) != len(principals):
            raise ValueError("Duplicate credential")
    except (OSError, ValueError, TypeError):
        raise ApiError("SERVICE_UNAVAILABLE") from None
    token_hash = digest(authorization[7:])
    for principal in principals:
        if secrets.compare_digest(principal.token_sha256, token_hash):
            if principal.expires_at <= datetime.now(UTC):
                break
            return principal
    raise ApiError("UNAUTHENTICATED")


def reject_constant(_):
    raise ValueError("Non-finite JSON")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key")
        result[key] = value
    return result


def create_app(settings=None, database=None):
    settings = settings or Settings()
    if not re.fullmatch(r"[a-f0-9]{40}", settings.contracts_source_revision):
        raise ValueError("Set KO_CONTRACTS_SOURCE_REVISION to the exact tested source SHA")
    if settings.contracts_environment not in {"development", "staging", "production"}:
        raise ValueError("Invalid Contracts environment")
    settings = settings.model_copy(
        update={
            "run_ttl_seconds": wire.RETENTION,
            "max_session_runs": min(4, settings.max_session_runs),
            "max_active_runs": min(16, settings.max_active_runs),
        }
    )
    db = database or Database(settings.database_url)
    streams: dict[str, int] = {}

    @asynccontextmanager
    async def lifespan(_):
        if database is None:
            db.open()
        yield
        if database is None:
            db.close()

    app = FastAPI(
        title="KnowledgeOps Contracts v1",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.db, app.state.settings = db, settings
    router = APIRouter()

    def error_response(request, code):
        payload = wire.error_payload(
            code, getattr(request.state, "correlation_id", str(uuid.uuid4()))
        )
        return JSONResponse(
            payload,
            status_code=payload["http_status"],
            headers={"Retry-After": "60"} if payload["http_status"] == 429 else None,
        )

    @app.exception_handler(ApiError)
    async def api_error(request, exc):
        return error_response(request, exc.code)

    @app.exception_handler(JobError)
    async def job_error(request, exc):
        code = {
            401: "UNAUTHENTICATED",
            403: "FORBIDDEN",
            404: "NOT_FOUND",
            409: "CONFLICT",
            410: "RUN_EXPIRED",
            422: "VALIDATION_ERROR",
            429: "RATE_LIMITED",
            503: "SERVICE_UNAVAILABLE",
        }.get(exc.status, "SERVICE_UNAVAILABLE")
        return error_response(request, code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, _):
        return error_response(request, "VALIDATION_ERROR")

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error_response(
            request, "NOT_FOUND" if exc.status_code in {404, 405} else "VALIDATION_ERROR"
        )

    @app.exception_handler(DatabaseError)
    @app.exception_handler(PoolTimeout)
    @app.exception_handler(ContractError)
    async def unavailable(request, _):
        return error_response(request, "SERVICE_UNAVAILABLE")

    @app.middleware("http")
    async def boundaries(request, call_next):
        request.state.correlation_id = str(uuid.uuid4())
        try:
            if request.url.hostname != urlparse(settings.public_origin).hostname:
                raise ApiError("FORBIDDEN")
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                origin = request.headers.get("origin")
                if origin is not None and origin != settings.public_origin:
                    raise ApiError("FORBIDDEN")
                if request.headers.get("content-type", "").split(";")[0] != "application/json":
                    raise ApiError("VALIDATION_ERROR")
                parts, size = [], 0
                async for part in request.stream():
                    size += len(part)
                    if size > wire.BODY_LIMIT:
                        raise ApiError("PAYLOAD_TOO_LARGE")
                    parts.append(part)
                try:
                    request.state.payload = json.loads(
                        b"".join(parts),
                        parse_constant=reject_constant,
                        object_pairs_hook=unique_object,
                    )
                except (ValueError, RecursionError, UnicodeError):
                    raise ApiError("VALIDATION_ERROR") from None
            response = await call_next(request)
        except ApiError as exc:
            response = error_response(request, exc.code)
        except Exception:
            response = error_response(request, "SERVICE_UNAVAILABLE")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if settings.secure_cookies:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    def body(request, name=None):
        payload = request.state.payload
        try:
            if name:
                wire.REGISTRY.validate(name, payload)
            elif payload != {}:
                raise ContractError("Empty object required")
        except (ContractError, RecursionError):
            raise ApiError("VALIDATION_ERROR") from None
        return payload

    def session(request: Request):
        bearer = "authorization" in request.headers
        authorization = request.headers.get("authorization", "")
        token = (
            authorization.removeprefix("Bearer ")
            if bearer
            else request.cookies.get("ko_v1_session", "")
        )
        if (bearer and not authorization.startswith("Bearer ")) or not re.fullmatch(
            r"[A-Za-z0-9_-]{43}", token
        ):
            raise ApiError("UNAUTHENTICATED")
        sid = digest(token)
        with db.connection() as conn:
            row = conn.execute(
                "SELECT s.* FROM sessions s JOIN contract_sessions c ON c.session_id=s.id WHERE s.id=%s AND s.expires_at>now()",
                (sid,),
            ).fetchone()
            if not row:
                raise ApiError("UNAUTHENTICATED")
            rate_limit(conn, f"contract-read:{sid}", 240)
            if request.method != "GET" and not bearer:
                if request.headers.get(
                    "origin"
                ) != settings.public_origin or not secrets.compare_digest(
                    digest(request.headers.get("x-csrf-token", "")), row["csrf_hash"]
                ):
                    raise ApiError("FORBIDDEN")
        return sid

    def owned(conn, sid, run_id):
        try:
            identifier = uuid.UUID(str(run_id))
        except ValueError:
            raise ApiError("NOT_FOUND") from None
        metadata = conn.execute(
            """SELECT c.request,c.provenance,r.expires_at<=now() AS expired
            FROM contract_runs c JOIN runs r ON r.id=c.run_id
            WHERE r.id=%s AND r.session_id=%s""",
            (identifier, sid),
        ).fetchone()
        if not metadata:
            raise ApiError("NOT_FOUND")
        if metadata["expired"]:
            raise ApiError("RUN_EXPIRED")
        return {**get_run(conn, sid, run_id), **metadata}

    @router.get("/health")
    def health():
        return {
            "status": "ok",
            "app_version": __version__,
            "contracts_version": "1.0.0",
            "source_revision": settings.contracts_source_revision,
        }

    @router.get("/ready")
    def ready():
        with db.connection() as conn:
            conn.execute("SELECT 1 FROM contract_runs LIMIT 1")
            if not conn.execute(
                "SELECT 1 FROM documents WHERE tenant_id='demo' AND lifecycle='active' LIMIT 1"
            ).fetchone():
                raise ApiError("SERVICE_UNAVAILABLE")
        return {"status": "ready", "fixtures_version": FIXTURES_VERSION}

    @router.get("/identity")
    def identity():
        return wire.verified(
            "project-identity",
            {
                "schema_version": "1.0",
                "project_id": "knowledgeops",
                "origin": "https://portfolio.gappazappa.in",
                "case_study_route": "/projects/knowledgeops",
                "api_prefix": wire.PREFIX,
                "app_route": None,
            },
        )

    @router.get("/simulation/schema")
    def schema():
        return wire.schema(settings)

    @router.get("/simulation/presets")
    def presets():
        return wire.presets()

    @router.get("/portfolio-evidence")
    def portfolio_evidence():
        path = ROOT / "portfolio-evidence.contracts-v1.json"
        if not path.exists():
            raise ApiError("NOT_FOUND")
        return wire.verified("portfolio-evidence", json.loads(path.read_text()))

    @router.post("/simulation/session", status_code=201)
    def bootstrap(request: Request):
        body(request)
        if request.headers.get("origin") != settings.public_origin:
            raise ApiError("FORBIDDEN")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        sid = digest(token)
        ip_hash = digest(
            settings.public_origin + (request.client.host if request.client else "unknown")
        )
        ttl = min(3600, settings.session_ttl_seconds)
        with db.connection() as conn:
            rate_limit(conn, f"contract-session:{ip_hash}", 30)
            rate_limit(conn, "contract-session:global", 200)
            conn.execute(
                "INSERT INTO sessions(id,csrf_hash,ip_hash,expires_at) VALUES (%s,%s,%s,now()+(%s * interval '1 second'))",
                (sid, digest(csrf), ip_hash, ttl),
            )
            conn.execute("INSERT INTO contract_sessions(session_id) VALUES (%s)", (sid,))
        response = JSONResponse(
            {"session_token": token, "csrf_token": csrf, "expires_in": ttl}, status_code=201
        )
        response.set_cookie(
            "ko_v1_session",
            token,
            max_age=ttl,
            httponly=True,
            secure=settings.secure_cookies,
            samesite="strict",
            path=wire.PREFIX,
        )
        return response

    @router.post("/simulations", status_code=202)
    def start(request: Request, sid=Depends(session)):
        payload = body(request, "simulation-start")
        if payload["project_id"] != "knowledgeops":
            raise ApiError("FORBIDDEN")
        if payload["mode"] not in {"SANDBOX_DETERMINISTIC", "REPLAY_RECORDED"}:
            raise ApiError("FORBIDDEN")
        key = request.headers.get("idempotency-key", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", key):
            raise ApiError("VALIDATION_ERROR")
        # Seed has one authority: the canonical envelope, never a second nested value.
        if "seed" in payload["input"]:
            raise ApiError("VALIDATION_ERROR")
        extensions = payload.get("extensions", {})
        if set(extensions) - {"x_replay_run_id"}:
            raise ApiError("VALIDATION_ERROR")
        replay_id = extensions.get("x_replay_run_id")
        if (payload["mode"] == "REPLAY_RECORDED") != bool(replay_id):
            raise ApiError("VALIDATION_ERROR")
        try:
            domain = SimulationStart(
                mode=payload["mode"],
                preset_id=payload.get("preset_id"),
                input=SearchInput(**payload["input"], seed=payload["seed"]),
                replay_run_id=replay_id,
            )
        except (ValidationError, TypeError):
            raise ApiError("VALIDATION_ERROR") from None
        if domain.preset_id and domain.preset_id not in {
            p["preset_id"] for p in wire.presets()["presets"]
        }:
            raise ApiError("VALIDATION_ERROR")
        with db.connection() as conn:
            # Do not attach new provenance to an existing legacy run, even if a caller
            # reused the same session token/key through the legacy interface.
            conn.execute("SELECT pg_advisory_xact_lock(704401)")
            legacy = conn.execute(
                """SELECT r.id FROM runs r LEFT JOIN contract_runs c ON c.run_id=r.id
                WHERE r.session_id=%s AND r.idempotency_key=%s AND c.run_id IS NULL""",
                (sid, key),
            ).fetchone()
            if legacy:
                raise ApiError("CONFLICT")
            prov = wire.provenance(settings, domain.mode)
            if replay_id:
                source = owned(conn, sid, replay_id)
                if (
                    source["status"] != "SUCCEEDED"
                    or source["input"]["input"] != domain.input.model_dump()
                ):
                    raise ApiError("CONFLICT")
                prov = {
                    **source["provenance"],
                    "data_origin": "RECORDED_SYNTHETIC",
                    "connector_kind": "REPLAY",
                }
            run = enqueue(conn, sid, domain, key, settings)
            prior = conn.execute(
                "SELECT request FROM contract_runs WHERE run_id=%s", (run["id"],)
            ).fetchone()
            if prior and prior["request"] != payload:
                raise ApiError("CONFLICT")
            if not prior:
                conn.execute(
                    "INSERT INTO contract_runs(run_id,request,provenance) VALUES (%s,%s,%s)",
                    (run["id"], Jsonb(payload), Jsonb(prov)),
                )
            return wire.run_payload(owned(conn, sid, run["id"]))

    @router.get("/simulations/{run_id}")
    def read_run(run_id: str, sid=Depends(session)):
        with db.connection() as conn:
            return wire.run_payload(owned(conn, sid, run_id))

    @router.get("/simulations/{run_id}/result")
    def result(run_id: str, sid=Depends(session)):
        with db.connection() as conn:
            run = owned(conn, sid, run_id)
            if run["status"] in wire.ACTIVE:
                raise ApiError("CONFLICT")
            return wire.result_payload(run)

    @router.post("/simulations/{run_id}/cancel")
    def cancel(request: Request, run_id: str, sid=Depends(session)):
        body(request)
        with db.connection() as conn:
            owned(conn, sid, run_id)
            conn.execute(
                "UPDATE runs SET status='CANCELLED',claim_epoch=claim_epoch+1,finished_at=now(),lease_until=NULL WHERE id=%s AND status IN ('ACCEPTED','RUNNING')",
                (run_id,),
            )
            return wire.run_payload(owned(conn, sid, run_id))

    def event_page(sid, run_id, after):
        with db.connection() as conn:
            run = owned(conn, sid, run_id)
            if after > run["event_count"]:
                raise ApiError("INVALID_CURSOR")
            rows = conn.execute(
                "SELECT payload FROM events WHERE run_id=%s AND sequence>%s ORDER BY sequence LIMIT 128",
                (run_id, after),
            ).fetchall()
            events = [wire.event_payload(row["payload"], run) for row in rows]
            return wire.verified(
                "simulation-events",
                {
                    "schema_version": "1.0",
                    "run_id": run_id,
                    "events": events,
                    "next_sequence": events[-1]["sequence"] if events else after,
                    "terminal": run["status"] not in wire.ACTIVE,
                },
            )

    @router.get("/simulations/{run_id}/events")
    async def events(request: Request, run_id: str, sid=Depends(session)):
        # Authorize before interpreting even malformed cursors for an unknown run.
        await asyncio.to_thread(event_page, sid, run_id, 0)
        cursors = [
            x
            for x in [request.query_params.get("after"), request.headers.get("last-event-id")]
            if x is not None
        ]
        if (
            any(not re.fullmatch(r"[0-9]{1,4}", c) for c in cursors)
            or len({int(c) for c in cursors}) > 1
        ):
            raise ApiError("INVALID_CURSOR")
        cursor = int(cursors[0]) if cursors else 0
        initial = await asyncio.to_thread(event_page, sid, run_id, cursor)
        if "text/event-stream" not in request.headers.get("accept", ""):
            return JSONResponse(initial)
        if streams.get(sid, 0) >= 2 or sum(streams.values()) >= 16:
            raise ApiError("RATE_LIMITED")
        streams[sid] = streams.get(sid, 0) + 1

        async def stream():
            position, size, started, heartbeat = cursor, 0, time.monotonic(), 0.0
            try:
                while time.monotonic() - started < 10:
                    if await request.is_disconnected():
                        break
                    # Recheck session, TTL and document revocations before every batch.
                    batch = await asyncio.to_thread(event_page, sid, run_id, position)
                    for event in batch["events"]:
                        frame = (
                            f"id: {event['sequence']}\nevent: step\ndata: {json.dumps(event)}\n\n"
                        )
                        if size + len(frame.encode()) > 260000:
                            return
                        size += len(frame.encode())
                        yield frame
                        position = event["sequence"]
                    if batch["terminal"]:
                        yield (
                            "event: end\ndata: " + json.dumps({"last_sequence": position}) + "\n\n"
                        )
                        return
                    if time.monotonic() - heartbeat >= 5:
                        yield ": heartbeat\n\n"
                        heartbeat = time.monotonic()
                    await asyncio.sleep(0.25)
            except (ApiError, JobError, DatabaseError, PoolTimeout, ContractError):
                # Headers already sent: close without end; clients must reauthenticate/poll.
                return
            finally:
                streams[sid] -= 1
                if not streams[sid]:
                    del streams[sid]

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @router.post("/knowledge/query")
    def knowledge_query(request: Request):
        principal = service_principal(request, settings)
        query = body(request, "knowledge-query")
        response = {
            "schema_version": "1.0",
            "status": "DENIED",
            "answer": None,
            "citations": [],
            "correlation_id": query["correlation_id"],
            "mode": query["mode"],
            "limitations": ["The request is outside the authenticated grant."],
        }
        with db.connection() as conn:
            rate_limit(conn, f"knowledge:{principal.token_sha256}", 60)
            rate_limit(conn, "knowledge:global", 240)
            allowed = (
                principal.application_id == query["application_id"]
                and principal.tenant_id == query["tenant_id"]
                and principal.environment == settings.contracts_environment
                and principal.audience == "knowledgeops"
                and "knowledge:read" in principal.scopes
                and set(query["collection_ids"]) <= set(principal.collection_ids)
                and query["mode"] == "SANDBOX_DETERMINISTIC"
            )
            if not allowed:
                return wire.verified("knowledge-response", response)
            try:
                # FlowOps's fixed synthetic policy query needs exact revision matching.
                # Keep it an additional SQL passage filter after the same ACL/tenant checks.
                revision_filter = ""
                if principal.application_id == "flowops" and re.fullmatch(
                    r"Synthetic refund policy [A-Za-z0-9_-]{1,32}", query["query"]
                ):
                    revision_filter = query["query"] + "."
                search = SearchInput(
                    question=query["query"],
                    role=principal.role,
                    collections=query["collection_ids"],
                    top_k=query["max_results"],
                    keyword_filter=revision_filter,
                )
            except ValidationError:
                raise ApiError("VALIDATION_ERROR") from None
            output = retrieve(conn, search, principal.tenant_id)
        response.update(
            status="NO_RESULTS", limitations=["No answer supported by the authorized evidence."]
        )
        if output["abstention"] or not output["citations"]:
            return wire.verified("knowledge-response", response)
        citations = []
        # Bound the answer using the same returned passages, including newline separators.
        excerpt_limit = min(
            query["max_excerpt_chars"],
            (3000 - len(output["citations"])) // len(output["citations"]),
        )
        for citation in output["citations"]:
            passage = citation["quote"][:excerpt_limit]
            citations.append(
                {
                    "source_id": citation["document_id"],
                    "title": citation["title"],
                    "passage": passage,
                    "location": f"version {citation['version']}; chunk {citation['chunk_id']}; page {citation['page']}",
                    "evidence": wire.evidence(citation, passage),
                }
            )
        response.update(
            status="OK",
            answer="\n".join(c["passage"] for c in citations),
            citations=citations,
            limitations=[
                "Extractive synthetic passages; no generative model or external provider.",
                "Excerpt hashes cover exactly the returned text; hosting cost is unallocated.",
            ],
        )
        return wire.verified("knowledge-response", response)

    # Canonical internal paths and explicit prefix aliases permit integration rehearsals.
    app.include_router(router)
    app.include_router(router, prefix=wire.PREFIX, include_in_schema=False)

    @app.get("/openapi.json", include_in_schema=False)
    @app.get(wire.PREFIX + "/openapi.json", include_in_schema=False)
    def openapi():
        from knowledgeops.contracts_openapi import specification

        return specification()

    return app
