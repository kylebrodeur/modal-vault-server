"""Tests for MODAL_VAULT_* env configuration and shared types."""

from pathlib import Path
from typing import Any

import pytest

from server.config import Config
from server.types import EdgeRow, IndexResult, SyncResult


class TestConfigDefaults:
    """Config.load() with no MODAL_VAULT_* env set."""

    def test_load_returns_config_without_any_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODAL_VAULT_API_TOKEN", raising=False)
        cfg = Config.load()
        assert isinstance(cfg, Config)

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODAL_VAULT_API_TOKEN", raising=False)
        monkeypatch.delenv("MODAL_VAULT_DATA_DIR", raising=False)
        monkeypatch.delenv("MODAL_VAULT_STATE_DIR", raising=False)
        cfg = Config.load()
        assert cfg.data_dir == Path("/vault")
        assert cfg.state_dir == Path("/vault/state")
        assert cfg.sync_mode == "pull-only"
        assert cfg.sync_timeout == 1800

    def test_dir_defaults_derive_from_data_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODAL_VAULT_DATA_DIR", "/custom/vault")
        monkeypatch.delenv("MODAL_VAULT_STATE_DIR", raising=False)
        cfg = Config.load()
        assert cfg.data_dir == Path("/custom/vault")
        assert cfg.state_dir == Path("/custom/vault/state")

    def test_api_token_defaults_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing MODAL_VAULT_API_TOKEN does not raise at load; fail-closed-at-boot is app.py's job."""
        monkeypatch.delenv("MODAL_VAULT_API_TOKEN", raising=False)
        assert Config.load().api_token == ""


class TestConfigEnvOverrides:
    def test_all_env_overrides(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        data, state = tmp_path / "vault", tmp_path / "state"
        monkeypatch.setenv("MODAL_VAULT_API_TOKEN", "tok-123")
        monkeypatch.setenv("MODAL_VAULT_DATA_DIR", str(data))
        monkeypatch.setenv("MODAL_VAULT_STATE_DIR", str(state))
        monkeypatch.setenv("MODAL_VAULT_SYNC_MODE", "push")
        monkeypatch.setenv("MODAL_VAULT_SYNC_TIMEOUT", "60")

        cfg = Config.load()
        assert cfg.api_token == "tok-123"
        assert cfg.data_dir == data
        assert cfg.state_dir == state
        assert cfg.sync_mode == "push"
        assert cfg.sync_timeout == 60

    def test_explicit_dir_override_wins_over_derivation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODAL_VAULT_DATA_DIR", "/custom/vault")
        monkeypatch.setenv("MODAL_VAULT_STATE_DIR", "/elsewhere/state")
        assert Config.load().state_dir == Path("/elsewhere/state")

    def test_token_values_round_trip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODAL_VAULT_API_TOKEN", "  secret-with-specials_#@$  ")
        assert Config.load().api_token == "  secret-with-specials_#@$  "


class TestTypes:
    def test_edge_row_frozen(self) -> None:
        edge = EdgeRow(src="a.md", dst="b.md", kind="note-link", raw="[[b]]")
        with pytest.raises((AttributeError, TypeError)):
            edge.src = "z.md"  # type: ignore[misc]

    def test_index_result_defaults(self) -> None:
        r = IndexResult()
        assert r.notes_scanned == 0
        assert r.edges_upserted == 0
        assert r.skipped_reason is None

    def test_index_result_repurposed_fields(self) -> None:
        r = IndexResult(notes_scanned=42, edges_upserted=7, skipped_reason="empty vault")
        assert r.notes_scanned == 42
        assert r.edges_upserted == 7
        assert r.skipped_reason == "empty vault"

    def test_sync_result_defaults(self) -> None:
        r = SyncResult(ok=True, mode="pull-only", detail="")
        assert r.ok is True
        assert r.detail == ""

    def test_sync_result_failure_shape(self) -> None:
        r = SyncResult(ok=False, mode="pull-only", detail="ob: not logged in")
        assert r.ok is False
        assert r.mode == "pull-only"
        assert r.detail == "ob: not logged in"
