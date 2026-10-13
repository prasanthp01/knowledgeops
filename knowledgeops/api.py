import asyncio
import hashlib
import json
import re
import secrets
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout
from starlette.middleware.trustedhost import TrustedHostMiddleware

from knowledgeops import __version__
from knowledgeops.config import ROOT, Settings
from knowledgeops.db import Database
from knowledgeops.jobs import JobError, enqueue, get_run, rate_limit
from knowledgeops.models import FIXTURES_VERSION, PrivateInput, SimulationStart
from knowledgeops.private_ai import answer

PREFIX = "/api/projects/knowledgeops"
PAGE = "/projects/knowledgeops"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def safe_validation_fields(errors):
    # Locations include request-controlled JSON keys. Expose only known schema names,
    # never raw keys, input values, validator messages, or arbitrary array offsets.
    fields = set(SimulationStart.model_fields) | set(PrivateInput.model_fields)
    from knowledgeops.models import SearchInput

    tree: dict = {"body": {name: {} for name in fields}, "header": {"idempotency-key": {}}}
    tree["body"]["input"] = {name: {} for name in SearchInput.model_fields}
    tree["query"] = {"after": {}, "format": {}}
    paths = []
    for error in errors[:16]:
        node, parts = tree, []
        for part in error["loc"][:5]:
            if isinstance(part, int):
                parts.append("item")
                break
            if part not in node:
                parts.append("unknown")
                break
            parts.append(part)
            node = node[part]
        paths.append(".".join(parts)[:96])
    return list(dict.fromkeys(paths))


