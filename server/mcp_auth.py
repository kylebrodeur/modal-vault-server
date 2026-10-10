"""Reusable MCP OAuth 2.1 authorization-server + resource-server seam.

The family's remote MCP surface today is a single static bearer token. That
works for clients that let you hardcode a header, but most of the MCP client
ecosystem (Claude, Cursor, Gemini Spark custom apps, ...) expects OAuth 2.1:
discover the Protected Resource Metadata, discover the Authorization Server
Metadata, dynamically register a client (DCR), run an authorization-code +
PKCE browser flow, and present a bearer access token. Static tokens alone
cannot participate in that handshake.

This module adds a **spec-compliant OAuth 2.1 authorization server** (issuer,
`/authorize`, `/token`, `/register`, `/revoke`, metadata) and a **token
verifier** that accepts BOTH the operator's static token and OAuth tokens on
one `/mcp` endpoint. It is deliberately **client-agnostic**: the redirect-URI
policy is "whatever the client registers via DCR" (the SDK validates the
authorize `redirect_uri` against the registered URI set), so Gemini Spark's
per-connector `https://oauth-redirect.googleusercontent.com/r/<id>` callback
works without any hardcoded allowlist, and so does any other standard client.

Design choices (kept simple and correct for a single-user private vault):

- **Opaque tokens**, not JWTs. Access + refresh tokens are random 256-bit
  strings; only their SHA-256 hashes are persisted (a Volume read never hands
  out a live token), and revocation is exact. No signing-key management. When
  a second resource server appears and family-wide SSO is wanted, the same
  provider class can be hosted in a standalone `modal-oauth-server` app and
  swapped to JWT/JWKS without touching the resource server (it only ever
  calls a `TokenVerifier`).
- **State on the Volume** (`<state_dir>/mcp-as/`), file-based, mode 600,
  atomic writes - the same idiom the vault already uses for sync-mode.json,
  the delete flag, and the watermark. Survives scale-to-zero.
- **Consent gates on the operator's static token.** `/authorize` redirects to
  a minimal in-app consent page; the operator pastes the static token once.
  This closes the confused-deputy hole (a client cannot self-authorize) and
  keeps the single-user model: there is exactly one resource owner.
- **Scopes** are `vault.read` / `vault.write`; enforced per tool at call time
  (a read-only grant cannot call a write tool). The static operator token
  carries both.

This module is a candidate to graduate into `modal-shared-libs` (or a future
standalone `modal-oauth-server`) once a second family MCP server needs it; it
has no dependency on vault internals beyond the static token + state dir.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    TokenVerifier,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

# -- scopes -------------------------------------------------------------------

SCOPE_READ = "vault.read"
SCOPE_WRITE = "vault.write"
ALL_SCOPES = (SCOPE_READ, SCOPE_WRITE)

READ_TOOLS = frozenset(
    {"vault.search", "vault.read", "vault.list", "vault.query_graph", "vault.status", "vault.snapshots"}
)
WRITE_TOOLS = frozenset({"vault.create_note", "vault.update_note", "vault.delete_note", "vault.revert"})

# Token lifetimes (seconds). Access tokens are short; refresh tokens rotate.
CODE_TTL = 300
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
PENDING_TTL = 600

_CLIENT_ID_STATIC = "static-operator"
_SUBJECT = "operator"

# Modes for `MODAL_VAULT_MCP_AUTH`.
MODE_TOKEN = "token"  # static bearer only (default; unchanged behavior)
MODE_OAUTH = "oauth"  # OAuth only
MODE_BOTH = "both"  # static bearer + OAuth on one endpoint
MODES = (MODE_TOKEN, MODE_OAUTH, MODE_BOTH)


def required_scope(tool_name: str) -> str | None:
    """The scope a tool call needs, or None for an unknown tool (dispatch rejects it anyway)."""
    if tool_name in WRITE_TOOLS:
        return SCOPE_WRITE
    if tool_name in READ_TOOLS:
        return SCOPE_READ
    return None


def _hash(value: str) -> str:
    """SHA-256 of a token/code, hex. Only hashes are ever persisted."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def auth_settings(issuer_url: str, resource_server_url: str) -> Any:
    """SDK `AuthSettings` for the self-hosted AS + RS.

    DCR is enabled with the vault scopes; revocation is enabled; the issuer is
    our own origin. `validate_token_resource=True` binds every issued token to
    this resource server's URL (the verifier sets `AccessToken.resource`).
    """
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
    from pydantic import AnyHttpUrl

    return AuthSettings(
        issuer_url=AnyHttpUrl(issuer_url),
        resource_server_url=AnyHttpUrl(resource_server_url),
        validate_token_resource=True,
        required_scopes=None,  # per-tool enforcement happens at call time
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=list(ALL_SCOPES),
            default_scopes=list(ALL_SCOPES),
        ),
        revocation_options=RevocationOptions(enabled=True),
    )


