"""Shared dataclasses for the vault server. Contract lives in the plan; do not rename anything."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EdgeRow:
    """One note-link graph edge."""

    src: str  # vault-relative path
    dst: str  # vault-relative path (resolved)
    kind: str  # "note-link" in slice 1
    raw: str  # raw link text as written


@dataclass
class IndexResult:
    """Outcome of one link-graph pass."""

    notes_scanned: int = 0
    edges_upserted: int = 0
    skipped_reason: str | None = None


@dataclass
class SyncResult:
    """Outcome of one `ob` sync invocation."""

    ok: bool
    mode: str  # "pull-only"
    detail: str  # "" on success, stderr excerpt on failure


LAST_SYNC_FILE = "last_sync.json"  # the watermark's single shared name (mcp_tools reads it, web writes it)
