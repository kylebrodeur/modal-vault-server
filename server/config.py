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
    """Runtime configuration, loaded from VAULT_* environment variables."""

    api_token: str
    data_dir: Path  # vault clone
    state_dir: Path  # ob login/sync state
    sync_mode: str = "pull-only"
    sync_timeout: int = 1800

    @classmethod
    def load(cls) -> Config:
        """Read env with family defaults; state stays colocated with the vault by default."""
        data_dir = _path_env("VAULT_DATA_DIR", "/vault")
        return cls(
            api_token=os.environ.get("VAULT_API_TOKEN", ""),
            data_dir=data_dir,
            state_dir=_path_env("VAULT_STATE_DIR", str(data_dir / "state")),
            sync_mode=os.environ.get("VAULT_SYNC_MODE", "pull-only"),
            sync_timeout=int(os.environ.get("VAULT_SYNC_TIMEOUT", "1800")),
        )
