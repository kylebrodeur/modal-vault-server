"""Shadow git: a real git repo OVER the vault clone as the write-path safety net.

Before any MCP/API write is pushed upstream, the current clone state is
committed (`commit_before_write`) against a git repo at the clone root with
`.git` excluded from sync (--excluded-folders: ob never carries the metadata
upstream). Every write is therefore revertable per-write via
`revert(path, ref)` and inspectable via `log(path)`/`snapshots()`.

Fail-open for the WRITE but honest in the REPORT: a failed snapshot commit
does not block the write (the note content is the product), but the reply
records `snapshot: {ok: false, detail}` so operators see an unprotected
write instead of assuming one. Delete (when enabled) REQUIRES a good
snapshot: no snapshot, no delete.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

GIT_TIMEOUT = 60


class ShadowGit:
    """Git lifecycle over the clone: identity, ensure/init, per-write commits, lookups."""

    def __init__(self, clone_root: Path, author_name: str = "modal-vault-server") -> None:
        self._root = clone_root
        self._author = author_name

    # -- public API ------------------------------------------------------------

    def ensure(self) -> dict:
        """Init (once) + baseline; idempotent.

        Sync-side exclusion of `.git` is the deploy root's job (`ob
        sync-config --excluded-folders .git` after setup; see app.py) so the
        metadata never rides sync.
        """
        repo = self._root / ".git"
        if not (repo / "HEAD").is_file():
            self._git("init")
        baseline = self.commit("baseline: ensure a snapshot point exists", allow_empty=True)
        return {"git": "ready" if baseline["ok"] else "degraded", "last": baseline}

    def commit(self, message: str, allow_empty: bool = False) -> dict:
        """Stage ALL clone changes + commit; degraded (ok:False) on any git failure."""
        try:
            self._git("add", "-A")
            args = ["commit", "-m", message]
            if allow_empty:
                args.append("--allow-empty")
            proc = self._git(*args)
            sha = self._git("rev-parse", "HEAD").stdout.strip()
            return {"ok": True, "sha": sha, "detail": proc.stdout.strip()[:200]}
        except subprocess.SubprocessError as exc:
            return {"ok": False, "detail": str(exc)[:200]}

    def log(self, path: str | None = None, limit: int = 20) -> list[dict]:
        """Commit log (optionally for one path): oldest-last, capped."""
        args = ["log", f"--max-count={limit}", "--format=%H%x1f%aI%x1f%s"]
        if path:
            args += ["--", path]
        out = self._git(*args).stdout.strip()
        rows = []
        for line in out.splitlines():
            sha, at, subject = [*line.split("\x1f"), "", ""][:3]
            rows.append({"sha": sha, "at": at, "subject": subject})
        return rows

    def show_file(self, ref: str, path: str) -> str | None:
        """One file's content at a ref; None when it did not exist there."""
        proc = self._run("show", f"{ref}:{path}")
        return proc.stdout if proc.returncode == 0 else None

    def revert_path(self, path: str, ref: str) -> dict:
        """Restore `path`'s content from `ref` + commit the restoration."""
        content = self.show_file(ref, path)
        if content is None:
            return {"ok": False, "detail": f"{path} does not exist at {ref}"}
        target = self._root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return self.commit(f"revert {path} to {ref[:10]}")

    # -- internals -------------------------------------------------------------

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        cmd = [
            "git",
            "-C",
            str(self._root),
            "-c",
            f"user.name={self._author}",
            "-c",
            f"user.email={self._author}@local",
            *args,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=GIT_TIMEOUT, check=True)
        return proc

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        cmd = ["git", "-C", str(self._root), *args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False)
