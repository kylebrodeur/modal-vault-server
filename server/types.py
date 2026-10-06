"""Shared dataclasses for the vault server. Contract lives in the plan's Shared Interfaces; do not rename anything."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NoteRow:
    """One LanceDB row: a whole embedded note."""

    id: str  # vault-relative path, e.g. "projects/idea.md"
    vector: list[float]
    text: str  # full raw note text (whole note)
    frontmatter: dict  # parsed YAML frontmatter ({} when absent)
    sha256: str
    embedded_at: str  # ISO-8601 UTC


@dataclass(frozen=True)
class EdgeRow:
    """One note-link graph edge."""

    src: str  # vault-relative path
    dst: str  # vault-relative path (resolved)
    kind: str  # "note-link" in slice 1
    raw: str  # raw link text as written


@dataclass
class IndexResult:
    """Outcome of one index pass."""

    notes_embedded: int = 0
    notes_removed: int = 0
    edges_upserted: int = 0
    skipped_reason: str | None = None


@dataclass
class SyncResult:
    """Outcome of one `ob` sync invocation."""

    ok: bool
    mode: str  # "pull-only"
    detail: str  # "" on success, stderr excerpt on failure
