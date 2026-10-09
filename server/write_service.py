"""The write door: create/update notes over the clone, snapshot-first.

Built on the runtime-decided posture (`server/sync_mode`): a write is
snapshot-committed to the shadow git, written to the clone, then - in
`sync-on-write` or `continuous` postures - one serialized `ob` burst carries
it upstream. In `pull-only` the write STAYS LOCAL (staged) and the reply says
so: pull-only is the safety posture, writes land only when the operator flips
the mode.

Deletion is NOT part of the steady-state tool surface. It exists only while
the windowed `allow_delete` runtime flag is armed (`arm_delete`); the flag
persists a self-expiry in the state dir, disarms itself on restart (a fresh
container reads an expired flag), and every delete REQUIRES a successful
pre-delete snapshot commit (no snapshot, no delete).
"""

from __future__ import annotations

import contextlib
import json
import posixpath
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from server.shadow_git import ShadowGit
from server.sync_mode import MODE_PULL_ONLY

_ARM_FILE = "allow-delete.json"
_DEFAULT_WINDOW_MINUTES = 60


def arm_delete(state_dir: Path, window_minutes: int = _DEFAULT_WINDOW_MINUTES, *, by: str = "") -> dict:
    """Arm the delete door for a window; re-arming refreshes the expiry."""
    state_dir.mkdir(parents=True, exist_ok=True)
    from datetime import timedelta

    now = datetime.now(UTC)
    payload = {
        "armed_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=max(window_minutes, 1))).isoformat(),
        "by": by,
    }
    _arm_path(state_dir).write_text(json.dumps(payload), encoding="utf-8")
    return {"allow_delete": True, "expires_at": payload["expires_at"]}


def disarm_delete(state_dir: Path) -> dict:
    """Disarm now."""
    with contextlib.suppress(OSError):
        _arm_path(state_dir).unlink()
    return {"allow_delete": False}


def delete_armed(state_dir: Path) -> bool:
    """True while the window is live; expired flags self-clear (restart-safe)."""
    try:
        record = json.loads(_arm_path(state_dir).read_text(encoding="utf-8"))
        if datetime.now(UTC) < datetime.fromisoformat(str(record.get("expires_at"))):
            return True
        _arm_path(state_dir).unlink()
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return False


def _arm_path(state_dir: Path) -> Path:
    return state_dir / _ARM_FILE


class WriteService:
    """Create/update/delete over the clone, with snapshot + posture + sync burst."""

    def __init__(
        self,
        cfg: Any,
        sync: Any,  # SyncService-compatible: one_shot(timeout_seconds)
        git: ShadowGit,
    ) -> None:
        self._cfg = cfg
        self._sync = sync
        self._git = git

    # -- public API ------------------------------------------------------------

    def create_note(self, path: str, text: str, agent: str = "") -> dict:
        """Create a NEW note; existing path = refuses (update is the honest tool)."""
        safe = posixpath_norm_write(path)
        if not safe:
            return {"error": "invalid path"}
        target = self._cfg.data_dir / safe
        if target.is_file():
            return {"error": "note already exists", "path": safe, "hint": "use vault.update_note"}
        return self._write(safe, text, agent, creating=True)

    def update_note(self, path: str, text: str, agent: str = "") -> dict:
        """Replace one note's content (full-text update; frontmatter included in text)."""
        safe = posixpath_norm_write(path)
        if not safe:
            return {"error": "invalid path"}
        target = self._cfg.data_dir / safe
        if not target.is_file():
            return {"error": "note does not exist", "path": safe, "hint": "use vault.create_note"}
        return self._write(safe, text, agent, creating=False)

    def upsert(self, path: str, text: str, agent: str = "") -> dict:
        """Create-or-update (the REST form): create when missing, else full-text update."""
        safe = posixpath_norm_write(path)
        if not safe:
            return {"error": "invalid path"}
        if (self._cfg.data_dir / safe).is_file():
            return self.update_note(path, text, agent=agent)
        return self.create_note(path, text, agent=agent)

    def delete_note(self, path: str, state_dir: Path, agent: str = "") -> dict:
        """Delete one note - ONLY while the windowed flag is armed + snapshot ok."""
        safe = posixpath_norm_write(path)
        if not safe:
            return {"error": "invalid path"}
        target = self._cfg.data_dir / safe
        if not target.is_file():
            return {"error": "note does not exist", "path": safe}
        if not delete_armed(state_dir):
            return {"error": "delete is not armed", "hint": "arm via the admin config route"}
        snapshot = self._git.commit(f"pre-delete snapshot of {safe} (by {agent or 'unknown'})")
        if not snapshot.get("ok"):
            return {"error": "delete requires a good snapshot", "snapshot": snapshot}
        try:
            target.unlink()
        except OSError as exc:
            return {"error": "delete failed", "detail": str(exc)[:200]}
        post = self._git.commit(f"delete {safe} ( tombstone in history; by {agent or 'unknown'})")
        synced = self._burst()
        return {"deleted": True, "path": safe, "pre_snapshot": snapshot, "post_commit": post, "sync": synced}

    def snapshots(self, path: str | None = None, limit: int = 20) -> dict:
        """The shadow-git log (optionally for one path)."""
        return {"snapshots": self._git.log(path, limit=limit)}

    def revert(self, path: str, ref: str, agent: str = "") -> dict:
        """Restore one path's content from a snapshot ref + sync per posture."""
        out = self._git.revert_path(path, ref)
        if out.get("ok"):
            out["sync"] = self._burst()
        return out

    # -- internals -------------------------------------------------------------

    def _write(self, safe: str, text: str, agent: str, *, creating: bool) -> dict:
        snapshot = self._git.commit(
            f"pre-{'create' if creating else 'update'} state (before write of {safe} by {agent or 'unknown'})",
            allow_empty=True,
        )
        target = self._cfg.data_dir / safe
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.write_text(text, encoding="utf-8")
        except OSError as exc:
            return {"error": "write failed", "detail": str(exc)[:200], "snapshot": snapshot}
        post = self._git.commit(f"{'create' if creating else 'update'} {safe} (by {agent or 'unknown'})")
        synced = self._burst()
        return {
            "written": True,
            "path": safe,
            "creating": creating,
            "pre_snapshot": snapshot,
            "post_commit": post,
            "sync": synced,
        }

    def _burst(self) -> dict:
        """One serialized ob pull/push burst - ONLY when the posture says so."""
        from server.sync_mode import current_mode

        mode = current_mode(self._cfg.state_dir)
        if mode == MODE_PULL_ONLY:
            return {
                "ran": False,
                "mode": mode,
                "detail": "pull-only: the write is staged locally; flip the posture to sync it",
            }
        result = self._sync.one_shot(self._cfg.sync_timeout)
        return {"ran": True, "mode": mode, "ok": result.ok, "detail": result.detail}


def posixpath_norm_write(path: str) -> str:
    """Vault-relative, single-segment-safe normalization for WRITES.

    Rejects empties, absolutes, escapes, and trailing-dir nonsense; keeps the
    `.md`-agnostic honesty (any filename is fine - this is a note writer, not
    an enforcer).
    """
    raw = str(path or "").strip()
    if raw.startswith("/"):
        return ""  # absolute paths are never vault-relative
    rel = raw
    if not rel or rel in (".", ".."):
        return ""
    norm = posixpath.normpath(rel)
    if norm.startswith("../") or norm in ("..", "/") or norm.startswith("/"):
        return ""
    return norm
