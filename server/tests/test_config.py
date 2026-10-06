from pathlib import Path
from typing import Any

import pytest

from server.config import Config
from server.types import EdgeRow, IndexResult, NoteRow, SyncResult


class TestConfigDefaults:
    """Config.load() with no VAULT_* env set."""

    def test_load_returns_config_without_any_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VAULT_API_TOKEN", raising=False)
        monkeypatch.delenv("VAULT_EMBED_URL", raising=False)
        cfg = Config.load()
        assert isinstance(cfg, Config)

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VAULT_EMBED_URL", raising=False)
        cfg = Config.load()
        assert cfg.embed_model == "embeddinggemma"
        assert cfg.embed_dim == 768
        assert cfg.sync_mode == "pull-only"
        assert cfg.sync_timeout == 1800

    def test_embed_url_none_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VAULT_EMBED_URL", raising=False)
        cfg = Config.load()
        assert cfg.embed_url is None

    def test_embed_api_token_none_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VAULT_EMBED_API_TOKEN", raising=False)
        cfg = Config.load()
        assert cfg.embed_api_token is None

    def test_dir_defaults_derive_from_data_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VAULT_DATA_DIR", "/custom/vault")
        monkeypatch.delenv("VAULT_INDEX_DIR", raising=False)
        monkeypatch.delenv("VAULT_STATE_DIR", raising=False)
        cfg = Config.load()
        assert cfg.data_dir == Path("/custom/vault")
        assert cfg.index_dir == Path("/custom/vault/index")
        assert cfg.state_dir == Path("/custom/vault/state")

    def test_api_token_defaults_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing VAULT_API_TOKEN does not raise at load; fail-closed-at-boot is app.py's job."""
        monkeypatch.delenv("VAULT_API_TOKEN", raising=False)
        assert Config.load().api_token == ""


class TestConfigEnvOverrides:
    def test_all_env_overrides(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        data, index, state = tmp_path / "vault", tmp_path / "index", tmp_path / "state"
        monkeypatch.setenv("VAULT_API_TOKEN", "tok-123")
        monkeypatch.setenv("VAULT_DATA_DIR", str(data))
        monkeypatch.setenv("VAULT_INDEX_DIR", str(index))
        monkeypatch.setenv("VAULT_STATE_DIR", str(state))
        monkeypatch.setenv("VAULT_EMBED_URL", "http://embed.internal:8000")
        monkeypatch.setenv("VAULT_EMBED_API_TOKEN", "embed-tok-9")
        monkeypatch.setenv("VAULT_EMBED_MODEL", "bge-small")
        monkeypatch.setenv("VAULT_EMBED_DIM", "384")
        monkeypatch.setenv("VAULT_SYNC_MODE", "push")
        monkeypatch.setenv("VAULT_SYNC_TIMEOUT", "60")

        cfg = Config.load()
        assert cfg.api_token == "tok-123"
        assert cfg.data_dir == data
        assert cfg.index_dir == index
        assert cfg.state_dir == state
        assert cfg.embed_url == "http://embed.internal:8000"
        assert cfg.embed_api_token == "embed-tok-9"
        assert cfg.embed_model == "bge-small"
        assert cfg.embed_dim == 384
        assert cfg.sync_mode == "push"
        assert cfg.sync_timeout == 60

    def test_embed_url_set_round_trips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VAULT_EMBED_URL", "http://localhost:9999/embed")
        assert Config.load().embed_url == "http://localhost:9999/embed"

    def test_explicit_dir_override_wins_over_derivation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VAULT_DATA_DIR", "/custom/vault")
        monkeypatch.setenv("VAULT_STATE_DIR", "/elsewhere/state")
        cfg = Config.load()
        assert cfg.state_dir == Path("/elsewhere/state")

    def test_token_values_round_trip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VAULT_API_TOKEN", "  secret-with-specials_#@$  ")
        monkeypatch.setenv("VAULT_EMBED_API_TOKEN", "e-tok")
        cfg = Config.load()
        assert cfg.api_token == "  secret-with-specials_#@$  "
        assert cfg.embed_api_token == "e-tok"


class TestTypes:
    def test_note_row_fields(self) -> None:
        row = NoteRow(
            id="projects/idea.md",
            vector=[0.1, 0.2],
            text="# Idea",
            frontmatter={"tags": ["a"]},
            sha256="abc",
            embedded_at="2026-10-06T00:00:00+00:00",
        )
        assert row.id == "projects/idea.md"
        assert row.frontmatter == {"tags": ["a"]}

    def test_note_row_frozen(self) -> None:
        row = NoteRow(id="x.md", vector=[0.0], text="t", frontmatter={}, sha256="s", embedded_at="now")
        with pytest.raises((AttributeError, TypeError)):
            row.id = "y.md"  # type: ignore[misc]

    def test_edge_row_frozen(self) -> None:
        edge = EdgeRow(src="a.md", dst="b.md", kind="note-link", raw="[[b]]")
        with pytest.raises((AttributeError, TypeError)):
            edge.src = "z.md"  # type: ignore[misc]

    def test_index_result_defaults(self) -> None:
        r = IndexResult()
        assert r.notes_embedded == 0
        assert r.notes_removed == 0
        assert r.edges_upserted == 0
        assert r.skipped_reason is None

    def test_sync_result_defaults(self) -> None:
        r = SyncResult(ok=True, mode="pull-only", detail="")
        assert r.ok is True
        assert r.detail == ""
