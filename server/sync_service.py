"""`ob` CLI orchestration for the vault clone.

Contracts verified against the real `obsidian-headless` binary (0.0.12 /
0.0.14, verified live by the writing-duo deploy) - three of them are the
opposite of what the TTY-era docs suggest:

- `ob whoami` does not exist: login state is present iff the token file
  `$XDG_CONFIG_HOME/obsidian-headless/auth_token` exists.
- `ob sync` takes only `--path` / `--continuous`: `--mode pull-only` is
  INVALID on `sync`. Pull-only is a CONFIGURATION
  (`ob sync-config --mode pull-only --path <dir>`), persisted in the
  sync config; the pull itself is a bare `ob sync --path <dir>`.
- `ob login` reads ARGV flags (`--email/--password`, `--mfa` when set):
  stdin-piped login exits 0 but silently persists nothing in a
  container. Argv credentials are accepted inside a single-tenant
  container (`ps` is only visible to that container's own processes).
- `XDG_CONFIG_HOME` is the only state knob (`OB_STATE` is inert); the
  state dir must EXIST before any `ob` run (ob creates only its own
  subdir under an existing XDG root, never the parent).
- `sync-setup` defaults to BIDIRECTIONAL: this service always follows it
  with `sync-config --mode pull-only` (the server never pushes).

Every invocation is serialized with a process-wide lock: `ob` holds a
workspace lock on the clone directory, so concurrent `ob` processes
would fight over it.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import threading
from pathlib import Path

from server.types import SyncResult

_STDERR_TAIL_CHARS = 500  # failure detail: last 500 chars of stderr, enough to diagnose
_STATE_SUBDIR = "obsidian-headless"  # ob's own subdir under XDG_CONFIG_HOME
_TOKEN_FILE = "auth_token"


def _stderr_text(stderr: str | bytes | None) -> str:
    """Stderr as text. TimeoutExpired carries raw BYTES on POSIX (bpo-43431: text decoding
    is skipped on the timeout kill path), so decode with replacement before use."""
    if stderr is None:
        return ""
    if isinstance(stderr, bytes):
        return stderr.decode(errors="replace")
    return stderr


class SyncService:
    """Pull-sync lifecycle for the Headless Sync clone (state check / login / link / pull)."""

    def __init__(self, workspace_dir: Path, state_dir: Path, ob_bin: str = "ob") -> None:
        self._workspace_dir = workspace_dir
        self._state_dir = state_dir
        self._ob_bin = ob_bin
        self._lock = threading.Lock()

    # -- public API ------------------------------------------------------------

    def is_logged_in(self) -> bool:
        """True when ob's token file exists under the XDG state root.

        No subprocess: the file check IS the login-state truth (the real
        binary has no `whoami`; a shell-out would just re-read the file).
        """
        return self.token_file().is_file()

    def bootstrap(self, email: str, password: str, mfa: str = "") -> None:
        """Run `ob login` for first-boot provisioning; raises CalledProcessError on failure.

        Credentials ride argv flags - the form the real binary actually
        honors (stdin-piped login exits 0 but silently persists nothing
        outside a TTY). Accepted inside a single-tenant container: `ps` is
        only visible to that container's own processes, and the flag form
        is what the proven integration path uses.
        """
        with self._lock:
            self._ensure_state_dir()
            argv = [self._ob_bin, "login", "--email", email, "--password", password]
            if mfa:
                argv += ["--mfa", mfa]
            self._run(argv, check=True)

    def link_vault(self, vault_name: str, e2e_password: str = "", device_name: str = "modal-vault-server") -> None:
        """Run `ob sync-setup` once after login; raises CalledProcessError on failure.

        Associates this clone/state pair with the named Obsidian Sync vault.
        `e2e_password` rides `--password` when the vault is e2e-encrypted;
        standard vaults omit it (omission = standard encryption). After
        setup, `sync-config --mode pull-only` makes the mode durable so the
        clone can never push (setup defaults to bidirectional).
        """
        with self._lock:
            self._ensure_state_dir()
            argv = [
                self._ob_bin,
                "sync-setup",
                "--vault",
                vault_name,
                "--path",
                str(self._workspace_dir),
                "--device-name",
                device_name,
            ]
            if e2e_password:
                argv += ["--password", e2e_password]
            self._run(argv, check=True)
            self._run(
                [self._ob_bin, "sync-config", "--mode", "pull-only", "--path", str(self._workspace_dir)],
                check=True,
            )
            # Keep git metadata out of Sync: the shadow repo's .git never rides upstream.
            self._run(
                [
                    self._ob_bin,
                    "sync-config",
                    "--path",
                    str(self._workspace_dir),
                    "--excluded-folders",
                    ".git",
                ],
                check=True,
            )

    def one_shot(self, timeout_seconds: int = 1800) -> SyncResult:
        """One bare `ob sync --path <dir>` pull; failures degrade to a SyncResult (never raises).

        The mode is whatever the sync config says (pull-only, set by
        link_vault at set-up time); passing `--mode` here would be invalid.
        """
        with self._lock:
            try:
                proc = self._run(
                    [self._ob_bin, "sync", "--path", str(self._workspace_dir)], timeout_seconds=timeout_seconds
                )
            except subprocess.TimeoutExpired as exc:
                return SyncResult(ok=False, mode="pull-only", detail=_stderr_text(exc.stderr)[-_STDERR_TAIL_CHARS:])
            if proc.returncode == 0:
                return SyncResult(ok=True, mode="pull-only", detail="")
            return SyncResult(ok=False, mode="pull-only", detail=_stderr_text(proc.stderr)[-_STDERR_TAIL_CHARS:])

    def exclude_paths(self, paths: tuple[str, ...] = (".git",)) -> None:
        """Best-effort: exclude paths from ob sync (the shadow repo's .git never rides Sync).

        `ob sync-config --excluded-folders …`. Contained: when the clone is not
        yet linked, ob fails and we swallow it - `link_vault` re-applies after
        every successful setup.
        """
        with self._lock, contextlib.suppress(subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            self._run(
                [
                    self._ob_bin,
                    "sync-config",
                    "--path",
                    str(self._workspace_dir),
                    "--excluded-folders",
                    ",".join(paths),
                ],
                check=True,
            )

    # -- internals -------------------------------------------------------------

    def token_file(self) -> Path:
        """The ob auth-token path (login-state truth): `<state>/obsidian-headless/auth_token`."""
        return self._state_dir / _STATE_SUBDIR / _TOKEN_FILE

    def _ensure_state_dir(self) -> None:
        """ob will not mkdir the XDG parent; it only creates its own subdir under it."""
        self._state_dir.mkdir(parents=True, exist_ok=True)

    def _env(self) -> dict[str, str]:
        """Inherited env plus the state wiring `ob` actually reads.

        obsidian-headless reads only `XDG_CONFIG_HOME` (+ the
        `OBSIDIAN_AUTH_TOKEN` override); `OB_STATE` is inert and stays set
        only for forward-compat.
        """
        env = dict(os.environ)
        env["OB_STATE"] = str(self._state_dir)
        env["XDG_CONFIG_HOME"] = str(self._state_dir)
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
