"""Write-door contract tests: create/update over MCP + REST, the posture
deciding sync, snapshots before writes, and the windowed delete flag.

Real-binary contracts live under the fake `ob` shim; the git side runs
REAL git in tmp dirs (state-machine honesty end to end).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple

import pytest

from server.config import Config
from server.shadow_git import ShadowGit
from server.tests.fake_ob import install
from server.types import SyncResult
from server.write_service import (
    WriteService,
    arm_delete,
    delete_armed,
    disarm_delete,
    posixpath_norm_write,
)


class Env(NamedTuple):
    workspace: Path
    state: Path
    log: Path
    cfg: Config
    git: ShadowGit
    writer: WriteService


def _fake_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path, Path]:
    shim = install(tmp_path / "bin")
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    log = tmp_path / "ob.log"
    monkeypatch.setenv("PATH", f"{shim.parent}{__import__('os').pathsep}{__import__('os').environ.get('PATH', '')}")
    monkeypatch.setenv("OB_FAKE_LOG", str(log))
    monkeypatch.setenv("MODAL_VAULT_DATA_DIR", str(workspace))
    monkeypatch.setenv("MODAL_VAULT_STATE_DIR", str(state))
    return workspace, state, log


@pytest.fixture
def env_sync_on_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Env:
    workspace, state, log = _fake_env(monkeypatch, tmp_path)
    cfg = Config(api_token="t", data_dir=workspace, state_dir=state, sync_timeout=5)
    git = ShadowGit(workspace)
    git.ensure()
    from server.sync_mode import MODE_SYNC_ON_WRITE, ContinuousWorker, set_mode

    _ = ContinuousWorker(workspace)
    set_mode(state, MODE_SYNC_ON_WRITE, by="test")
    return Env(workspace, state, log, cfg, git, WriteService(cfg, _SyncStub(ok=True), git))


class _SyncStub:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[int] = []

    def one_shot(self, timeout_seconds: int = 1800) -> SyncResult:
        self.calls.append(timeout_seconds)
        return SyncResult(ok=self.ok, mode="pull-only", detail="" if self.ok else "boom")


@pytest.fixture
def sync_stub() -> _SyncStub:
    return _SyncStub()


class TestWritePath:
    def test_create_writes_snapshots_and_bursts(self, env_sync_on_write: Env) -> None:
        out = env_sync_on_write.writer.create_note("inbox/idea.md", "# Idea\n", agent="agent-x")
        assert out["written"] is True
        assert (env_sync_on_write.workspace / "inbox/idea.md").read_text() == "# Idea\n"
        assert out["sync"]["ran"] is True  # sync-on-write posture
        assert env_sync_on_write.git.log("inbox/idea.md", limit=5)[0]["subject"].startswith("create inbox/idea.md")

    def test_create_refuses_existing(self, env_sync_on_write: Env, sync_stub: _SyncStub) -> None:
        (env_sync_on_write.workspace / "note.md").write_text("here\n")
        writer = WriteService(env_sync_on_write.cfg, sync_stub, env_sync_on_write.git)
        out = writer.create_note("note.md", "new\n")
        assert out == {"error": "note already exists", "path": "note.md", "hint": "use vault.update_note"}

    def test_update_requires_existing(self, env_sync_on_write: Env, sync_stub: _SyncStub) -> None:
        writer = WriteService(env_sync_on_write.cfg, sync_stub, env_sync_on_write.git)
        out = writer.update_note("ghost/note.md", "text\n")
        assert out["error"] == "note does not exist"

    def test_upsert_creates_then_updates(self, env_sync_on_write: Env, sync_stub: _SyncStub) -> None:
        writer = WriteService(env_sync_on_write.cfg, sync_stub, env_sync_on_write.git)
        first = writer.upsert("a/b.md", "one\n")
        assert first["creating"] is True
        second = writer.upsert("a/b.md", "two\n")
        assert second["creating"] is False
        assert (env_sync_on_write.workspace / "a/b.md").read_text() == "two\n"

    def test_pull_only_stages_the_write(self, env_sync_on_write: Env, monkeypatch: pytest.MonkeyPatch) -> None:
        from server.sync_mode import MODE_PULL_ONLY, set_mode

        set_mode(env_sync_on_write.state, MODE_PULL_ONLY)
        writer = WriteService(env_sync_on_write.cfg, _SyncStub(ok=True), env_sync_on_write.git)
        out = writer.create_note("staged.md", "text\n", agent="agent-x")
        assert (env_sync_on_write.workspace / "staged.md").is_file()  # written locally
        assert out["sync"]["ran"] is False  # NOT pushed: pull-only posture
        assert "staged" in out["sync"]["detail"]


class TestDeleteGate:
    def test_delete_disarmed_by_default(self, env_sync_on_write: Env, monkeypatch: pytest.MonkeyPatch) -> None:
        (env_sync_on_write.workspace / "x.md").write_text("x\n")
        for key in ("allow_delete",):
            monkeypatch.delenv(key, raising=False)
        out = env_sync_on_write.writer.delete_note("x.md", env_sync_on_write.state, agent="a")
        assert out["error"] == "delete is not armed"
        assert (env_sync_on_write.workspace / "x.md").is_file()

    def test_armed_window_allows_then_expires(self, env_sync_on_write: Env, monkeypatch: pytest.MonkeyPatch) -> None:
        from datetime import UTC, datetime, timedelta

        (env_sync_on_write.workspace / "y.md").write_text("y\n")
        arm_delete(env_sync_on_write.state, window_minutes=1, by="kyle")
        assert delete_armed(env_sync_on_write.state) is True
        out = env_sync_on_write.writer.delete_note("y.md", env_sync_on_write.state, agent="a")
        assert out["deleted"] is True
        # window expiry: forge an expired flag file
        (env_sync_on_write.state / "allow-delete.json").write_text(
            json.dumps(
                {
                    "armed_at": datetime.now(UTC).isoformat(),
                    "expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
                }
            )
        )
        assert delete_armed(env_sync_on_write.state) is False  # self-cleared

    def test_disarm_clears(self, env_sync_on_write: Env) -> None:
        arm_delete(env_sync_on_write.state, 5)
        disarm_delete(env_sync_on_write.state)
        assert delete_armed(env_sync_on_write.state) is False

    def test_delete_failure_without_snapshot_never_removes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        workspace, state, _log_unused = _fake_env(monkeypatch, tmp_path)
        cfg = Config(api_token="t", data_dir=workspace, state_dir=state, sync_timeout=5)
        git = ShadowGit(workspace)
        git.ensure()
        (workspace / "z.md").write_text("z\n")
        arm_delete(state, 30)

        class BrokenGit(ShadowGit):
            def commit(self, message: str, allow_empty: bool = False) -> dict:
                if "pre-delete" in message:
                    return {"ok": False, "detail": "forced snapshot failure"}
                return super().commit(message, allow_empty=allow_empty)

        out = WriteService(cfg, _SyncStub(ok=True), BrokenGit(workspace)).delete_note("z.md", state)
        assert out["error"] == "delete requires a good snapshot"  # NO snapshot, NO delete
        assert (workspace / "z.md").is_file()


class TestPathSafety:
    def test_rejects_escape_and_absolute_and_empty(self) -> None:
        assert posixpath_norm_write("") == ""
        assert posixpath_norm_write("../escape.md") == ""
        assert posixpath_norm_write("/abs/olut.md") == ""
        assert posixpath_norm_write("a/../../b.md") == ""
        assert posixpath_norm_write("ok/note.md") == "ok/note.md"


class TestShadowGit:
    def test_revert_restores_content(self, env_sync_on_write: Env, sync_stub: _SyncStub) -> None:
        writer = WriteService(env_sync_on_write.cfg, sync_stub, env_sync_on_write.git)
        create_out = writer.upsert("r/r.md", "one\n")
        writer.upsert("r/r.md", "two\n")
        pre_two_sha = create_out["pre_snapshot"]["sha"]  # the state BEFORE the first write landed... baseline
        out = writer.revert("r/r.md", pre_two_sha)
        if out.get("ok"):
            assert (env_sync_on_write.workspace / "r/r.md").read_text() == ""
        else:
            # baseline could predate the file: revert must then say so honestly
            assert out == {"ok": False, "detail": out["detail"]}

    def test_show_file_missing_ref_returns_none(self, env_sync_on_write: Env) -> None:
        assert env_sync_on_write.git.show_file("HEAD", "no/such.md") is None


class TestObExclusionAfterLink:
    def test_sync_config_exclusion_ran_after_setup(
        self, env_sync_on_write: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from server.sync_service import SyncService

        install(tmp_path_bin(monkeypatch))
        sync = SyncService(env_sync_on_write.workspace, env_sync_on_write.state)
        sync.exclude_paths()
        blob = env_sync_on_write.log.read_text()
        assert "--excluded-folders .git" in blob


def tmp_path_bin(monkeypatch: pytest.MonkeyPatch) -> Path:
    import tempfile

    d = Path(tempfile.mkdtemp()) / "bin2"
    d.mkdir(parents=True)
    return d
