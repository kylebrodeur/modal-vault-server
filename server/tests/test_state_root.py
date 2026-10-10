"""State-root + sync-setup contracts from the writing-duo deploy lessons:
`ob` reads XDG_CONFIG_HOME (never OB_STATE), and sync-setup takes --path.

The shim records `xdg=<value>` per line, so these test the actual env the
real binary would receive.
"""

from __future__ import annotations

from typing import NamedTuple

import pytest

from server.config import Config
from server.sync_service import SyncService
from server.tests.fake_ob import install
from server.web import run_boot


class Env(NamedTuple):
    workspace: object
    state: object
    log: object
    sync: SyncService


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Env:
    install(tmp_path / "bin")
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    log = tmp_path / "ob.log"
    monkeypatch.setenv(
        "PATH", f"{tmp_path / 'bin'}{__import__('os').pathsep}{__import__('os').environ.get('PATH', '')}"
    )
    monkeypatch.setenv("OB_FAKE_LOG", str(log))
    monkeypatch.setenv("OB_FAKE_WHOAMI", "1")
    return Env(workspace, state, log, SyncService(workspace, state))


def _cfg(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> Config:
    for key, value in {"email": "a@b.c", "password": "pw", "vault": "V"}.items():
        value = overrides.pop(key, value)
        monkeypatch.setenv("MODAL_VAULT_OB_" + key.upper(), value)
    for key, value in overrides.items():
        name = {"e2e_password": "MODAL_VAULT_OB_E2E_PASSWORD", "mfa": "MODAL_VAULT_OB_MFA"}.get(
            key, "MODAL_VAULT_OB_" + key.upper()
        )
        monkeypatch.setenv(name, value) if value else monkeypatch.delenv(name, raising=False)
    return Config.load()


class TestStateRoot:
    def test_every_ob_invocation_carries_xdg_state_dir(self, env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
        run_boot(_cfg(monkeypatch), env.sync)
        for line in env.log.read_text().splitlines():
            if not line.startswith("start "):
                continue  # stdin echo lines carry no xdg field
            assert f"xdg={env.state.resolve()}" in line, line


class TestSyncSetupPath:
    def test_sync_setup_carries_path_of_the_workspace(self, env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
        run_boot(_cfg(monkeypatch), env.sync)
        setup_line = next(line for line in env.log.read_text().splitlines() if line.startswith("start sync-setup"))
        assert f"--path {env.workspace.resolve()}" in setup_line
        assert "--vault V" in setup_line

    def test_standard_vault_sync_setup_has_no_password(self, env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
        run_boot(_cfg(monkeypatch), env.sync)
        setup_line = next(line for line in env.log.read_text().splitlines() if line.startswith("start sync-setup"))
        assert "--password" not in setup_line

    def test_e2e_vault_sync_setup_carries_password(self, env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
        run_boot(_cfg(monkeypatch, e2e_password="e2e-secret"), env.sync)
        setup_line = next(line for line in env.log.read_text().splitlines() if line.startswith("start sync-setup"))
        assert "--password e2e-secret" in setup_line