def create_app(settings=None, database=None):
    settings = settings or Settings()
    db = database or Database(settings.database_url)

    @asynccontextmanager
    async def lifespan(_):
        if database is None:
            db.open()
        yield
        if database is None:
            db.close()

    app = FastAPI(
        title="KnowledgeOps",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=PREFIX + "/openapi.json",
        lifespan=lifespan,
    )
    app.state.db, app.state.settings = db, settings
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=[urlparse(settings.public_origin).hostname]
    )
    streams: dict[str, int] = {}

    @app.exception_handler(JobError)
    async def job_error(_, exc):
        return JSONResponse(
            {"error": {"code": exc.code}},
            status_code=exc.status,
            headers={"Retry-After": "60"} if exc.status == 429 else None,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_, exc):
        return JSONResponse(
            {
                "error": {
                    "code": "INVALID_INPUT",
                    "fields": safe_validation_fields(exc.errors()),
                }
            },
            status_code=422,
        )

    @app.exception_handler(DatabaseError)
    @app.exception_handler(PoolTimeout)
    async def database_error(_, exc):
        return JSONResponse({"error": {"code": "DEPENDENCY_UNAVAILABLE"}}, status_code=503)

    @app.middleware("http")
    async def boundaries(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"error": {"code": "JSON_REQUIRED"}}, status_code=415)
            size, parts = 0, []
            async for part in request.stream():
                size += len(part)
                if size > 16384:
                    return JSONResponse({"error": {"code": "PAYLOAD_TOO_LARGE"}}, status_code=413)
                parts.append(part)
            request._body = b"".join(parts)
            if not request.url.path.startswith(PREFIX + "/private/"):
                if request.headers.get("origin") != settings.public_origin:
                    return JSONResponse({"error": {"code": "ORIGIN_REJECTED"}}, status_code=403)
        try:
            response = await call_next(request)
        except Exception:
            response = JSONResponse({"error": {"code": "INTERNAL_ERROR"}}, status_code=500)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'; form-action 'self'"
        )
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cache-Control"] = "no-store"
        if settings.secure_cookies:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    def session(request: Request):
        token = request.cookies.get("ko_session", "")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{43}", token):
            raise JobError("SESSION_REQUIRED", 401)
        sid = digest(token)
        with db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id=%s AND expires_at>now()", (sid,)
            ).fetchone()
            if not row:
                raise JobError("SESSION_REQUIRED", 401)
            rate_limit(conn, f"read:{sid}", 240)
            if request.method != "GET" and not secrets.compare_digest(
                digest(request.headers.get("x-csrf-token", "")), row["csrf_hash"]
            ):
                raise JobError("CSRF_REJECTED", 403)
        return sid

    @app.get(PREFIX + "/health")
    def health():
        return {"status": "ok", "app_version": __version__}

    @app.get(PREFIX + "/ready")
    def ready():
        with db.connection() as conn:
            conn.execute("SELECT 1 FROM runs LIMIT 1")
            corpus = conn.execute(
                "SELECT 1 FROM documents WHERE tenant_id='demo' LIMIT 1"
            ).fetchone()
        if not corpus:
            raise JobError("CORPUS_NOT_READY", 503)
        return {"status": "ready", "fixtures_version": FIXTURES_VERSION}

    @app.post(PREFIX + "/session", status_code=201)
    def create_session(request: Request):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        ip_hash = digest(
            settings.public_origin + (request.client.host if request.client else "unknown")
        )
        with db.connection() as conn:
            rate_limit(conn, f"session:{ip_hash}", 30)
            rate_limit(conn, "session:global", 200)
            conn.execute(
                "INSERT INTO sessions(id,csrf_hash,ip_hash,expires_at) VALUES (%s,%s,%s,now()+(%s * interval '1 second'))",
                (digest(token), digest(csrf), ip_hash, settings.session_ttl_seconds),
            )
        response = JSONResponse(
            {"csrf_token": csrf, "expires_in": settings.session_ttl_seconds}, status_code=201
        )
        response.set_cookie(
            "ko_session",
            token,
            max_age=settings.session_ttl_seconds,
            httponly=True,
            secure=settings.secure_cookies,
            samesite="strict",
            path=PREFIX,
        )
        return response

    @app.get(PREFIX + "/simulation/schema")
    def schema():
        return SimulationStart.model_json_schema()

    @app.get(PREFIX + "/simulation/presets")
    def presets():
        return json.loads((ROOT / "data/presets.json").read_text())

    @app.post(PREFIX + "/simulations", status_code=202)
    def start(
        body: SimulationStart,
        sid=Depends(session),
        idempotency_key: str = Header(
            ..., min_length=8, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$"
        ),
    ):
        allowed = {p["id"] for p in presets()}
        if body.preset_id and body.preset_id not in allowed:
            raise JobError("INVALID_PRESET", 422)
        with db.connection() as conn:
            run = enqueue(conn, sid, body, idempotency_key, settings)
        return public_run(run)

    def public_run(run):
        return {
            "schema_version": "1.0",
            "project_id": "knowledgeops",
            "run_id": str(run["id"]),
            "status": run["status"],
            "mode": run["mode"],
            "created_at": run["created_at"].isoformat(),
            "completed_at": run["finished_at"].isoformat() if run["finished_at"] else None,
            "event_count": run.get("event_count", 0),
            "app_version": __version__,
            "fixtures_version": FIXTURES_VERSION,
            "connector_kind": "synthetic_postgresql",
            "expires_at": run["expires_at"].isoformat(),
            "error_code": run["error_code"],
            "result_ref": PREFIX + f"/simulations/{run['id']}/result",
            "result_reference": PREFIX + f"/simulations/{run['id']}/result",
        }

    @app.get(PREFIX + "/simulations/{run_id}")
    def read_run(run_id: str, sid=Depends(session)):
        with db.connection() as conn:
            return public_run(get_run(conn, sid, run_id))

    @app.get(PREFIX + "/simulations/{run_id}/result")
    def result(run_id: str, sid=Depends(session)):
        with db.connection() as conn:
            run = get_run(conn, sid, run_id)
            if run["status"] != "SUCCEEDED":
                raise JobError("RESULT_NOT_READY")
            return run["result"]

    @app.post(PREFIX + "/simulations/{run_id}/cancel")
    def cancel(run_id: str, sid=Depends(session)):
        with db.connection() as conn:
            get_run(conn, sid, run_id)
            conn.execute(
                "UPDATE runs SET status='CANCELLED',claim_epoch=claim_epoch+1,finished_at=now() WHERE id=%s AND status IN ('ACCEPTED','RUNNING')",
                (run_id,),
            )
            return public_run(get_run(conn, sid, run_id))

    def read_events(sid, run_id, after):
        with db.connection() as conn:
            run = get_run(conn, sid, run_id)
            events = conn.execute(
                "SELECT payload FROM events WHERE run_id=%s AND sequence>%s ORDER BY sequence LIMIT 100",
                (run_id, after),
            ).fetchall()
            return {"events": [e["payload"] for e in events], "status": run["status"]}

    @app.get(PREFIX + "/simulations/{run_id}/events")
    async def events(
        request: Request,
        run_id: str,
        after: int = Query(default=0, ge=0, le=10000),
        format: str = Query(default="sse", pattern="^(sse|json)$"),
        sid=Depends(session),
    ):
        initial = await asyncio.to_thread(read_events, sid, run_id, after)
        if format == "json":
            return JSONResponse(initial)
        if streams.get(sid, 0) >= 2 or sum(streams.values()) >= 16:
            raise JobError("STREAM_LIMIT", 429)
        last_id = request.headers.get("last-event-id", "")
        if last_id:
            try:
                after = max(after, min(10000, max(0, int(last_id))))
            except ValueError:
                raise JobError("INVALID_EVENT_CURSOR", 422) from None
        streams[sid] = streams.get(sid, 0) + 1

        async def stream():
            cursor = after
            try:
                for _ in range(40):
                    if await request.is_disconnected():
                        break
                    try:
                        batch = await asyncio.to_thread(read_events, sid, run_id, cursor)
                    except JobError as exc:
                        yield "event: blocked\ndata: " + json.dumps({"code": exc.code}) + "\n\n"
                        break
                    for event in batch["events"]:
                        cursor = event["sequence"]
                        yield f"id: {cursor}\nevent: step\ndata: {json.dumps(event)}\n\n"
                    if batch["status"] not in {"ACCEPTED", "RUNNING"}:
                        yield (
                            "event: done\ndata: " + json.dumps({"status": batch["status"]}) + "\n\n"
                        )
                        break
                    yield ": heartbeat\n\n"
                    await asyncio.sleep(0.25)
            finally:
                streams[sid] -= 1
                if streams[sid] == 0:
                    del streams[sid]

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
        )

    @app.post(PREFIX + "/private/answers")
    def private_answer(body: PrivateInput, authorization: str = Header(default="")):
        if not settings.private_token or not secrets.compare_digest(
            authorization, "Bearer " + settings.private_token
        ):
            raise JobError("PRIVATE_AUTH_REQUIRED", 401)
        return answer(db, settings, body)

    @app.get(PAGE, include_in_schema=False)
    @app.get(PAGE + "/", include_in_schema=False)
    def page():
        return FileResponse(ROOT / "web/index.html")

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse(PAGE)

    @app.get(PAGE + "/evidence.json", include_in_schema=False)
    def evidence():
        path = ROOT / "portfolio-evidence.json"
        return (
            FileResponse(path)
            if path.exists()
            else JSONResponse({"status": "PLANNED", "benchmarks": []})
        )

    app.mount(PAGE + "/assets", StaticFiles(directory=ROOT / "web"), name="assets")
    return app


app = create_app()