# -- persistent store ---------------------------------------------------------


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    """Read a JSON object; missing/corrupt degrades to the default (repo idiom)."""
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw) if raw.strip() else None
        return parsed if isinstance(parsed, dict) else default
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default


def _write_json(path: Path, obj: dict[str, Any]) -> None:
    """Atomic 0600 write (tmp file + os.replace), never a partial read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(obj, indent=2, sort_keys=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(data)
    os.replace(tmp, path)


class AuthStore:
    """File-backed AS state on the Volume: clients, pending consent, codes, tokens.

    Every record is keyed by the SHA-256 of its secret (or the client_id), so the
    on-disk bytes never contain a live token or code. Writes prune expired
    entries, bounding growth in a single-file design.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def _clients_path(self) -> Path:
        return self.root / "clients.json"

    @property
    def _pending_path(self) -> Path:
        return self.root / "pending.json"

    @property
    def _codes_path(self) -> Path:
        return self.root / "codes.json"

    @property
    def _tokens_path(self) -> Path:
        return self.root / "tokens.json"

    # -- clients --------------------------------------------------------------

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        return _read_json(self._clients_path, {}).get(client_id)

    def put_client(self, client_id: str, record: dict[str, Any]) -> None:
        clients = _read_json(self._clients_path, {})
        clients[client_id] = record
        _write_json(self._clients_path, clients)

    # -- pending consent ------------------------------------------------------

    def put_pending(self, pending_id: str, record: dict[str, Any]) -> None:
        now = time.time()
        pending = {k: v for k, v in _read_json(self._pending_path, {}).items() if v.get("expires_at", 0) > now}
        pending[pending_id] = {**record, "expires_at": now + PENDING_TTL}
        _write_json(self._pending_path, pending)

    def take_pending(self, pending_id: str) -> dict[str, Any] | None:
        now = time.time()
        pending = _read_json(self._pending_path, {})
        record = pending.pop(pending_id, None)
        live = {k: v for k, v in pending.items() if v.get("expires_at", 0) > now}
        _write_json(self._pending_path, live)
        if record and record.get("expires_at", 0) > now:
            return record
        return None

    # -- codes ----------------------------------------------------------------

    def put_code(self, code: str, record: dict[str, Any]) -> None:
        now = time.time()
        codes = {k: v for k, v in _read_json(self._codes_path, {}).items() if v.get("expires_at", 0) > now}
        codes[_hash(code)] = record
        _write_json(self._codes_path, codes)

    def take_code(self, code: str) -> dict[str, Any] | None:
        now = time.time()
        codes = _read_json(self._codes_path, {})
        record = codes.pop(_hash(code), None)
        live = {k: v for k, v in codes.items() if v.get("expires_at", 0) > now}
        _write_json(self._codes_path, live)
        if record and record.get("expires_at", 0) > now:
            return record
        return None

    def get_code(self, code: str) -> dict[str, Any] | None:
        """Read (without consuming) an auth code - the token handler loads it first."""
        now = time.time()
        record = _read_json(self._codes_path, {}).get(_hash(code))
        if record and record.get("expires_at", 0) > now:
            return record
        return None

    # -- tokens ---------------------------------------------------------------

    def _tokens(self) -> dict[str, Any]:
        data = _read_json(self._tokens_path, {})
        return {
            "access": data.get("access") if isinstance(data.get("access"), dict) else {},
            "refresh": data.get("refresh") if isinstance(data.get("refresh"), dict) else {},
        }

    def _save_tokens(self, tokens: dict[str, Any]) -> None:
        now = time.time()
        pruned = {
            "access": {k: v for k, v in tokens["access"].items() if (v.get("expires_at") or 0) > now},
            "refresh": {k: v for k, v in tokens["refresh"].items() if (v.get("expires_at") or 0) > now},
        }
        _write_json(self._tokens_path, pruned)

    def put_access(self, token: str, record: dict[str, Any]) -> None:
        tokens = self._tokens()
        tokens["access"][_hash(token)] = record
        self._save_tokens(tokens)

    def put_refresh(self, token: str, record: dict[str, Any]) -> None:
        tokens = self._tokens()
        tokens["refresh"][_hash(token)] = record
        self._save_tokens(tokens)

    def get_access(self, token: str) -> dict[str, Any] | None:
        return self._tokens()["access"].get(_hash(token))

    def get_refresh(self, token: str) -> dict[str, Any] | None:
        return self._tokens()["refresh"].get(_hash(token))

    def take_refresh(self, token: str) -> dict[str, Any] | None:
        tokens = self._tokens()
        record = tokens["refresh"].pop(_hash(token), None)
        self._save_tokens(tokens)
        return record

    def revoke(self, token: str) -> None:
        tokens = self._tokens()
        tokens["access"].pop(_hash(token), None)
        tokens["refresh"].pop(_hash(token), None)
        self._save_tokens(tokens)


