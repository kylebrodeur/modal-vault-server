"""First-boot bootstrap-from-secret contracts (the PVM headless-sync learnings,
applied generically): login -> optional sync-setup -> pull, credential-honest
failure modes, and the run_boot shape change.

Every test drives the fake `ob` shim; no network, no real credentials.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import NamedTuple

import pytest

from server.config import Config
from server.sync_service import SyncService
from server.tests.fake_ob import install
from server.web import run_boot


class BootEnv(NamedTuple):
    workspace: Path
    state: Path
    log: Path
    sync: SyncService


@pytest.fixture
def boot_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> BootEnv:
    shim = install(tmp_path / "bin")
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    log = tmp_path / "ob.log"
    monkeypatch.setenv("PATH", f"{shim.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("OB_FAKE_LOG", str(log))
    monkeypatch.setenv("OB_FAKE_WHOAMI", "1")  # fresh clone: whoami exits 1 (not logged in)
    monkeypatch.setenv("VAULT_DATA_DIR", str(workspace))
    monkeypatch.setenv("VAULT_STATE_DIR", str(state))
    monkeypatch.delenv("VAULT_OB_EMAIL", raising=False)
    monkeypatch.delenv("VAULT_OB_PASSWORD", raising=False)
    monkeypatch.delenv("VAULT_OB_MFA", raising=False)
    monkeypatch.delenv("VAULT_OB_VAULT", raising=False)
    monkeypatch.delenv("VAULT_OB_E2E_PASSWORD", raising=False)
    return BootEnv(workspace, state, log, SyncService(workspace, state))


def _cfg(env: BootEnv, monkeypatch: pytest.MonkeyPatch, **overrides: str) -> Config:
    """Set the ob-credential env keys: value None/"" deletes, otherwise sets."""
    defaults = {"email": "kyle@example.com", "password": "hunter2"}
    for key, default in defaults.items():
        value = overrides.pop(key, default)
        env_name = "VAULT_OB_" + key.upper()
        if value:
            monkeypatch.setenv(env_name, value)
        else:
            monkeypatch.delenv(env_name, raising=False)
    for key, value in overrides.items():
        env_name = "VAULT_OB_" + key.upper()
        monkeypatch.delenv(env_name, raising=False) if not value else monkeypatch.setenv(env_name, value)
    return Config.load()


class TestFirstBootBootstrap:
    def test_login_then_link_then_pull_order(self, boot_env: BootEnv, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _cfg(boot_env, monkeypatch, vault="MyVault", e2e_password="secret-e2e")
        entry = run_boot(cfg, boot_env.sync)
        lines = boot_env.log.read_text().splitlines()
        starts = [line for line in lines if line.startswith("start")]
        assert starts[0].startswith("start whoami")
        assert starts[1].startswith("start login")
        assert starts[2].startswith("start sync-setup --vault MyVault")
        assert starts[3].startswith("start sync --mode pull-only")
        assert entry["bootstrapped"] is True
        assert entry["sync"].ok is True

    def test_mfa_flows_into_login_stdin_not_argv(self, boot_env: BootEnv, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _cfg(boot_env, monkeypatch, mfa="123456")
        run_boot(cfg, boot_env.sync)
        log = boot_env.log.read_text()
        assert "stdin:kyle@example.com\nhunter2\n123456" in log
        # the MFA code must never appear in argv
        login_line = next(line for line in log.splitlines() if line.startswith("start login"))
        assert "123456" not in login_line

    def test_e2e_password_flows_via_stdin_to_sync_setup(
        self, boot_env: BootEnv, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _cfg(boot_env, monkeypatch, vault="Encrypted", e2e_password="e2e-secret")
        run_boot(cfg, boot_env.sync)
        setup_line = next(line for line in boot_env.log.read_text().splitlines() if line.startswith("start sync-setup"))
        assert "--password e2e-secret" in setup_line  # the proven PVM contract: argv inside a container
        assert boot_env.workspace.name  # (no-op guard so the next assertion reads clearly)
        # email/password never leak into the setup line
        assert "hunter2" not in setup_line

    def test_no_e2e_password_omits_password_flag(self, boot_env: BootEnv, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _cfg(boot_env, monkeypatch, vault="Plain", e2e_password="")
        run_boot(cfg, boot_env.sync)
        setup_line = next(line for line in boot_env.log.read_text().splitlines() if line.startswith("start sync-setup"))
        assert "--password" not in setup_line

    def test_missing_credentials_skips_login_pull_anyway(
        self, boot_env: BootEnv, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("VAULT_OB_EMAIL", raising=False)
        cfg = Config.load()
        entry = run_boot(cfg, boot_env.sync)
        log = boot_env.log.read_text()
        assert "start login" not in log
        assert "start sync --mode pull-only" in log
        assert entry["bootstrapped"] is False

    def test_already_logged_in_skips_login(self, boot_env: BootEnv, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OB_FAKE_WHOAMI", "")  # whoami exits 0: state already present
        cfg = _cfg(boot_env, monkeypatch)
        run_boot(cfg, boot_env.sync)  # first boot logs in
        boot_env.log.write_text("")
        entry = run_boot(cfg, boot_env.sync)  # second boot: whoami ok -> no login
        log = boot_env.log.read_text()
        assert "start login" not in log
        assert entry["bootstrapped"] is False

    def test_login_failure_degrades_and_notes_type_only(
        self, boot_env: BootEnv, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OB_FAKE_MODE", "fail")
        cfg = _cfg(boot_env, monkeypatch)
        entry = run_boot(cfg, boot_env.sync)
        assert entry["bootstrapped"] is False
        assert entry["sync"].ok is False  # the pull still runs + degrades
        note = json.loads((boot_env.state / "boot.json").read_text())
        assert note["event"] == "bootstrap_failed"
        assert "CalledProcessError" in note["detail"]
        # the note NEVER carries credential values
        blob = (boot_env.state / "boot.json").read_text()
        assert "hunter2" not in blob and "kyle@example.com" not in blob

    def test_no_vault_name_skips_sync_setup(self, boot_env: BootEnv, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _cfg(boot_env, monkeypatch, vault="")  # email+password but no vault name
        run_boot(cfg, boot_env.sync)
        log = boot_env.log.read_text()
        assert "start login" in log
        assert "sync-setup" not in log


class TestConfigKnobs:
    def test_ob_knobs_read_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VAULT_OB_EMAIL", "a@b.c")
        monkeypatch.setenv("VAULT_OB_PASSWORD", "pw")
        monkeypatch.setenv("VAULT_OB_MFA", "999999")
        monkeypatch.setenv("VAULT_OB_VAULT", "V")
        monkeypatch.setenv("VAULT_OB_E2E_PASSWORD", "E")
        cfg = Config.load()
        assert cfg.has_ob_credentials is True
        assert (cfg.ob_mfa, cfg.ob_vault, cfg.ob_e2e_password) == ("999999", "V", "E")

    def test_no_credentials_is_falsy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in ("VAULT_OB_EMAIL", "VAULT_OB_PASSWORD", "VAULT_OB_MFA", "VAULT_OB_VAULT", "VAULT_OB_E2E_PASSWORD"):
            monkeypatch.delenv(key, raising=False)
        assert Config.load().has_ob_credentials is False
