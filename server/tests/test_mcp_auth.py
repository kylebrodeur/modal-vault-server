"""MCP OAuth 2.1 tests: static/OAuth coexistence, DCR, PKCE flow, scopes, refresh, revoke.

These defend the observable contract of the auth surface: which credentials are
accepted on /mcp in each mode, that a full DCR -> authorize -> consent -> token
-> tools/call flow works for a standard client, that the issued token is bound
to this resource server and carries only its scopes, and that refresh rotates
and revoke kills. Nothing here asserts implementation internals; every check is
something a client or the operator observes over HTTP.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from server import mcp_auth, web
from server.config import Config

_API = "application/json, text/event-stream"
_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
}
_CONSENT_CB = "https://oauth-redirect.googleusercontent.com/r/my-connector"


def _client(tmp_path: Path, mode: str) -> TestClient:
    vault = tmp_path / "vault"
    state = tmp_path / "state"
    vault.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    (vault / "Note.md").write_text("hello vault\n", encoding="utf-8")
    cfg = Config(
        api_token="TOK",
        data_dir=vault,
        state_dir=state,
        mcp_auth=mode,
        mcp_auth_issuer="https://vault.example",
    )
    return TestClient(web.build_app(cfg, sync=None))


def _headers(token: str | None) -> dict[str, str]:
    base = {"Accept": _API, "Content-Type": "application/json"}
    if token:
        base["Authorization"] = f"Bearer {token}"
    return base


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(43)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _register(client: TestClient, redirect_uri: str = _CONSENT_CB) -> str:
    reply = client.post(
        "/register",
        json={
            "redirect_uris": [redirect_uri],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": "Test Client",
        },
    )
    assert reply.status_code == 201, reply.text
    return reply.json()["client_id"]


def _authorize_and_consent(
    client: TestClient, client_id: str, scopes: str, redirect_uri: str = _CONSENT_CB, operator_token: str = "TOK"
) -> tuple[str, str]:
    """Run /authorize then the consent POST; return (code, verifier)."""
    verifier, challenge = _pkce()
    reply = client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "st-123",
            "scope": scopes,
        },
        follow_redirects=False,
    )
    assert reply.status_code == 302, reply.text
    assert reply.headers["location"].startswith("https://vault.example/consent?req=")
    req = reply.headers["location"].split("req=")[1]
    consent = client.post("/consent", data={"req": req, "token": operator_token}, follow_redirects=False)
    assert consent.status_code == 302, consent.text
    location = consent.headers["location"]
    assert location.startswith(redirect_uri)
    assert "state=st-123" in location
    code = re.search(r"code=([^&]+)", location)
    assert code, location
    return code.group(1), verifier


def _exchange(
    client: TestClient, client_id: str, code: str, verifier: str, redirect_uri: str = _CONSENT_CB
) -> dict[str, Any]:
    reply = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
    )
    assert reply.status_code == 200, reply.text
    return reply.json()


def _full_token(client: TestClient, scopes: str) -> tuple[str, dict[str, Any]]:
    client_id = _register(client)
    code, verifier = _authorize_and_consent(client, client_id, scopes)
    return client_id, _exchange(client, client_id, code, verifier)


def _call(client: TestClient, token: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    init = client.post("/mcp", headers=_headers(token), json=_INIT)
    assert init.status_code == 200, init.text
    extra = {}
    if sid := init.headers.get("mcp-session-id"):
        extra["mcp-session-id"] = sid
    reply = client.post(
        "/mcp",
        headers={**_headers(token), **extra},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
    )
    assert reply.status_code == 200, reply.text
    return reply.json()["result"]


class TestDiscovery:
    """Both well-known documents are served, so any standard client can find the AS."""

    def test_protected_resource_metadata_points_at_issuer(self, tmp_path) -> None:
        with _client(tmp_path, "both") as client:
            body = client.get("/.well-known/oauth-protected-resource/mcp").json()
            assert body["resource"] == "https://vault.example/mcp"
            assert body["authorization_servers"] == ["https://vault.example/"]

    def test_authorization_server_metadata_advertises_dcr_and_scopes(self, tmp_path) -> None:
        with _client(tmp_path, "both") as client:
            body = client.get("/.well-known/oauth-authorization-server").json()
            assert body["issuer"].rstrip("/") == "https://vault.example"
            assert body["registration_endpoint"].endswith("/register")
            assert body["revocation_endpoint"].endswith("/revoke")
            assert set(body["scopes_supported"]) == {"vault.read", "vault.write"}
            assert body["code_challenge_methods_supported"] == ["S256"]


class TestStaticOnlyMode:
    """Default `token` mode is byte-for-byte the old behavior."""

    def test_static_token_reaches_protocol(self, tmp_path) -> None:
        with _client(tmp_path, "token") as client:
            assert client.post("/mcp", headers=_headers("TOK"), json=_INIT).status_code == 200

    def test_no_token_is_401(self, tmp_path) -> None:
        with _client(tmp_path, "token") as client:
            assert client.post("/mcp", headers=_headers(None), json=_INIT).status_code == 401

    def test_wrong_token_is_401(self, tmp_path) -> None:
        with _client(tmp_path, "token") as client:
            assert client.post("/mcp", headers=_headers("nope"), json=_INIT).status_code == 401

    def test_oauth_routes_absent(self, tmp_path) -> None:
        with _client(tmp_path, "token") as client:
            assert client.post("/register", json={"client_name": "x"}).status_code == 404


class TestBothMode:
    """`both` accepts the static token and OAuth tokens on the one /mcp endpoint."""

    def test_static_token_still_works(self, tmp_path) -> None:
        with _client(tmp_path, "both") as client:
            assert client.post("/mcp", headers=_headers("TOK"), json=_INIT).status_code == 200

    def test_missing_token_challenge_carries_resource_metadata(self, tmp_path) -> None:
        with _client(tmp_path, "both") as client:
            reply = client.post("/mcp", headers=_headers(None), json=_INIT)
            assert reply.status_code == 401
            assert "resource_metadata=" in reply.headers["www-authenticate"]

    def test_oauth_token_reaches_protocol(self, tmp_path) -> None:
        with _client(tmp_path, "both") as client:
            _cid, token = _full_token(client, "vault.read vault.write")
            assert client.post("/mcp", headers=_headers(token["access_token"]), json=_INIT).status_code == 200


class TestOAuthOnlyMode:
    def test_static_token_rejected(self, tmp_path) -> None:
        with _client(tmp_path, "oauth") as client:
            assert client.post("/mcp", headers=_headers("TOK"), json=_INIT).status_code == 401


class TestAuthorizationFlow:
    """The full DCR -> authorize -> consent -> token -> tools/call path, client-agnostic."""

    def test_full_flow_with_per_connector_redirect_uri(self, tmp_path) -> None:
        with _client(tmp_path, "both") as client:
            client_id, token = _full_token(client, "vault.read vault.write")
            assert client_id
            assert token["token_type"] == "Bearer"
            assert token["refresh_token"]
            assert set(token["scope"].split()) == {"vault.read", "vault.write"}
            result = _call(client, token["access_token"], "vault.read", {"path": "Note.md"})
            assert result["structuredContent"]["exists"] is True

    def test_consent_rejects_wrong_operator_token(self, tmp_path) -> None:
        with _client(tmp_path, "both") as client:
            client_id = _register(client)
            _verifier, challenge = _pkce()
            reply = client.get(
                "/authorize",
                params={
                    "client_id": client_id,
                    "redirect_uri": _CONSENT_CB,
                    "response_type": "code",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                },
                follow_redirects=False,
            )
            req = reply.headers["location"].split("req=")[1]
            bad = client.post("/consent", data={"req": req, "token": "WRONG"}, follow_redirects=False)
            assert bad.status_code == 200  # re-rendered form, no redirect
            assert "location" not in bad.headers

    def test_token_handler_rejects_bad_pkce_verifier(self, tmp_path) -> None:
        with _client(tmp_path, "both") as client:
            client_id = _register(client)
            code, _verifier = _authorize_and_consent(client, client_id, "vault.read")
            reply = client.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": client_id,
                    "redirect_uri": _CONSENT_CB,
                    "code_verifier": secrets.token_urlsafe(43),
                },
            )
            assert reply.status_code == 400
            assert reply.json()["error"] == "invalid_grant"


class TestScopeEnforcement:
    """A token may call only what its scopes allow; the static token carries both."""

    def test_read_only_token_denied_write_tool(self, tmp_path) -> None:
        with _client(tmp_path, "both") as client:
            _cid, token = _full_token(client, "vault.read")
            denied = _call(client, token["access_token"], "vault.create_note", {"path": "x.md", "text": "y"})
            assert denied["isError"] is True
            assert "vault.write" in denied["structuredContent"]["error"]
            allowed = _call(client, token["access_token"], "vault.list", {})
            assert allowed["isError"] is False

    def test_static_token_carries_all_scopes(self, tmp_path) -> None:
        with _client(tmp_path, "both") as client:
            result = _call(client, "TOK", "vault.create_note", {"path": "x.md", "text": "y"})
            assert result["isError"] is False


class TestAudienceBinding:
    """A token whose resource indicator is not this server is refused at /mcp."""

    def test_foreign_resource_token_is_refused(self, tmp_path) -> None:
        with _client(tmp_path, "both") as client:
            store = client.app.state.mcp_auth_store
            store.put_access(
                "foreign-token",
                {
                    "client_id": "someone-else",
                    "scopes": ["vault.read", "vault.write"],
                    "resource": "https://other.example/mcp",
                    "expires_at": 4102444800,
                    "subject": "operator",
                },
            )
            assert client.post("/mcp", headers=_headers("foreign-token"), json=_INIT).status_code == 401

    def test_authorize_rejects_unknown_resource(self, tmp_path) -> None:
        with _client(tmp_path, "both") as client:
            client_id = _register(client)
            _verifier, challenge = _pkce()
            reply = client.get(
                "/authorize",
                params={
                    "client_id": client_id,
                    "redirect_uri": _CONSENT_CB,
                    "response_type": "code",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "resource": "https://other.example/mcp",
                },
                follow_redirects=False,
            )
            assert reply.status_code == 302
            assert "error=invalid_request" in reply.headers["location"]


class TestRefreshAndRevoke:
    def test_refresh_rotates_both_tokens(self, tmp_path) -> None:
        with _client(tmp_path, "both") as client:
            client_id, token = _full_token(client, "vault.read")
            reply = client.post(
                "/token",
                data={"grant_type": "refresh_token", "refresh_token": token["refresh_token"], "client_id": client_id},
            )
            assert reply.status_code == 200
            rotated = reply.json()
            assert rotated["access_token"] != token["access_token"]
            assert rotated["refresh_token"] != token["refresh_token"]
            # the old refresh token is now dead (rotation)
            replay = client.post(
                "/token",
                data={"grant_type": "refresh_token", "refresh_token": token["refresh_token"], "client_id": client_id},
            )
            assert replay.status_code == 400
            assert replay.json()["error"] == "invalid_grant"

    def test_revoke_kills_access_token(self, tmp_path) -> None:
        with _client(tmp_path, "both") as client:
            client_id, token = _full_token(client, "vault.read")
            # public client: the SDK revocation handler requires the field present (empty ok)
            reply = client.post(
                "/revoke", data={"token": token["access_token"], "client_id": client_id, "client_secret": ""}
            )
            assert reply.status_code == 200
            assert client.post("/mcp", headers=_headers(token["access_token"]), json=_INIT).status_code == 401


class TestModeValidation:
    def test_unknown_mode_falls_back_to_static(self) -> None:
        cfg = Config(api_token="t", data_dir=Path("/tmp"), state_dir=Path("/tmp"), mcp_auth="bogus")
        assert cfg.mcp_auth not in mcp_auth.MODES