# -- authorization server provider --------------------------------------------


class VaultOAuthProvider:
    """Self-hosted OAuth 2.1 AS: DCR, authorization-code + PKCE, refresh rotation, revoke.

    Implements the SDK's `OAuthAuthorizationServerProvider` protocol. `/authorize`
    redirects to the in-app consent page (registered by `consent_routes`); the
    consent page mints the authorization code after the operator proves identity.
    """

    def __init__(self, store: AuthStore, issuer_url: str, resource_server_url: str) -> None:
        self.store = store
        self.issuer_url = issuer_url.rstrip("/")
        self.resource_server_url = resource_server_url.rstrip("/")

    # -- client registry (DCR) ------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        record = self.store.get_client(client_id)
        return OAuthClientInformationFull.model_validate(record) if record else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.store.put_client(client_info.client_id, client_info.model_dump(mode="json"))

    # -- authorization --------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and params.resource.rstrip("/") != self.resource_server_url:
            raise AuthorizeError(
                error="invalid_request",
                error_description=(
                    f"Unknown resource '{params.resource}': "
                    f"this server issues tokens only for {self.resource_server_url}"
                ),
            )
        pending_id = secrets.token_urlsafe(24)
        self.store.put_pending(
            pending_id,
            {
                "client_id": client.client_id,
                "client_name": client.client_name or client.client_id,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "code_challenge": params.code_challenge,
                "scopes": list(params.scopes or []),
                "resource": self.resource_server_url,
                "state": params.state,
            },
        )
        query = urlencode({"req": pending_id})
        return f"{self.issuer_url}/consent?{query}"

    # -- authorization codes --------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        record = self.store.get_code(authorization_code)
        return self._to_code(authorization_code, record) if record else None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        record = self.store.take_code(authorization_code.code)
        if not record or record.get("client_id") != client.client_id:
            raise TokenError(error="invalid_grant", error_description="authorization code not found")
        return self._issue(record)

    # -- refresh tokens -------------------------------------------------------

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        record = self.store.get_refresh(refresh_token)
        return self._to_refresh(refresh_token, record) if record else None

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        record = self.store.take_refresh(refresh_token.token)
        if not record or record.get("client_id") != client.client_id:
            raise TokenError(error="invalid_grant", error_description="refresh token not found")
        record = {**record, "scopes": list(scopes) or record.get("scopes", [])}
        return self._issue(record)

    # -- access tokens (used by the composite verifier) -----------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        record = self.store.get_access(token)
        if not record:
            return None
        return AccessToken(
            token=token,
            client_id=record.get("client_id", ""),
            scopes=list(record.get("scopes", [])),
            expires_at=record.get("expires_at"),
            resource=record.get("resource"),
            subject=record.get("subject"),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.store.revoke(token.token)

    # -- helpers --------------------------------------------------------------

    def _issue(self, record: dict[str, Any]) -> OAuthToken:
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        now = time.time()
        base = {
            "client_id": record.get("client_id", ""),
            "scopes": list(record.get("scopes", [])),
            "resource": record.get("resource") or self.resource_server_url,
            "subject": record.get("subject") or _SUBJECT,
        }
        self.store.put_access(access, {**base, "expires_at": int(now + ACCESS_TTL)})
        self.store.put_refresh(refresh, {**base, "expires_at": int(now + REFRESH_TTL)})
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            scope=" ".join(base["scopes"]),
            refresh_token=refresh,
        )

    @staticmethod
    def _to_code(code: str, record: dict[str, Any]) -> AuthorizationCode:
        return AuthorizationCode(
            code=code,
            scopes=list(record.get("scopes", [])),
            expires_at=record.get("expires_at", 0),
            client_id=record.get("client_id", ""),
            code_challenge=record.get("code_challenge", ""),
            redirect_uri=AnyUrl(record.get("redirect_uri", "")),
            redirect_uri_provided_explicitly=bool(record.get("redirect_uri_provided_explicitly", True)),
            resource=record.get("resource"),
            subject=record.get("subject") or _SUBJECT,
        )

    @staticmethod
    def _to_refresh(token: str, record: dict[str, Any]) -> RefreshToken:
        expires = record.get("expires_at")
        return RefreshToken(
            token=token,
            client_id=record.get("client_id", ""),
            scopes=list(record.get("scopes", [])),
            expires_at=int(expires) if expires is not None else None,
            resource=record.get("resource"),
            subject=record.get("subject") or _SUBJECT,
        )


