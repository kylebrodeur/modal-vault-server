"""Note-link graph from markdown links. Regex-based whole-file scan; no marksman in v1."""

from __future__ import annotations

import posixpath
import re
from pathlib import Path
from urllib.parse import unquote

from server.types import EdgeRow

_KIND = "note-link"

# [[wikilink]] / [[wikilink|alias]] / ![[embed]] — capture target and optional alias.
_WIKILINK_RE = re.compile(r"!{0,2}\[\[([^\[\]|]+)(?:\|[^\[\]]*)?\]\]")
# [label](target) / ![alt](target) — target may contain spaces; surrounding whitespace trimmed.
_MD_LINK_RE = re.compile(r"!{0,2}\[[^\[\]]*\]\(\s*([^()]+?)\s*\)")
# Any scheme (http(s), obsidian, mailto, …) marks an external target.
_EXTERNAL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def parse_links(text: str) -> list[str]:
    """Raw link targets as written, in document order.

    Covers ``[[wikilink]]``, ``[[wikilink|alias]]``, and ``[label](relative.md)``
    (embed forms ``![[...]]``/``![...](...)`` included). Skips ``http(s)://``,
    ``obsidian://``, any other scheme, bare anchors, and empty targets. Heading
    fragments stay in the target ("note#section" resolves to "note").
    """
    targets: list[tuple[int, str]] = []
    for match in _WIKILINK_RE.finditer(text):
        targets.append((match.start(), unquote(match.group(1).split("|", 1)[0].strip(), errors="strict")))
    for match in _MD_LINK_RE.finditer(text):
        targets.append((match.start(), unquote(match.group(1).strip(), errors="strict")))
    return [target for _, target in sorted(targets) if _is_internal(target)]


def _is_internal(target: str) -> bool:
    """False for scheme-external targets, bare anchors, and empties; True otherwise."""
    return bool(target) and not target.startswith("#") and not _EXTERNAL_RE.match(target)


def _note_names(note_paths: list[str]) -> dict[str, str]:
    """Every lookup form a note answers to, mapped to its canonical vault-relative path."""
    names: dict[str, str] = {}
    for note_path in sorted(set(note_paths)):
        norm = posixpath_norm(note_path)
        if not norm:
            continue
        names.setdefault(norm.lower(), norm)
        norm_lower = norm.lower()
        if norm_lower.endswith(".md"):
            names.setdefault(norm_lower[: -len(".md")], norm)
        parts = norm.split("/")
        stem = posixpath.basename(norm)
        if parts[-1].lower().endswith(".md"):
            stem = stem[: -len(".md")]
        names.setdefault(stem.lower(), norm)
        if stem.lower() != posixpath.basename(norm).lower():
            names.setdefault(posixpath.basename(norm).lower(), norm)
        for i in range(1, len(parts)):
            suffix_path = posixpath_norm("/".join(parts[i:]))
            if suffix_path and suffix_path.lower() != norm.lower():
                names.setdefault(suffix_path.lower(), norm)
                if suffix_path.lower().endswith(".md"):
                    names.setdefault(suffix_path.lower()[: -len(".md")], norm)
    return names


def posixpath_norm(path: str) -> str:
    """Vault-relative POSIX normalization; '' for paths that escape the vault."""
    norm = posixpath.normpath(path.replace("\\", "/"))
    if norm in ("", ".") or norm.startswith("..") or posixpath.isabs(norm):
        return ""
    return norm


def resolve_link(target: str, names: dict[str, str]) -> str:
    """One raw target to a canonical vault-relative note path, or "" (still an edge: dangling).

    Case-insensitive matching on full path, suffix path, or file stem; a leading
    '/' anchors to the vault root.
    """
    link = target.strip().removeprefix("/").lower()
    if not link or link.startswith("#"):
        return ""
    link = link.removesuffix(".md").removesuffix(".markdown")
    if not link:
        return ""
    stem = link.split("#", 1)[0]
    if not stem:
        return ""
    before, sep, after = stem.rpartition(":")
    if sep and after.isdigit():
        stem = before
    return names.get(stem, "")


def _scan_links(text: str) -> list[tuple[str, str]]:
    """(whole-link text as written, target) pairs in document order."""
    pairs: list[tuple[int, str, str]] = []
    for match in _WIKILINK_RE.finditer(text):
        pairs.append((match.start(), match.group(0), match.group(1).split("|", 1)[0].strip()))
    for match in _MD_LINK_RE.finditer(text):
        pairs.append((match.start(), match.group(0), unquote(match.group(1).strip(), errors="strict")))
    pairs.sort(key=lambda entry: entry[0])
    return [(whole, target) for _, whole, target in pairs if _is_internal(target)]


def build_edge_set(vault_dir: Path, note_paths: list[str]) -> list[EdgeRow]:
    """Parse every note once and resolve its links into EdgeRow tuples.

    Edges are emitted even when unresolvable (dst="") so dangling links stay
    honest. Self-links (src == dst) are skipped. `note_paths` may be unsorted;
    edges come back in sorted-by-src scan order, one edge per link in document order.
    """
    names = _note_names(note_paths)
    edges: list[EdgeRow] = []
    for src in sorted(set(note_paths)):
        note_path = vault_dir / Path(src)
        if not note_path.is_file():
            continue
        try:
            text = note_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # Single pass: whole-link text as written, target extracted for resolution.
        for whole, raw_target in _scan_links(text):
            dst = resolve_link(raw_target, names)
            if not dst and "/" in raw_target and not raw_target.startswith("/"):
                # Markdown relative links resolve against the src note's directory first.
                src_dir = posixpath.dirname(src.replace("\\", "/"))
                joined = posixpath.normpath(posixpath.join(src_dir, raw_target.replace("\\", "/")))
                lowered = joined.lower()
                if lowered.endswith(".md"):
                    lowered = lowered[: -len(".md")]
                dst = names.get(lowered, "")
            if dst == src:
                continue
            edges.append(EdgeRow(src=src, dst=dst, kind=_KIND, raw=whole))
    return edges
