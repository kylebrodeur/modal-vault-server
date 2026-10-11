"""Live keyword search over the vault clone: no index, no cache — scan on demand.

Replaces the cancelled store.py/indexer.py pair (vault + MCP only amendment):
ranking is a simple occurrence count (title hits count double), every query
token must match (AND semantics), and directory-prefix pruning keeps the scan
proportional to the slice of the vault actually asked about.
"""

from __future__ import annotations

import os
import posixpath
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from server.linker import posixpath_norm

_PRUNED_DIRS = frozenset({".obsidian", ".trash"})
_DESCRIPTION_LIMIT = 280
_WEIGHT_BODY = 1.0
_WEIGHT_TITLE = 2.0


def _json_safe(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value


def _frontmatter_parts(text: str) -> tuple[str | None, str, str]:
    """Return raw leading YAML, body text, and metadata status without raising."""
    if not text.startswith("---"):
        return None, text, "missing"
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None, text, "missing"
    for end, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return "".join(lines[1:end]), "".join(lines[end + 1 :]), "present"
    return None, "", "invalid"


def frontmatter_details(text: str) -> dict[str, Any]:
    """Leading frontmatter as a JSON-safe mapping plus its honest parse state."""
    block, _body, state = _frontmatter_parts(text)
    if state == "missing":
        return {"frontmatter": {}, "frontmatter_status": "missing"}
    if state == "invalid" or block is None:
        return {"frontmatter": {}, "frontmatter_status": "invalid"}
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError:
        return {"frontmatter": {}, "frontmatter_status": "invalid"}
    if not isinstance(data, dict):
        return {"frontmatter": {}, "frontmatter_status": "non_mapping"}
    return {"frontmatter": _json_safe(data), "frontmatter_status": "valid"}


def frontmatter_of(text: str) -> dict[str, Any]:
    """Leading `---` YAML block as a JSON-safe mapping; compatibility wrapper."""
    return frontmatter_details(text)["frontmatter"]


def _compact_description(value: str) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= _DESCRIPTION_LIMIT:
        return normalized
    return f"{normalized[: _DESCRIPTION_LIMIT - 3].rstrip()}..."


def _visible_body(text: str) -> str:
    """Body following a closed frontmatter block; malformed headers have no trusted body."""
    _block, body, state = _frontmatter_parts(text)
    return body if state == "present" else text if state == "missing" else ""


def _meaningful_body_lines(body: str) -> list[str]:
    """Visible prose lines, excluding fences, comments, callouts, and headings."""
    visible: list[str] = []
    in_fence = False
    in_comment = False
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if "<!--" in line:
            in_comment = True
        if in_comment:
            if "-->" in line:
                in_comment = False
            continue
        if not line or line.startswith(">") or re.fullmatch(r"[-*_]{3,}", line):
            if visible:
                break
            continue
        if re.match(r"^#{1,6}\s+", line):
            continue
        visible.append(line)
    return visible


def note_description(text: str, frontmatter: dict[str, Any], frontmatter_status: str, path: str) -> dict[str, str]:
    """Compact agent-facing note description with transparent fallback provenance."""
    if frontmatter_status == "valid":
        for key in ("description", "summary", "abstract", "title"):
            value = frontmatter.get(key)
            if isinstance(value, str) and value.strip():
                return {"description": _compact_description(value), "description_source": f"frontmatter.{key}"}

    body = _visible_body(text)
    in_fence = False
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if not in_fence:
            match = re.match(r"^#\s+(.+?)\s*#*\s*$", line)
            if match and match.group(1).strip():
                return {"description": _compact_description(match.group(1)), "description_source": "heading"}

    prose = _meaningful_body_lines(body)
    if prose:
        return {"description": _compact_description(" ".join(prose)), "description_source": "body_excerpt"}

    name = re.sub(r"[_-]+", " ", Path(path).stem).strip() or "untitled note"
    return {"description": name, "description_source": "filename"}


def _is_protected_relative(rel: str) -> bool:
    return any(part.casefold() in _PRUNED_DIRS for part in rel.split("/"))


def _resolved_vault_path(vault_dir: Path, rel: str) -> Path | None:
    """Resolved vault-contained path, or None for traversal and symlink escapes."""
    try:
        root = vault_dir.resolve()
        candidate = (root / rel).resolve()
        candidate.relative_to(root)
    except (OSError, ValueError):
        return None
    return candidate


def safe_directory_path(vault_dir: Path, path: str) -> Path | None:
    """Existing directory under the vault, including root for the empty path."""
    if not vault_dir.is_dir():
        return None
    if not path:
        try:
            return vault_dir.resolve()
        except OSError:
            return None
    rel = posixpath_norm(path)
    if not rel or _is_protected_relative(rel):
        return None
    candidate = _resolved_vault_path(vault_dir, rel)
    return candidate if candidate is not None and candidate.is_dir() else None


def safe_note_path(vault_dir: Path, path: str) -> Path | None:
    """Existing Markdown note under the vault, never a control-dir or escaping path."""
    rel = posixpath_norm(path)
    if not rel or _is_protected_relative(rel) or not rel.casefold().endswith(".md"):
        return None
    candidate = _resolved_vault_path(vault_dir, rel)
    return candidate if candidate is not None and candidate.is_file() else None


def _walk_note_paths(vault_dir: Path, rel_base: str) -> list[str]:
    """Sorted safe Markdown paths under rel_base, pruning control dirs at every depth."""
    start = safe_directory_path(vault_dir, rel_base)
    if start is None:
        return []
    try:
        root = vault_dir.resolve()
    except OSError:
        return []
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(start, onerror=lambda _error: None, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d.casefold() not in _PRUNED_DIRS)
        for name in sorted(filenames):
            raw_path = Path(dirpath) / name
            try:
                rel = raw_path.relative_to(root).as_posix()
            except ValueError:
                continue
            if safe_note_path(vault_dir, rel) is not None:
                found.append(rel)
    return sorted(found)


def _is_subdir(vault_dir: Path, rel: str) -> bool:
    """True when the normalized prefix is an existing safe directory inside the clone."""
    return bool(rel) and safe_directory_path(vault_dir, rel) is not None


def _notes_under(vault_dir: Path, prefix: str) -> list[str]:
    """Relative note paths under a caller-supplied prefix; [] when unreachable or invalid."""
    rel = posixpath_norm(prefix)
    if _is_subdir(vault_dir, rel):
        return _walk_note_paths(vault_dir, rel)
    return []


def collect_notes(vault_dir: Path, prefix: str = "") -> list[dict]:
    """Safe, sorted Markdown note rows over the whole vault or a prefix."""
    if not vault_dir.is_dir():
        return []
    if not prefix:
        return _collect_from_paths(vault_dir, _walk_note_paths(vault_dir, ""))
    return _collect_from_paths(vault_dir, _notes_under(vault_dir, prefix))


def _collect_from_paths(vault_dir: Path, rel_paths: list[str]) -> list[dict]:
    """Read each safe note once; unreadable or undecodable notes degrade to absence."""
    notes: list[dict] = []
    try:
        root = vault_dir.resolve()
    except OSError:
        return []
    for rel in rel_paths:
        path = safe_note_path(vault_dir, rel)
        if path is None:
            continue
        try:
            text = path.read_text(encoding="utf-8")
            stat = path.stat()
        except (OSError, UnicodeDecodeError):
            continue
        details = frontmatter_details(text)
        canonical_rel = path.relative_to(root).as_posix()
        notes.append(
            {
                "path": canonical_rel,
                "text": text,
                **details,
                **note_description(text, details["frontmatter"], details["frontmatter_status"], canonical_rel),
                "bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat().replace("+00:00", "Z"),
            }
        )
    return sorted(notes, key=lambda note: note["path"])


def scan_vault(vault_dir: Path, query: str, k: int = 8, prefix: str = "") -> list[dict]:
    """Ranked keyword scan; every token must match; title hits weigh 2x body hits."""
    tokens = query.lower().split()
    if not tokens or not vault_dir.is_dir():
        return []

    rel_paths = _notes_under(vault_dir, prefix) if prefix else _walk_note_paths(vault_dir, "")
    ranked: list[tuple[float, str, dict]] = []
    for rel in rel_paths:
        path = safe_note_path(vault_dir, rel)
        if path is None:
            continue
        try:
            text = path.read_text(encoding="utf-8")
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
        ranked.append((score, rel, {"id": rel, "text": text, "frontmatter": frontmatter_of(text), "score": score}))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [entry[2] for entry in ranked[: max(k, 0)]]