# -- token verifier (static + OAuth on one endpoint) --------------------------


class VaultTokenVerifier(TokenVerifier):
    """Accepts the operator's static bearer token AND OAuth access tokens.

    `mode` decides which paths are live: `token` (static only), `oauth` (OAuth
    only), `both`. The static compare is constant-time and length-independent
    (the same rule the admin route uses). OAuth tokens resolve through the
    provider's `load_access_token` (hash lookup + expiry).
    """

    def __init__(
        self,
        provider: VaultOAuthProvider,
        static_token: str,
        mode: str,
        resource_server_url: str,
    ) -> None:
        self.provider = provider
        self.static_token = static_token
        self.mode = mode
        self.resource_server_url = resource_server_url.rstrip("/")

    async def verify_token(self, token: str) -> AccessToken | None:
        if self.mode in (MODE_TOKEN, MODE_BOTH) and self._is_static(token):
            return AccessToken(
                token="",
                client_id=_CLIENT_ID_STATIC,
                scopes=list(ALL_SCOPES),
                expires_at=None,
                resource=self.resource_server_url,
                subject=_SUBJECT,
            )
        if self.mode in (MODE_OAUTH, MODE_BOTH):
            return await self.provider.load_access_token(token)
        return None

    def _is_static(self, token: str) -> bool:
        if not self.static_token:
            return False
        return len(token) == len(self.static_token) and hmac.compare_digest(token, self.static_token)


