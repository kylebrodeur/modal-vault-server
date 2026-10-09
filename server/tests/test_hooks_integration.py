"""Hooks + env-fallback contract tests: the integration seam lanes build on.

Guarantees that matter: unknown-tag registration refuses; hook errors are
contained + reported (never break boot/write); the OB_* short-name fallback
resolves with VAULT_OB_* precedence.
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


class TestConfigNameFallback:
    def test_short_ob_names_resolve(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("VAULT_OB_EMAIL", "VAULT_OB_PASSWORD", "OB_EMAIL", "OB_PASSWORD", "VAULT_OB_MFA", "OB_MFA"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("OB_EMAIL", "short@name")
        monkeypatch.setenv("OB_PASSWORD", "short-pw")
        monkeypatch.setenv("OB_VAULT", "ShortVault")
        cfg = Config.load()
        assert (cfg.ob_email, cfg.ob_password, cfg.ob_vault) == ("short@name", "short-pw", "ShortVault")
        assert cfg.has_ob_credentials is True

    def test_vault_prefixed_name_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OB_EMAIL", "short@name")
        monkeypatch.setenv("VAULT_OB_EMAIL", "prefixed@name")
        cfg = Config.load()
        assert cfg.ob_email == "prefixed@name"

    def test_prefix_only_when_unprefixed_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("VAULT_OB_EMAIL", "OB_EMAIL"):
            monkeypatch.delenv(name, raising=False)
        assert Config.load().has_ob_credentials is False
