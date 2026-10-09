"""Env-knob configuration. Every knob uses the VAULT_ prefix (MODAL_ platform vars pass through only in app.py)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _path_env(name: str, default: str) -> Path:
    """Read a VAULT_* path knob; unset falls back to the given default path."""
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else Path(default)


@dataclass(frozen=True)
class Config:
    """Runtime configuration, loaded from VAULT_* environment variables.

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

    @property
    def has_ob_credentials(self) -> bool:
        return bool(self.ob_email and self.ob_password)

    @classmethod
    def load(cls) -> Config:
        """Read env with family defaults; state stays colocated with the vault by default.

        The ob credential knobs accept BOTH name shapes: the upstream
        `VAULT_OB_*` names and the integration overlay's short `OB_*` names
        (`OB_EMAIL`, `OB_PASSWORD`, `OB_MFA`, `OB_VAULT`, `OB_E2E_PASSWORD`) -
        so an existing lane's Secrets work without remapping. `VAULT_OB_*`
        wins when both are set.
        """
        data_dir = _path_env("VAULT_DATA_DIR", "/vault")
        return cls(
            api_token=os.environ.get("VAULT_API_TOKEN", ""),
            data_dir=data_dir,
            state_dir=_path_env("VAULT_STATE_DIR", str(data_dir / "state")),
            sync_mode=os.environ.get("VAULT_SYNC_MODE", "pull-only"),
            sync_timeout=int(os.environ.get("VAULT_SYNC_TIMEOUT", "1800")),
            ob_email=_env_of("VAULT_OB_EMAIL", "OB_EMAIL"),
            ob_password=_env_of("VAULT_OB_PASSWORD", "OB_PASSWORD"),
            ob_mfa=_env_of("VAULT_OB_MFA", "OB_MFA"),
            ob_vault=_env_of("VAULT_OB_VAULT", "OB_VAULT"),
            ob_e2e_password=_env_of("VAULT_OB_E2E_PASSWORD", "OB_E2E_PASSWORD"),
        )


def _env_of(*names: str) -> str:
    """First non-empty env value among the names (name-fallback chain)."""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""