# -- consent page (operator proves identity; code is minted here) -------------

_CONSENT_PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Authorize {client_name}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:34rem;margin:3rem auto;padding:0 1rem;line-height:1.5}}
input[type=password]{{width:100%;padding:.6rem;font-size:1rem;margin:.5rem 0 1rem}}
button{{padding:.6rem 1.2rem;font-size:1rem;cursor:pointer}}
.error{{color:#b00020}}
code{{background:#f2f2f2;padding:.1rem .3rem;border-radius:.2rem}}
</style></head>
<body>
<h1>Authorize {client_name}</h1>
<p>An application is requesting access to your vault over MCP.</p>
<p><strong>Requested scopes:</strong> {scopes}</p>
{error}
<form method="post" action="/consent">
<input type="hidden" name="req" value="{req}">
<label>Operator token<br><input type="password" name="token" autofocus autocomplete="off"></label>
<button type="submit">Authorize</button>
</form>
<p>Paste the vault's <code>MODAL_VAULT_API_TOKEN</code> to approve. Nothing is shared until you do.</p>
</body></html>
"""


def _render_consent(client_name: str, scopes: list[str], req: str, error: str = "") -> HTMLResponse:
    body = _CONSENT_PAGE.format(
        client_name=_html_escape(client_name),
        scopes=_html_escape(", ".join(scopes) or "(none)"),
        req=_html_escape(req),
        error=f'<p class="error">{_html_escape(error)}</p>' if error else "",
    )
    return HTMLResponse(body)


def _html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


_EXPIRED_MSG = "This authorization request has expired. Start again from your client."


def consent_routes(cfg: Any, store: AuthStore) -> list[Any]:
    """Two top-level routes: GET renders the consent form, POST mints the code.

    Registered on the main app (before the catch-all MCP mount) so they are not
    behind the bearer gate - the operator authenticates by pasting the token.
    """
    from starlette.routing import Route

    async def get_consent(request: Request) -> Response:
        req = request.query_params.get("req", "")
        pending = store.take_pending(req)
        if not pending:
            return _render_consent("unknown application", [], req, _EXPIRED_MSG)
        store.put_pending(req, pending)  # not consumed yet; keep it live for the POST
        return _render_consent(pending.get("client_name", "application"), list(pending.get("scopes", [])), req)

    async def post_consent(request: Request) -> Response:
        form = await request.form()
        req = str(form.get("req", ""))
        token = str(form.get("token", ""))
        pending = store.take_pending(req)
        if not pending:
            return _render_consent("unknown application", [], req, _EXPIRED_MSG)
        expected = cfg.api_token or ""
        given = token or ""
        if not expected or len(given) != len(expected) or not hmac.compare_digest(given, expected):
            store.put_pending(req, pending)
            return _render_consent(
                pending.get("client_name", "application"),
                list(pending.get("scopes", [])),
                req,
                "Operator token did not match. Try again.",
            )
        code = secrets.token_urlsafe(32)
        store.put_code(
            code,
            {
                "client_id": pending.get("client_id", ""),
                "scopes": list(pending.get("scopes", [])),
                "code_challenge": pending.get("code_challenge", ""),
                "redirect_uri": pending.get("redirect_uri", ""),
                "redirect_uri_provided_explicitly": pending.get("redirect_uri_provided_explicitly", True),
                "resource": pending.get("resource"),
                "subject": _SUBJECT,
                "expires_at": time.time() + CODE_TTL,
            },
        )
        target = construct_redirect_uri(str(pending.get("redirect_uri", "")), code=code, state=pending.get("state"))
        return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})

    return [
        Route("/consent", get_consent, methods=["GET"]),
        Route("/consent", post_consent, methods=["POST"]),
    ]
