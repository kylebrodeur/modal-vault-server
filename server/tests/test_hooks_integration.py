"""Hooks + env-fallback contract tests: the integration seam lanes build on.

Guarantees that matter: unknown-tag registration refuses; hook errors are
contained + reported (never break boot/write); every knob is a single
MODAL_VAULT_* name (no legacy short-name fallback).
"""

from __future__ import annotations

import pytest

from server import hooks
from server.config import Config


class TestHookRegistry:
    def test_register_and_fire_in_order(self) -> None:
        calls: list[str] = []
        hooks.reset()
        hooks.register(hooks.TAG_BOOT_POST, lambda cfg, report: calls.append("first"))
        hooks.register(hooks.TAG_BOOT_POST, lambda cfg, report: calls.append("second"))
        errors = hooks.fire(hooks.TAG_BOOT_POST, object(), {})
        assert calls == ["first", "second"]
        assert errors == []

    def test_on_decorator_registers_and_preserves_fn(self) -> None:
        calls: list[str] = []

        @hooks.on(hooks.TAG_WRITE_POST)
        def lane_logger(report: dict) -> None:
            calls.append(report["path"])

        assert callable(lane_logger)  # the decorated name stays the function
        errors = hooks.fire(hooks.TAG_WRITE_POST, {"path": "note.md"})
        assert calls == ["note.md"]
        assert errors == []
        hooks.reset()

    def test_unknown_tag_refuses(self) -> None:
        hooks.reset()
        with pytest.raises(ValueError, match="unknown hook tag"):
            hooks.register("made.up.tag", lambda: None)

    def test_hook_error_contained_and_reported(self) -> None:
        hooks.reset()
        seen: list[dict] = []

        def broken(report: dict) -> None:
            raise RuntimeError("lane exploded")

        hooks.register(hooks.TAG_WRITE_POST, broken)
        hooks.register(hooks.TAG_WRITE_POST, lambda report: seen.append(report))
        errors = hooks.fire(hooks.TAG_WRITE_POST, {"written": True})
        assert seen == [{"written": True}]  # the healthy hook still ran
        assert len(errors) == 1 and "RuntimeError" in errors[0] and "lane exploded" in errors[0]
        assert hooks.last_errors(hooks.TAG_WRITE_POST) == errors

    def test_reset_clears(self) -> None:
        hooks.reset()
        hooks.register(hooks.TAG_BOOT_PRE, lambda cfg, sync: None)
        hooks.reset()
        assert hooks.registrations(hooks.TAG_BOOT_PRE) == []


class TestConfigNameContract:
    def test_prefixed_ob_names_resolve(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in (
            "MODAL_VAULT_OB_EMAIL",
            "MODAL_VAULT_OB_PASSWORD",
            "MODAL_VAULT_OB_MFA",
            "MODAL_VAULT_OB_VAULT",
            "MODAL_VAULT_OB_E2E_PASSWORD",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("MODAL_VAULT_OB_EMAIL", "lane@name")
        monkeypatch.setenv("MODAL_VAULT_OB_PASSWORD", "lane-pw")
        monkeypatch.setenv("MODAL_VAULT_OB_VAULT", "LaneVault")
        cfg = Config.load()
        assert (cfg.ob_email, cfg.ob_password, cfg.ob_vault) == ("lane@name", "lane-pw", "LaneVault")
        assert cfg.has_ob_credentials is True

    def test_bare_ob_names_are_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No legacy short-name fallback: only MODAL_VAULT_* resolves."""
        for name in ("MODAL_VAULT_OB_EMAIL", "MODAL_VAULT_OB_PASSWORD"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("OB_EMAIL", "short@name")
        monkeypatch.setenv("OB_PASSWORD", "short-pw")
        assert Config.load().has_ob_credentials is False

    def test_unset_means_no_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("MODAL_VAULT_OB_EMAIL", "MODAL_VAULT_OB_PASSWORD"):
            monkeypatch.delenv(name, raising=False)
        assert Config.load().has_ob_credentials is False
