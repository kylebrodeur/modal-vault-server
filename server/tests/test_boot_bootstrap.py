"""First-boot bootstrap-from-secret contracts (the PVM headless-sync
learnings, corrected by the real-binary deploy findings of 2026-10-08):
argv-flag login (stdin login silently no-ops), state-dir pre-creation,
and the pull is a BARE `sync --path` (never `--mode`).

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
    log = tmp_path / "ob.log"
    monkeypatch.setenv("PATH", f"{shim.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("OB_FAKE_LOG", str(log))
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
        env_name = {"e2e_password": "VAULT_OB_E2E_PASSWORD", "mfa": "VAULT_OB_MFA"}.get(key, "VAULT_OB_" + key.upper())
        if value:
            monkeypatch.setenv(env_name, value)
        else:
            monkeypatch.delenv(env_name, raising=False)
    return Config.load()


class TestFirstBootBootstrap:
    def test_login_links_configs_then_bare_pull_in_order(
        self, boot_env: BootEnv, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The proven order: login -> sync-setup -> sync-config pull-only -> bare pull."""
        cfg = _cfg(boot_env, monkeypatch, vault="MyVault", e2e_password="secret-e2e")
        entry = run_boot(cfg, boot_env.sync)
        starts = [line for line in boot_env.log.read_text().splitlines() if line.startswith("start")]
        heads = [line.split(" cwd=")[0] for line in starts]
        setup_expected = (
            f"start sync-setup --vault MyVault --path {boot_env.workspace}"
            " --device-name modal-vault-server --password secret-e2e"
        )
        assert heads == [
            "start login --email kyle@example.com --password hunter2",
            setup_expected,
            f"start sync-config --mode pull-only --path {boot_env.workspace}",
            f"start sync --path {boot_env.workspace}",
        ]
        assert entry["bootstrapped"] is True
        assert entry["sync"].ok is True
        # the login PERSISTED a token (file-based state on the state dir)
        assert boot_env.sync.token_file().is_file()

    def test_mfa_rides_argv_when_set(self, boot_env: BootEnv, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _cfg(boot_env, monkeypatch, mfa="123456")
        run_boot(cfg, boot_env.sync)
        log = boot_env.log.read_text()
        login_line = next(line for line in log.splitlines() if line.startswith("start login"))
        assert "--mfa 123456" in login_line

    def test_no_mfa_omits_the_flag(self, boot_env: BootEnv, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _cfg(boot_env, monkeypatch, mfa="")
        run_boot(cfg, boot_env.sync)
        login_line = next(line for line in boot_env.log.read_text().splitlines() if line.startswith("start login"))
        assert "--mfa" not in login_line

    def test_e2e_password_rides_sync_setup_argv(self, boot_env: BootEnv, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _cfg(boot_env, monkeypatch, vault="Encrypted", e2e_password="e2e-secret")
        run_boot(cfg, boot_env.sync)
        setup_line = next(line for line in boot_env.log.read_text().splitlines() if line.startswith("start sync-setup"))
        assert "--password e2e-secret" in setup_line
        assert "hunter2" not in setup_line  # the LOGIN password never leaks here

    def test_standard_vault_setup_then_pull_only_config(
        self, boot_env: BootEnv, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Standard vaults: no --password on setup; pull-only mode is CONFIGURED after it."""
        cfg = _cfg(boot_env, monkeypatch, vault="Plain", e2e_password="")  # standard vault
        entry = run_boot(cfg, boot_env.sync)
        log = boot_env.log.read_text()
        setup_line = next(line for line in log.splitlines() if line.startswith("start sync-setup"))
        assert "--password" not in setup_line
        assert "configured:pull-only" in log  # the durable mode setting
        assert entry["sync"].ok is True

    def test_never_passes_mode_flag_on_sync(self, boot_env: BootEnv, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _cfg(boot_env, monkeypatch)
        entry = run_boot(cfg, boot_env.sync)
        sync_lines = [line for line in boot_env.log.read_text().splitlines() if line.startswith("start sync ")]
        assert all("--mode" not in line for line in sync_lines)  # invalid on the real binary
        assert entry["sync"].ok is True

    def test_state_dir_pre_created_before_login(self, boot_env: BootEnv, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _cfg(boot_env, monkeypatch)
        run_boot(cfg, boot_env.sync)
        # the shim persists the token UNDER xdg (which is the state dir): ob only
        # creates its own subdir - the state dir itself must have existed already
        assert (boot_env.state / "obsidian-headless" / "auth_token").is_file()

    def test_missing_credentials_skips_login_pull_anyway(
        self, boot_env: BootEnv, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("VAULT_OB_EMAIL", raising=False)
        cfg = Config.load()
        entry = run_boot(cfg, boot_env.sync)
        log = boot_env.log.read_text()
        assert "start login" not in log
        assert "start sync --path" in log
        assert entry["bootstrapped"] is False

    def test_already_logged_in_skips_login(self, boot_env: BootEnv, monkeypatch: pytest.MonkeyPatch) -> None:
        _cfg(boot_env, monkeypatch)
        run_boot(_cfg(boot_env, monkeypatch), boot_env.sync)  # first boot logs in
        boot_env.log.write_text("")
        entry = run_boot(_cfg(boot_env, monkeypatch), boot_env.sync)  # second boot: token exists -> no login
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
        blob = (boot_env.state / "boot.json").read_text()
        assert "hunter2" not in blob and "kyle@example.com" not in blob

    def test_no_vault_name_skips_sync_setup(self, boot_env: BootEnv, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _cfg(boot_env, monkeypatch, vault="")  # email+password but no vault name
        run_boot(cfg, boot_env.sync)
        log = boot_env.log.read_text()
        assert "start login" in log
        assert "sync-setup" not in log
        assert "sync-config" not in log


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
