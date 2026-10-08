"""`ob` CLI orchestration for the vault clone.

Every invocation is serialized with a process-wide lock: `ob` holds a workspace lock on
the clone directory, so concurrent `ob sync` processes would fight over it. Credentials
for `ob login` travel via stdin only — never argv (visible in `ps`) and never log output.
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

from server.types import SyncResult

_STDERR_TAIL_CHARS = 500  # failure detail: last 500 chars of stderr, enough to diagnose


def _stderr_text(stderr: str | bytes | None) -> str:
    """Stderr as text. TimeoutExpired carries raw BYTES on POSIX (bpo-43431: text decoding
    is skipped on the timeout kill path), so decode with replacement before use."""
    if stderr is None:
        return ""
    if isinstance(stderr, bytes):
        return stderr.decode(errors="replace")
    return stderr


class SyncService:
    """Pull-sync lifecycle for the Headless Sync clone (whoami / login / one-shot pull)."""

    def __init__(self, workspace_dir: Path, state_dir: Path, ob_bin: str = "ob") -> None:
        self._workspace_dir = workspace_dir
        self._state_dir = state_dir
        self._ob_bin = ob_bin
        self._lock = threading.Lock()

    # -- public API ------------------------------------------------------------

    def is_logged_in(self) -> bool:
        """True when `ob whoami` exits 0 (login state lives in state_dir)."""
        with self._lock:
            proc = self._run([self._ob_bin, "whoami"])
            return proc.returncode == 0

    def bootstrap(self, email: str, password: str) -> None:
        """Run `ob login` for first-boot provisioning; raises CalledProcessError on failure."""
        with self._lock:
            self._run([self._ob_bin, "login"], input=f"{email}\n{password}", check=True)

    def one_shot(self, timeout_seconds: int = 1800) -> SyncResult:
        """One `ob sync --mode pull-only` pull; failures degrade to a SyncResult (never raises)."""
        with self._lock:
            try:
                proc = self._run([self._ob_bin, "sync", "--mode", "pull-only"], timeout_seconds=timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                return SyncResult(ok=False, mode="pull-only", detail=_stderr_text(exc.stderr)[-_STDERR_TAIL_CHARS:])
            if proc.returncode == 0:
                return SyncResult(ok=True, mode="pull-only", detail="")
            return SyncResult(ok=False, mode="pull-only", detail=_stderr_text(proc.stderr)[-_STDERR_TAIL_CHARS:])

    # -- internals -------------------------------------------------------------

    def _env(self) -> dict[str, str]:
        """Inherited env plus OB_STATE so `ob` finds login state outside the clone."""
        env = dict(os.environ)
        env["OB_STATE"] = str(self._state_dir)
        return env

    def _run(
        self,
        argv: list[str],
        input: str | None = None,
        check: bool = False,
        timeout_seconds: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Single subprocess seam: capture always, text always, optional timeout.

        Raises TimeoutExpired when the timeout fires (caller decides degrade); raises
        CalledProcessError on nonzero exit only when check=True.
        """
        proc = subprocess.run(
            argv,
            cwd=self._workspace_dir,
            env=self._env(),
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        if check and proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, argv, output=proc.stdout, stderr=proc.stderr)
        return proc
