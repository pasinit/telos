"""Internal API that Cardea's kernel calls while it runs a mirrored Telos routine.

Cardea owns the execution of routines mirrored by :mod:`telos.cardea_bridge`; the
``telos-cardea`` operations package reaches back here for the continuity context
Telos would inject into its own routine runs and for the agent's property ledger.

The API is a separate ASGI app served by a second uvicorn server inside the Telos
process, on its own port (:data:`INTERNAL_PORT`), so it can be published on the
private network without exposing the public app. It serves only
``/internal/cardea/v1/*`` and is off unless ``TELOS_INTERNAL_API=1``.

Every request carries ``Authorization: Bearer <JWT-SVID>`` minted by SPIRE for
audience ``telos-internal``. The signature, audience and expiry are checked
against the JWT bundle of trust domain ``cardea.internal``, read from the local
SPIRE agent's Workload API and cached (see :mod:`telos.api.spiffe_auth`); the
subject must equal ``TELOS_INTERNAL_ALLOWED_SUB``.

Scope comes from ``X-Cardea-Namespace: telos-<user>`` and ``X-Cardea-Agent:
telos-<slug>`` (set by the operations package from Cardea's operation context,
never from model-chosen tool arguments). An agent without an enabled routine
that the bridge mirrors (its Cardea mode is shadow or live and it has a trigger
spec) is a 404.

Environment:

- ``TELOS_INTERNAL_API``: ``1`` to serve (default off).
- ``TELOS_INTERNAL_API_HOST``: bind address (default ``0.0.0.0``; inside the
  container, Docker's published port decides what is reachable).
- ``TELOS_INTERNAL_ALLOWED_SUB``: the only SPIFFE ID allowed to call.
- ``TELOS_SPIFFE_ENDPOINT_SOCKET``: Workload API address (default
  ``unix:///run/spire/agent.sock``).
- ``TELOS_INTERNAL_RECALL_TIMEOUT``: seconds the routine context waits for the
  reserved memory recall before disclosing it as unavailable (default 15; Cardea
  gives a context call 30 s in all).

Each request logs one INFO line on ``telos.internal_cardea``: method, route
template, caller SPIFFE ID, status, duration and error code; never query
strings, bodies or path parameters.
"""
import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import APIRouter, Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import HttpUrl, TypeAdapter, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from telos.api.spiffe_auth import BundleUnavailableError, InvalidSvidError, NotReadyError, SvidVerifier, WorkloadApiKeys
from telos.cardea_bridge import BridgeConfig, identifier, trigger_spec
from telos.core.persistence import Database
from telos.core.repos.property_candidates import PropertyCandidateRepo
from telos.core.repos.routines import Routine
from telos.domain.tools.property_candidates import PropertyAssessmentInput, canonical_listing_url
from telos.local_time import local_now, local_timezone
from telos.routine_invocation import RoutineInvocation
from telos.services.routine_context import (
    RecallFn,
    delivery_history_block,
    history_path,
    ledger_summary_block,
    reserved_recall,
    reserved_recall_block,
)

log = logging.getLogger("telos.internal_cardea")

INTERNAL_PORT = 7445
AUDIENCE = "telos-internal"
TRUST_DOMAIN = "cardea.internal"
DEFAULT_ALLOWED_SUB = "spiffe://cardea.internal/runtime/kernel"
DEFAULT_WORKLOAD_SOCKET = "unix:///run/spire/agent.sock"
MAX_IDEMPOTENCY_KEY = 128
MAX_LEDGER_PAGE = 200
DEFAULT_RECALL_TIMEOUT = 15.0

_listing_url: TypeAdapter[HttpUrl] = TypeAdapter(HttpUrl)


@dataclass(frozen=True)
class InternalApiDeps:
    """What the internal API reads; bound by the lifespan in :mod:`telos.api.app`."""

    routines: Callable[[], list[Routine]]
    agent_tools: Callable[[str, str], list[str]]
    storage_root: Path
    get_user_email: Callable[[str], str | None]
    recall_fn: RecallFn | None
    verifier: SvidVerifier
    allowed_sub: str = DEFAULT_ALLOWED_SUB
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    recall_timeout: float = DEFAULT_RECALL_TIMEOUT


@dataclass(frozen=True)
class AgentScope:
    user_id: str
    agent_slug: str
    history: Path


