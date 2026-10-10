"""Env-knob configuration. Every knob uses the MODAL_VAULT_ prefix (MODAL_ platform vars pass through in app.py)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _path_env(name: str, default: str) -> Path:
    """Read a MODAL_VAULT_* path knob; unset falls back to the given default path."""
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else Path(default)


@dataclass(frozen=True)
class Config:
    """Runtime configuration, loaded from MODAL_VAULT_* environment variables.

    The `ob_` credentials (optional) enable first-boot login when the state
    directory is empty: put them in the `modal-vault-secret` Modal Secret.
    Read from env once at boot; never logged, never echoed, never stored
    outside the process.
    """

    api_token: str
    data_dir: Path  # vault clone
    state_dir: Path  # ob login/sync state
    sync_mode: str = "pull-only"
    sync_timeout: int = 1800
    ob_email: str = ""
    ob_password: str = ""
    ob_mfa: str = ""  # MFA code when the account has MFA (one-time at login)
    ob_vault: str = ""  # Sync vault name; set to run sync-setup on first boot
    ob_e2e_password: str = ""  # only for e2e-encrypted vaults
    mcp_auth: str = "token"  # token | oauth | both (static bearer + OAuth 2.1)
    mcp_auth_issuer: str = ""  # this app's origin; blank => derive from the request

    @property
    def has_ob_credentials(self) -> bool:
        return bool(self.ob_email and self.ob_password)

    @classmethod
    def load(cls) -> Config:
        """Read env with family defaults; state stays colocated with the vault by default.

        Every knob is a single `MODAL_VAULT_*` name (no legacy short-name
        fallback): one vocabulary across the family, so a shell export can
        never resolve to a different server's value.
        """
        data_dir = _path_env("MODAL_VAULT_DATA_DIR", "/vault")
        return cls(
            api_token=os.environ.get("MODAL_VAULT_API_TOKEN", ""),
            data_dir=data_dir,
            state_dir=_path_env("MODAL_VAULT_STATE_DIR", str(data_dir / "state")),
            sync_mode=os.environ.get("MODAL_VAULT_SYNC_MODE", "pull-only"),
            sync_timeout=int(os.environ.get("MODAL_VAULT_SYNC_TIMEOUT", "1800")),
            ob_email=os.environ.get("MODAL_VAULT_OB_EMAIL", "").strip(),
            ob_password=os.environ.get("MODAL_VAULT_OB_PASSWORD", "").strip(),
            ob_mfa=os.environ.get("MODAL_VAULT_OB_MFA", "").strip(),
            ob_vault=os.environ.get("MODAL_VAULT_OB_VAULT", "").strip(),
            ob_e2e_password=os.environ.get("MODAL_VAULT_OB_E2E_PASSWORD", "").strip(),
            mcp_auth=os.environ.get("MODAL_VAULT_MCP_AUTH", "token").strip() or "token",
            mcp_auth_issuer=os.environ.get("MODAL_VAULT_MCP_AUTH_ISSUER", "").strip().rstrip("/"),
        )
