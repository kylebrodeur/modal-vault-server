"""Task 3: SyncService contracts exercised through the fake `ob` shim on PATH (no real binary)."""

import os
import subprocess
import threading
from pathlib import Path
from typing import NamedTuple

import pytest

from server.sync_service import SyncService
from server.tests.fake_ob import install
from server.types import SyncResult


class ObEnv(NamedTuple):
    """Harness bundle: shim-adjacent paths plus the service under test."""

    workspace: Path
    state: Path
    log: Path
    service: SyncService


@pytest.fixture
def ob_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ObEnv:
    """Install the fake `ob` on PATH and return the harness; tests flip behavior via OB_FAKE_MODE."""
    shim = install(tmp_path / "bin")
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    log = tmp_path / "ob.log"
    monkeypatch.setenv("PATH", f"{shim.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("OB_FAKE_LOG", str(log))
    return ObEnv(workspace=workspace, state=state, log=log, service=SyncService(workspace, state))


class TestIsLoggedIn:
    """The real binary has no `whoami`: the token file IS the login-state truth."""

    def test_fresh_state_is_false_and_touches_nothing(self, ob_env: ObEnv) -> None:
        assert ob_env.service.is_logged_in() is False
        assert not ob_env.log.exists()  # the check is a file test, no subprocess

    def test_state_file_present_is_true(self, ob_env: ObEnv) -> None:
        token_file = ob_env.state / "obsidian-headless" / "auth_token"
        token_file.parent.mkdir(parents=True)
        token_file.write_text("token-fake")
        assert ob_env.service.is_logged_in() is True


class TestBootstrap:
    def test_login_rides_argv_flags_and_persists_token(self, ob_env: ObEnv) -> None:
        """argv-flag login is the form the real binary honors (stdin no-ops)."""
        ob_env.service.bootstrap("agent@example.com", "s3cret-pass")
        blob = ob_env.log.read_text()
        line = blob.splitlines()[0]
        assert line.startswith("start login --email agent@example.com --password s3cret-pass ")
        assert f"cwd={ob_env.workspace.resolve()}" in line
        assert "stdin:" not in blob  # no stdin path anymore
        assert "persisted:auth_token" in blob  # the shim proves persistence happened

    def test_bootstrap_raises_on_failure_with_stderr(self, ob_env: ObEnv, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OB_FAKE_MODE", "fail")
        with pytest.raises(subprocess.CalledProcessError) as excinfo:
            ob_env.service.bootstrap("agent@example.com", "s3cret-pass")
        assert excinfo.value.returncode == 1
        assert "ERROR-LINE" in (excinfo.value.stderr or "")


class TestOneShot:
    def test_success_result_shape_and_argv(self, ob_env: ObEnv) -> None:
        result = ob_env.service.one_shot()
        assert result == SyncResult(ok=True, mode="pull-only", detail="")
        line = ob_env.log.read_text().splitlines()[0]
        assert line.startswith(f"start sync --path {ob_env.workspace.resolve()} ")
        assert "--mode" not in line  # INVALID on the real binary (takes only --path/--continuous)

    def test_failure_detail_is_stderr_tail_last_500_chars(self, ob_env: ObEnv, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OB_FAKE_MODE", "fail")
        result = ob_env.service.one_shot()
        assert result.ok is False
        assert result.mode == "pull-only"
        stderr_text = "".join(f"ERROR-LINE-{i:03d}-" + "a" * 42 + "\n" for i in range(27))
        assert len(stderr_text) > 500
        assert result.detail == stderr_text[-500:]
        assert len(result.detail) == 500

    def test_timeout_degrades_to_failed_result_not_exception(
        self, ob_env: ObEnv, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # TimeoutExpired.stderr arrives as raw bytes on POSIX (bpo-43431, text decoding
        # skipped on the kill path); the service decodes it — detail shows the shim's
        # pre-hang stderr line, never loses it to an isinstance(str) check.
        monkeypatch.setenv("OB_FAKE_MODE", "hang")
        result = ob_env.service.one_shot(timeout_seconds=1)
        assert result.ok is False
        assert result.mode == "pull-only"
        assert "HUNG-STDERR-LINE" in result.detail


class TestSerialization:
    def test_two_concurrent_one_shots_do_not_overlap(self, ob_env: ObEnv, monkeypatch: pytest.MonkeyPatch) -> None:
        """Lock contract: with a 0.2s shim, argv-file ordering must be start,end,start,end."""
        monkeypatch.setenv("OB_FAKE_MODE", "slow")
        results: list[SyncResult] = []

        def run() -> None:
            results.append(ob_env.service.one_shot())

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert [r.ok for r in results] == [True, True]
        # start/end ordering per invocation; 'pulled' progress lines interleaved
        flow = [line.split(" ")[0] for line in ob_env.log.read_text().splitlines()]
        assert flow == ["start", "pulled", "end", "start", "pulled", "end"]
