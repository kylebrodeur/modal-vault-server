"""HTTP surface: /health, bearer /admin/sync, and the MCP streamable-HTTP mount at /mcp.

FastAPI owns the readable routes; the official `mcp` SDK server (low-level `Server`)
owns the MCP wire protocol, built by `build_mcp_server` with every tool delegating
to `VaultTools.call`. The mount is guarded by a raw ASGI bearer gate covering
/mcp/*; /admin/* gates itself. The gate matches the exact single header
"Authorization: Bearer <cfg.api_token>" (case-insensitive header name, otherwise exact).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import mcp.types as mcp_types
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from mcp.server.lowlevel import Server
from starlette.routing import Mount, Route
from starlette.types import ASGIApp

from server import search_scan
from server import sync_mode as sync_mode_module
from server.config import Config
from server.mcp_tools import VaultTools, sync_state
from server.sync_service import SyncService
from server.types import LAST_SYNC_FILE, SyncResult

_WWW_AUTHENTICATE = "Bearer"


def _bearer_ok(header_value: str | None, token: str) -> bool:
    """True only for the exact `Authorization: Bearer <token>` pair (None -> False).

    THE one bearer comparison (admin route + ASGI guard both route through this);
    timing-safe: length-independent compare, never an early-exit on the secret.
    """
    expected = f"Bearer {token}"
    return (
        header_value is not None and len(header_value) == len(expected) and hmac.compare_digest(header_value, expected)
    )


def _reject() -> JSONResponse:
    return JSONResponse({"detail": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": _WWW_AUTHENTICATE})


class BearerGuard:
    """Raw ASGI bearer gate: non-matching Authorization gets a 401, everything else passes.

    Same exact-bearer rule as /admin — both route through `_bearer_ok` (one
    comparison in the codebase). Non-http scopes (lifespan) pass.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._token = token

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        headers = scope.get("headers") or []
        authorization: str | None = None
        for key, value in headers:
            if key.lower() == b"authorization":
                authorization = value.decode("latin-1")
                break
        if _bearer_ok(authorization, self._token):
            await self._app(scope, receive, send)
        else:
            await self._reject(scope, receive, send)

    async def _reject(self, scope, receive, send) -> None:
        reject = _reject()
        await reject(scope, receive, send)


