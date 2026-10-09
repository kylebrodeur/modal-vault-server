"""Runtime-mutable sync posture: pull-only | sync-on-write | continuous.

The mode is a BEARER-gated runtime decision persisted in the state dir
(`sync-mode.json`), not an env var: flipping it must not require a redeploy
or container restart. Continuous mode runs `ob --continuous` as an owned
worker subprocess (stopped on mode-change and shutdown); the SyncService
lock keeps /vault single-writer across all postures.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

MODE_PULL_ONLY = "pull-only"
MODE_SYNC_ON_WRITE = "sync-on-write"
MODE_CONTINUOUS = "continuous"
MODES = (MODE_PULL_ONLY, MODE_SYNC_ON_WRITE, MODE_CONTINUOUS)

_STATE_FILE = "sync-mode.json"
_STDERR_TAIL_CHARS = 500


def current_mode(state_dir: Path, default: str = MODE_PULL_ONLY) -> str:
    """The persisted mode; corrupt/missing state reads as the given default."""
    try:
        record = json.loads((state_dir / _STATE_FILE).read_text(encoding="utf-8"))
        mode = record.get("mode")
        if mode in MODES:
            return str(mode)
    except (OSError, json.JSONDecodeError):
        pass
    return default if default in MODES else MODE_PULL_ONLY


def set_mode(state_dir: Path, mode: str, *, by: str = "") -> dict:
    """Persist the mode; the record notes who flipped it (session provenance)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    from datetime import UTC, datetime

    record = {"mode": mode, "set_at": datetime.now(UTC).isoformat(), "by": by}
    (state_dir / _STATE_FILE).write_text(json.dumps(record), encoding="utf-8")
    return record


class ContinuousWorker:
    """Owner of the `ob --continuous` daemon subprocess in a mode's lifetime.

    One lock, one child; starting when already running is a no-op, stopping
    waits for a clean exit (SIGTERM via process termination) so the ob
    workspace lock releases before any pull/write path runs.
    """

    def __init__(self, workspace_dir: Path, ob_bin: str = "ob") -> None:
        self._workspace_dir = workspace_dir
        self._ob_bin = ob_bin
        self._child: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._xdg: str = ""

    def bind_env(self, state_dir: Path) -> None:
        """The ob env the child must run with (XDG_CONFIG_HOME on the Volume)."""
        self._xdg = str(state_dir)

    def is_running(self) -> bool:
        return self._child is not None and self._child.poll() is None

    def started_at(self) -> str | None:
        return getattr(self, "_started_at", None)

    def start(self) -> dict:
        import time as _time
        from datetime import UTC, datetime

        env = dict(_env_for(self._workspace_dir, self._xdg or str(self._workspace_dir / "state")))
        with self._lock:
            if self.is_running():
                return {"running": True, "pid": self._child.pid, "started_at": self.started_at()}
            self._child = subprocess.Popen(
                [self._ob_bin, "sync", "--path", str(self._workspace_dir), "--continuous"],
                cwd=self._workspace_dir,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._started_at = datetime.now(UTC).isoformat()
            _time.sleep(0.5)  # ob exits fast on a bad state; catch it before we claim success
            if self._child.poll() is not None:
                code = self._child.returncode
                self._child = None
                self._started_at = None
                raise RuntimeError(f"ob --continuous exited immediately (code {code})")
            return {"running": True, "pid": self._child.pid, "started_at": self._started_at}

    def stop(self, timeout: int = 10) -> dict:
        with self._lock:
            child = self._child
            if child is None or child.poll() is not None:
                self._child = None
                self._started_at = None
                return {"running": False}
            child.terminate()
            try:
                child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=timeout)
            self._child = None
            self._started_at = None
            return {"running": False}


def _env_for(workspace_dir: Path, state_dir: str) -> dict[str, str]:
    """ob's real env: XDG_CONFIG_HOME is the state knob; OB_STATE stays for compat."""
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = state_dir
    env["OB_STATE"] = state_dir
    return env


def apply_mode(
    mode: str,
    state_dir: Path,
    workspace_dir: Path,
    sync: Any | None = None,
    worker: ContinuousWorker | None = None,
    one_shot_timeout: int = 1800,
) -> dict:
    """Make the mode real: continuous starts the daemon; the others stop it.

    `sync` is the SyncService-compatible object (duck-typed: one_shot with
    timeout_seconds); `worker` is the ContinuousWorker this process owns.
    """
    if worker is not None:
        worker.bind_env(state_dir)
        if mode == MODE_CONTINUOUS:
            worker.start()
        else:
            worker.stop()
    result = None
    if sync is not None and mode != MODE_CONTINUOUS:
        result = sync.one_shot(one_shot_timeout)  # type: ignore[attr-defined]
    set_mode(state_dir, mode)
    return {"mode": mode, "running": (mode == MODE_CONTINUOUS), "result": result}
