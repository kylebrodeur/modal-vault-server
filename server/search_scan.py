"""Live keyword search over the vault clone: no index, no cache — scan on demand.

Replaces the cancelled store.py/indexer.py pair (vault + MCP only amendment):
ranking is a simple occurrence count (title hits count double), every query
token must match (AND semantics), and directory-prefix pruning keeps the scan
proportional to the slice of the vault actually asked about.
"""

from __future__ import annotations

import os
import posixpath
from pathlib import Path
from typing import Any

import yaml

from server.linker import posixpath_norm

_PRUNED_DIRS = frozenset({".obsidian", ".trash"})
_WEIGHT_BODY = 1.0
_WEIGHT_TITLE = 2.0


def frontmatter_of(text: str) -> dict[str, Any]:
    """Leading `---` YAML block as a dict; {} when absent, invalid, or not a mapping.

    Only a delimiter as the first line makes frontmatter ("body\\n---\\ntitle: T"
    is a thematic break in the body, not metadata). Malformed YAML and
    non-mapping documents degrade to {} instead of raising — a note is never
    unreadable just because its header block is broken.
    """
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    for end, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            block = "\n".join(lines[1:end])
            break
    else:
        return {}  # unclosed block: treat the rest as body
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _walk_note_paths(vault_dir: Path, rel_base: str) -> list[str]:
    """Sorted vault-relative .md paths under rel_base, pruning .obsidian/.trash at every depth."""
    start = vault_dir / rel_base if rel_base else vault_dir
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(start, onerror=lambda _error: None):
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in _PRUNED_DIRS)
        for name in sorted(filenames):
            if not name.lower().endswith(".md"):
                continue
            rel = (Path(dirpath) / name).relative_to(vault_dir).as_posix()
            found.append(rel)
    return sorted(found)


def _is_subdir(vault_dir: Path, rel: str) -> bool:
    """True when the normalized prefix is an existing directory inside the clone."""
    return bool(rel) and (vault_dir / rel).is_dir()


def _notes_under(vault_dir: Path, prefix: str) -> list[str]:
    """Relative note paths under a caller-supplied prefix (""/subtree root); [] when unreachable."""
    rel = posixpath_norm(prefix)
    if _is_subdir(vault_dir, rel):
        return _walk_note_paths(vault_dir, rel)
    return [] if rel else _walk_note_paths(vault_dir, "")


def collect_notes(vault_dir: Path, prefix: str = "") -> list[dict]:
    """{"path", "text", "frontmatter"} for every .md, sorted by vault-relative path.

    Prunes `.obsidian/` and `.trash/` at every depth. `prefix` restricts to the
    subtree rooted there ("" = whole vault). Missing vault dir degrades to [].
    """
    if not vault_dir.is_dir():
        return []
    if not prefix:
        return _collect_from_paths(vault_dir, _walk_note_paths(vault_dir, ""))
    return _collect_from_paths(vault_dir, _notes_under(vault_dir, prefix))


def _collect_from_paths(vault_dir: Path, rel_paths: list[str]) -> list[dict]:
    """Read each note once; unreadable or undecodable notes degrade to absence."""
    notes: list[dict] = []
    for rel in rel_paths:
        try:
            text = (vault_dir / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        notes.append({"path": rel, "text": text, "frontmatter": frontmatter_of(text)})
    return sorted(notes, key=lambda note: note["path"])


def scan_vault(vault_dir: Path, query: str, k: int = 8, prefix: str = "") -> list[dict]:
    """Ranked keyword scan; every token must match; title hits weigh 2x body hits.

    Returns top-k dicts {"id", "text", "frontmatter", "score"} (rank descending,
    then vault-relative path ascending). Empty/whitespace query → []. Case
    folds via str.lower on both sides; the file stem is the "title".
    """
    tokens = query.lower().split()
    if not tokens:
        return []
    if not vault_dir.is_dir():
        return []

    rel_paths = _notes_under(vault_dir, prefix) if prefix else _walk_note_paths(vault_dir, "")
    ranked: list[tuple[float, str, dict]] = []
    for rel in rel_paths:
        try:
            text = (vault_dir / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lowered = text.lower()
        if not all(tok in lowered for tok in tokens):
            continue
        stem = posixpath.basename(rel).removesuffix(".md").removesuffix(".markdown").lower()
        score = 0.0
        for tok in tokens:
            score += _WEIGHT_BODY * lowered.count(tok)
            score += _WEIGHT_TITLE * stem.count(tok)
        ranked.append(
            (score, rel, {"id": rel, "text": text, "frontmatter": frontmatter_of(text), "score": score})
        )
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [entry[2] for entry in ranked[: max(k, 0)]]