def build_mcp_server(cfg: Config, tools: VaultTools) -> Server:
    """Official mcp SDK low-level server: five tools, every call delegated to VaultTools.call."""

    async def on_list_tools(_context: Any, _params: Any) -> mcp_types.ListToolsResult:
        return mcp_types.ListToolsResult(tools=[mcp_types.Tool.model_validate(tool) for tool in tools.TOOLS])

    async def on_call_tool(_context: Any, params: mcp_types.CallToolRequestParams) -> mcp_types.CallToolResult:
        reply: dict[str, Any] = await tools.call(params.name, dict(params.arguments or {}))
        text = json.dumps(reply)
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(text=text)], structured_content=reply, is_error=False
        )

    return Server(
        "modal-vault-server",
        instructions="Read-only vault tools over the Headless Sync clone.",
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def build_app(cfg: Config, sync: SyncService) -> FastAPI:
    """FastAPI app: GET /health, bearer POST /admin/sync, MCP streamable-HTTP mounted at /mcp.

    The MCP session manager boots with the app lifespan; serving the clone never
    touches `ob` (only /admin/sync and boot do).
    """
    tools = VaultTools(cfg=cfg, search=search_scan, sync=sync)
    server = build_mcp_server(cfg, tools)
    mcp_asgi: ASGIApp = server.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")
    session_manager = server.session_manager
    worker = sync_mode_module.ContinuousWorker(cfg.data_dir)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Re-apply the persisted mode on every boot (the state dir survives
        # container restarts; the continuous daemon does NOT): continuous mode
        # restarts its daemon here, other modes just stop it.
        with contextlib.suppress(Exception):
            mode = sync_mode_module.current_mode(cfg.state_dir)
            if mode == sync_mode_module.MODE_CONTINUOUS:
                with contextlib.suppress(Exception):
                    worker.bind_env(cfg.state_dir)
                    worker.start()
            else:
                with contextlib.suppress(Exception):
                    worker.stop()
        async with session_manager.run():
            yield

    def require_admin(authorization: Annotated[str | None, Header()] = None) -> None:
        if not _bearer_ok(authorization, cfg.api_token):
            raise HTTPException(status_code=401, detail="unauthorized", headers={"WWW-Authenticate": _WWW_AUTHENTICATE})

    async def admin_sync_mode(request: Request, authorization: Annotated[str | None, Header()] = None) -> JSONResponse:
        """Bearer-gated runtime sync-mode flip: {"mode": "pull-only|sync-on-write|continuous"}."""
        require_admin(authorization)
        try:
            body = json.loads((await request.body()).decode("utf-8", errors="replace") or "{}")
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        mode = str(body.get("mode", ""))
        if mode not in sync_mode_module.MODES:
            return JSONResponse({"error": f"mode must be one of {list(sync_mode_module.MODES)}"}, status_code=400)
        out = await asyncio.to_thread(
            sync_mode_module.apply_mode, mode, cfg.state_dir, cfg.data_dir, sync, worker, cfg.sync_timeout
        )
        out["result"] = (
            {"ok": out["result"].ok, "mode": out["result"].mode, "detail": out["result"].detail}
            if out.get("result")
            else None
        )
        return JSONResponse(out)

    async def admin_get_sync_mode(authorization: Annotated[str | None, Header()] = None) -> JSONResponse:
        """Bearer-gated current sync posture (mode + whether the daemon is live)."""
        require_admin(authorization)
        mode = sync_mode_module.current_mode(cfg.state_dir)
        return JSONResponse(
            {
                "mode": mode,
                "continuous_running": worker.is_running(),
                "continuous_started_at": worker.started_at(),
            }
        )

    async def health() -> JSONResponse:
        """Vault + watermark + posture triage; only touches files, so it never raises."""
        payload = web_health(cfg)
        payload["sync_mode"] = sync_mode_module.current_mode(cfg.state_dir)
        payload["continuous_running"] = worker.is_running()
        return JSONResponse(payload)

    async def admin_sync(authorization: Annotated[str | None, Header()] = None) -> JSONResponse:
        """Bearer-gated one ob sync pull, off the event loop; SyncResult as JSON."""
        if not _bearer_ok(authorization, cfg.api_token):
            return _reject()
        result = await asyncio.to_thread(sync.one_shot, cfg.sync_timeout)
        return JSONResponse({"ok": result.ok, "mode": result.mode, "detail": result.detail})

    app = FastAPI(
        title="modal-vault-server",
        lifespan=lifespan,
        redirect_slashes=False,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.get("/health")(health)
    app.post("/admin/sync")(admin_sync)
    app.post("/admin/sync-mode")(admin_sync_mode)
    app.get("/admin/sync-mode")(admin_get_sync_mode)
    guarded = BearerGuard(mcp_asgi, cfg.api_token)
    # Two routes for one MCP endpoint: Mount serves sub-paths (Starlette keeps the
    # child's full path and sets root_path, so the SDK's inner Route("/mcp") matches
    # "/mcp/mcp"); the bare Route serves "/mcp" exactly (Mount needs the slash).
    app.router.routes.append(Mount("/mcp", app=guarded))
    app.router.routes.append(Route("/mcp", guarded, methods=["GET", "POST", "DELETE"]))
    return app


def web_health(cfg: Config) -> dict[str, Any]:
    """Health triage: {"status", "vault", "sync"}; degraded means vault missing or last sync not ok."""
    vault = _vault_exists(cfg.data_dir)
    sync_trio = sync_state(cfg.state_dir, cfg.sync_mode)
    degraded = (not vault) or (not sync_trio["ok"])
    return {"status": "degraded" if degraded else "ok", "vault": vault, "sync": sync_trio}


def _vault_exists(data_dir: Path) -> bool:
    try:
        return data_dir.is_dir()
    except OSError:
        return False


def run_boot(cfg: Config, sync: SyncService) -> dict[str, Any]:
    """Boot: login+link when possible (first boot), then one ob pull + watermark stamp.

    First boot (empty state + credentials present in env, from the
    modal-vault-secret Secret): `ob login` -> optional `ob sync-setup` ->
    then the pull, PVM-style phase order (auth before sync). With no
    credentials, the pull runs anyway and its failure degrades exactly as it
    did before - the honest fallback for state-less deploys. Credentials are
    read from env at boot only; they are never logged, echoed, or written
    outside the process.
    """
    bootstrapped = False
    if not sync.is_logged_in() and cfg.has_ob_credentials:
        try:
            sync.bootstrap(cfg.ob_email, cfg.ob_password, mfa=cfg.ob_mfa)
            if cfg.ob_vault:
                sync.link_vault(cfg.ob_vault, e2e_password=cfg.ob_e2e_password)
            bootstrapped = True
        except Exception as exc:  # contained: degrade into the pull attempt below
            bootstrapped = False
            detail = f"boot login failed: {type(exc).__name__}"
            with contextlib.suppress(OSError):
                write_boot_note(cfg.state_dir, detail)
    try:
        result = sync.one_shot(timeout_seconds=cfg.sync_timeout)
    except Exception as exc:
        result = SyncResult(ok=False, mode=cfg.sync_mode, detail=f"boot sync crashed: {exc!r}")
    if result.ok:
        try:
            write_watermark(cfg.state_dir, result.mode, _now_iso())
        except OSError as exc:
            detail = f"sync ok but watermark write failed: {exc}"
            return {"sync": SyncResult(ok=False, mode=result.mode, detail=detail), "degraded": True}
    entry = {"sync": result, "degraded": not result.ok, "bootstrapped": bootstrapped}
    return entry


def write_boot_note(state_dir: Path, detail: str) -> None:
    """Record a boot-phase failure WITHOUT the credential values (type only)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "boot.json").write_text(
        json.dumps({"event": "bootstrap_failed", "detail": detail, "at": _now_iso()}), encoding="utf-8"
    )


def write_watermark(state_dir: Path, mode: str, last_sync_at: str) -> None:
    """Stamp last_sync.json with the sync trio; caller owns its timestamp string."""
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"mode": mode, "last_sync_at": last_sync_at, "ok": True})
    (state_dir / LAST_SYNC_FILE).write_text(payload, encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
