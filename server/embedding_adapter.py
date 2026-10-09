"""The embedding-server connection: the vault's semantic-search door, made live.

Config (env via the Secret): `VAULT_EMBEDDING_URL` (the embedding server's
base URL) + optional `VAULT_EMBEDDING_TOKEN` (bearer for its authed reads).
Connected: `vault.status`'s semantic section flips from the constant
"configured: false" to a live probe (health + collections count); disconnected:
unchanged, honest. Unreachable = reported, never raises.
"""

from __future__ import annotations

import os
from typing import Any

_SEMANTIC_DOOR: dict[str, Any] = {
    "configured": False,
    "note": "connect modal-embedding-server later (VAULT_EMBEDDING_URL in the Secret)",
}

_EMBEDDING_ENV = "VAULT_EMBEDDING_URL"
_TOKEN_ENV = "VAULT_EMBEDDING_TOKEN"
_PROBE_TIMEOUT_S = 3.0


def _base_url() -> str:
    return os.environ.get(_EMBEDDING_ENV, "").strip().rstrip("/")


def _headers() -> dict[str, str]:
    token = os.environ.get(_TOKEN_ENV, "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def semantic_section(cfg: Any | None = None, timeout_s: float = _PROBE_TIMEOUT_S) -> dict[str, Any]:
    """The honest semantic door: constant when unconfigured, a live probe when connected."""
    base = _base_url()
    if not base:
        return dict(_SEMANTIC_DOOR)
    import httpx

    section: dict[str, Any] = {"configured": True, "url_set": True}
    try:
        with httpx.Client(timeout=timeout_s) as client:
            health = client.get(f"{base}/health")
            health.raise_for_status()
            body = health.json() if health.content else {}
            section["health"] = {
                "ok": bool(body.get("ok", body.get("status") == "ok")),
                "default_model": body.get("default_model"),
            }
            stats = client.get(f"{base}/stats", headers=_headers())
            if stats.status_code == 200:
                data = stats.json() if stats.content else {}
                collections = data.get("collections")
                section["collections"] = len(collections) if isinstance(collections, list) else collections
                section["loaded_models"] = data.get("loaded_models")
            else:
                section["collections"] = f"unavailable (HTTP {stats.status_code})"
    except Exception as exc:
        section["reachable"] = False
        section["error"] = str(exc)[:200]
    return section


def configured() -> bool:
    """True when VAULT_EMBEDDING_URL is set (the door's constant flips on it)."""
    return bool(_base_url())