class ApiError(HTTPException):
    """An HTTP error whose JSON body carries a stable machine-readable ``code`` next to ``detail``.

    Clients match on ``code``; ``detail`` is human-readable and may change.
    """

    def __init__(self, status_code: int, code: str, detail: Any, headers: dict[str, str] | None = None) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code


def _invalid(code: str, detail: Any) -> ApiError:
    return ApiError(422, code, detail)


async def _api_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = exc.code if isinstance(exc, ApiError) else "not_found" if exc.status_code == 404 else f"http_{exc.status_code}"
    request.state.error_code = code
    return JSONResponse({"detail": exc.detail, "code": code}, status_code=exc.status_code, headers=exc.headers)


async def _request_invalid(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    request.state.error_code = "invalid_request"
    return JSONResponse({"detail": jsonable_encoder(exc.errors()), "code": "invalid_request"}, status_code=422)


class RequestLog:
    """ASGI middleware: one INFO line per request, with no query string, body or path parameters."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        status = 500
        state = scope.setdefault("state", {})

        async def capture(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        except Exception:
            state.setdefault("error_code", "internal_error")
            raise
        finally:
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            log.info(
                "internal cardea request method=%s route=%s sub=%s status=%d duration_ms=%d code=%s",
                scope["method"], route, state.get("subject", "-"), status,
                round((time.monotonic() - started) * 1000), state.get("error_code", "-"),
            )


def _slot_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        slot = datetime.fromisoformat(raw)
    except ValueError:
        raise _invalid("invalid_slot_time", "X-Cardea-Slot-Time must be an ISO-8601 timestamp with offset") from None
    if slot.tzinfo is None:
        raise _invalid("invalid_slot_time", "X-Cardea-Slot-Time must include a UTC offset")
    return slot


def create_internal_app(deps: InternalApiDeps) -> FastAPI:
    """Build the internal ASGI app serving only ``/internal/cardea/v1/*``.

    Error bodies are ``{"detail": ..., "code": ...}``; clients match on ``code``:
    401 ``missing_token`` / ``invalid_token``, 403 ``subject_not_allowed``,
    503 ``not_ready`` (no trust bundle yet, e.g. right after a restart) /
    ``bundle_unavailable``, 400 ``missing_scope``, 404 ``no_mirrored_routine`` /
    ``assessment_not_found`` / ``not_found``, 422 ``invalid_request`` (query/body
    shape), ``invalid_slot_time``, ``invalid_url``, ``invalid_idempotency_key``,
    ``invalid_assessment`` or ``ledger_rule``.
    """

    async def authenticate(request: Request, authorization: str | None = Header(default=None)) -> None:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise ApiError(401, "missing_token", "Bearer JWT-SVID required", headers={"WWW-Authenticate": "Bearer"})
        try:
            subject = await asyncio.to_thread(deps.verifier.verify, token.strip())
        except InvalidSvidError as exc:
            log.warning("internal cardea API rejected a token: %s", exc)
            raise ApiError(401, "invalid_token", "Invalid JWT-SVID", headers={"WWW-Authenticate": "Bearer"}) from None
        except NotReadyError as exc:
            log.warning("internal cardea API not ready: %s", exc)
            raise ApiError(503, "not_ready", "Trust bundle not loaded yet", headers={"Retry-After": "2"}) from None
        except BundleUnavailableError as exc:
            log.error("internal cardea API cannot validate tokens: %s", exc)
            raise ApiError(503, "bundle_unavailable", "Trust bundle unavailable", headers={"Retry-After": "10"}) from None
        request.state.subject = subject
        if subject != deps.allowed_sub:
            log.warning("internal cardea API refused subject %s", subject)
            raise ApiError(403, "subject_not_allowed", "Subject not allowed")

    def agent_scope(
        namespace: str | None = Header(default=None, alias="X-Cardea-Namespace"),
        agent: str | None = Header(default=None, alias="X-Cardea-Agent"),
    ) -> AgentScope:
        if not namespace or not agent:
            raise ApiError(400, "missing_scope", "X-Cardea-Namespace and X-Cardea-Agent are required")
        # Match on the exact identifiers the bridge put in the trigger template,
        # so a scope can only name an agent Telos itself mirrored.
        for routine in deps.routines():
            if (identifier(f"telos-{routine.user_id}") == namespace and identifier(f"telos-{routine.agent_slug}") == agent
                    and trigger_spec(routine, deps.agent_tools(routine.user_id, routine.agent_slug), deps.bridge) is not None):
                return AgentScope(routine.user_id, routine.agent_slug,
                                  history_path(deps.storage_root, routine.user_id, routine.agent_slug))
        raise ApiError(404, "no_mirrored_routine", "No mirrored routine for this agent")

    def existing_ledger(scope: AgentScope) -> PropertyCandidateRepo | None:
        """The agent's ledger, without creating an empty one for read-only calls."""
        if not scope.history.exists() or not PropertyCandidateRepo.exists(Database(scope.history)):
            return None
        return PropertyCandidateRepo(Database(scope.history))

    router = APIRouter(prefix="/internal/cardea/v1", dependencies=[Depends(authenticate)])

    @router.get("/routine-context")
    async def routine_context(
        scope: AgentScope = Depends(agent_scope),
        slot_header: str | None = Header(default=None, alias="X-Cardea-Slot-Time"),
    ) -> dict:
        slot = _slot_time(slot_header)
        invocation = RoutineInvocation(slot.astimezone(local_timezone()) if slot else local_now()).metadata()
        recipient = await asyncio.to_thread(deps.get_user_email, scope.user_id) if scope.history.exists() else None
        try:
            recall = await asyncio.wait_for(reserved_recall(deps.recall_fn, scope.user_id, scope.agent_slug), deps.recall_timeout)
        except TimeoutError:
            # Cardea gives the whole context call 30 s; disclose the gap rather than time out.
            log.warning("routine context recall for %s/%s timed out after %.0fs", scope.user_id, scope.agent_slug, deps.recall_timeout)
            recall = {"records": [], "errors": [{"error": f"memory recall timed out after {deps.recall_timeout:.0f}s"}]}
        return {
            "invocation": {"reporting_date": invocation["reporting_date"], "timezone": invocation["timezone"],
                           "slot_time": slot.isoformat() if slot else None},
            "delivery_history": await delivery_history_block(scope.history, recipient),
            "ledger_summary": await asyncio.to_thread(ledger_summary_block, scope.history),
            "reserved_recall": reserved_recall_block(recall),
        }

    @router.get("/ledger")
    def ledger(
        scope: AgentScope = Depends(agent_scope),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=MAX_LEDGER_PAGE),
    ) -> dict:
        repo = existing_ledger(scope)
        if repo is None:
            return {"items": [], "next_offset": None}
        page = repo.page(offset=offset, limit=limit, max_limit=MAX_LEDGER_PAGE)
        return {"items": page["records"], "next_offset": offset + len(page["records"]) if page["has_more"] else None}

    @router.get("/ledger/history")
    def ledger_history(
        url: str,
        scope: AgentScope = Depends(agent_scope),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=20, ge=1, le=20),
    ) -> dict:
        try:
            canonical = canonical_listing_url(_listing_url.validate_python(url))
        except (ValidationError, ValueError) as exc:
            raise _invalid("invalid_url", f"url: {exc}") from None
        repo = existing_ledger(scope)
        return {"history": repo.history(canonical, offset=offset, limit=limit) if repo else []}

    @router.post("/ledger/assessments")
    def record_assessment(
        body: dict = Body(...),
        scope: AgentScope = Depends(agent_scope),
        dry_run: bool = Query(default=False),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        if not dry_run and not idempotency_key:
            raise _invalid("invalid_idempotency_key", "Idempotency-Key header is required")
        if idempotency_key is not None and not 0 < len(idempotency_key) <= MAX_IDEMPOTENCY_KEY:
            raise _invalid("invalid_idempotency_key", f"Idempotency-Key must be 1-{MAX_IDEMPOTENCY_KEY} characters")
        # A replay returns the original outcome even if the body would no longer
        # validate (e.g. "verified" evidence that has since aged past a day).
        if not dry_run and idempotency_key and (repo := existing_ledger(scope)) and (prior := repo.by_key(idempotency_key)):
            return JSONResponse({"recorded": True, "assessment_id": str(prior), "duplicate": True}, status_code=201)
        try:
            inp = PropertyAssessmentInput.model_validate(body)
            url = canonical_listing_url(inp.url)
        except ValidationError as exc:
            raise _invalid("invalid_assessment", exc.errors(include_url=False, include_context=False)) from None
        except ValueError as exc:
            raise _invalid("invalid_assessment", str(exc)) from None
        assessment = inp.model_dump(mode="json")
        try:
            if dry_run:
                if (repo := existing_ledger(scope)) is not None:
                    repo.check(url, assessment)
                return JSONResponse({"recorded": False, "valid": True}, status_code=200)
            assessment_id, _, duplicate = PropertyCandidateRepo(Database(scope.history)).record_keyed(
                url, assessment, idempotency_key=idempotency_key)
        except ValueError as exc:
            raise _invalid("ledger_rule", str(exc)) from None
        return JSONResponse({"recorded": True, "assessment_id": str(assessment_id), "duplicate": duplicate}, status_code=201)

    @router.get("/ledger/assessments/by-key/{key}")
    def assessment_by_key(key: str, scope: AgentScope = Depends(agent_scope)) -> dict:
        repo = existing_ledger(scope)
        assessment_id = repo.by_key(key) if repo else None
        if assessment_id is None:
            raise ApiError(404, "assessment_not_found", "No assessment recorded under this key")
        return {"assessment_id": str(assessment_id)}

    app = FastAPI(title="telos-internal-cardea", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_exception_handler(StarletteHTTPException, _api_error)
    app.add_exception_handler(RequestValidationError, _request_invalid)
    app.include_router(router)
    app.add_middleware(RequestLog)
    return app


class _EmbeddedServer(uvicorn.Server):
    """A uvicorn server that leaves signal handling to the main Telos server."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class InternalApiServer:
    """Runs the internal app on the current event loop next to the main server."""

    def __init__(self, app: FastAPI, *, host: str, port: int = INTERNAL_PORT, warm: Callable[[], object] | None = None) -> None:
        self._server = _EmbeddedServer(uvicorn.Config(app, host=host, port=port, lifespan="off", access_log=False, log_config=None))
        self._task: asyncio.Task | None = None
        # Loads the trust bundle at startup so the first Cardea call does not pay for it.
        self._warm = warm
        self._warm_task: asyncio.Future | None = None

    @property
    def started(self) -> bool:
        return self._server.started

    async def _serve(self) -> None:
        try:
            await self._server.serve()
        except SystemExit:
            # uvicorn exits the process when it cannot bind; keep Telos itself up.
            log.error("internal cardea API failed to start on %s:%s", self._server.config.host, self._server.config.port)

    def start(self) -> None:
        if self._warm is not None:
            self._warm_task = asyncio.ensure_future(asyncio.to_thread(self._warm))
        self._task = asyncio.create_task(self._serve())
        log.info("internal cardea API listening on %s:%s", self._server.config.host, self._server.config.port)

    async def stop(self) -> None:
        self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, timeout=10)


def internal_api_from_env(
    *,
    routines: Callable[[], list[Routine]],
    agent_tools: Callable[[str, str], list[str]],
    storage_root: Path,
    get_user_email: Callable[[str], str | None],
    recall_fn: RecallFn | None,
) -> InternalApiServer | None:
    """The internal API server when ``TELOS_INTERNAL_API=1``, else None. Call :meth:`InternalApiServer.start` on the loop."""
    if os.getenv("TELOS_INTERNAL_API", "").strip() != "1":
        return None
    keys = WorkloadApiKeys(os.getenv("TELOS_SPIFFE_ENDPOINT_SOCKET", DEFAULT_WORKLOAD_SOCKET), TRUST_DOMAIN)
    deps = InternalApiDeps(
        routines=routines,
        agent_tools=agent_tools,
        storage_root=storage_root,
        get_user_email=get_user_email,
        recall_fn=recall_fn,
        verifier=SvidVerifier(keys, audience=AUDIENCE, trust_domain=TRUST_DOMAIN),
        allowed_sub=os.getenv("TELOS_INTERNAL_ALLOWED_SUB", DEFAULT_ALLOWED_SUB),
        bridge=BridgeConfig.from_env(),
        recall_timeout=float(os.getenv("TELOS_INTERNAL_RECALL_TIMEOUT", str(DEFAULT_RECALL_TIMEOUT))),
    )
    host = os.getenv("TELOS_INTERNAL_API_HOST", "0.0.0.0")
    return InternalApiServer(create_internal_app(deps), host=host, warm=keys.warm)
