"""Env-knob configuration. Every knob uses the VAULT_ prefix (MODAL_ platform vars pass through only in app.py)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _path_env(name: str, default: str) -> Path:
    """Read a VAULT_* path knob; unset falls back to <data_dir default>/<leaf>."""
    raw = os.environ.get(name)
    if raw:
        return Path(raw).expanduser()
    leaf = default.split("/")[-1]
    data_default = Path(os.environ.get("VAULT_DATA_DIR", "/vault"))
    return data_default / leaf if name != "VAULT_DATA_DIR" else data_default


@dataclass(frozen=True)
class Config:
    """Runtime configuration, loaded from VAULT_* environment variables."""

    api_token: str
    data_dir: Path  # vault clone
    index_dir: Path
    state_dir: Path
    embed_url: str | None
    embed_api_token: str | None
    embed_model: str = "embeddinggemma"
    embed_dim: int = 768
    sync_mode: str = "pull-only"
    sync_timeout: int = 1800

    @classmethod
    def load(cls) -> Config:
        """Read env with family defaults; unset hook vars mean 'no embedding hook'."""
        data_dir = _path_env("VAULT_DATA_DIR", "/vault")
        index_dir = _path_env("VAULT_INDEX_DIR", "index")
        state_dir = _path_env("VAULT_STATE_DIR", "state")
        embed_url = os.environ.get("VAULT_EMBED_URL") or None
        return cls(
            api_token=os.environ.get("VAULT_API_TOKEN", ""),
            data_dir=data_dir,
            index_dir=index_dir,
            state_dir=state_dir,
            embed_url=embed_url,
            embed_api_token=os.environ.get("VAULT_EMBED_API_TOKEN") or None,
            embed_model=os.environ.get("VAULT_EMBED_MODEL", "embeddinggemma"),
            embed_dim=int(os.environ.get("VAULT_EMBED_DIM", "768")),
            sync_mode=os.environ.get("VAULT_SYNC_MODE", "pull-only"),
            sync_timeout=int(os.environ.get("VAULT_SYNC_TIMEOUT", "1800")),
        )
