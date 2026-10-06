"""Vault tool surface for MCP: the five read-only tools and their wire contract.

Contract lives in the amended plan and the Task 6 brief; every reply is a JSON
dict (errors degrade to ``{"error": ...}``, never raise), graph work happens
live over ``linker.build_edge_set`` with no persistent index, and the semantic
door stays honest until modal-embedding-server is actually connected.
"""

from __future__ import annotations

import json
import posixpath
from typing import Any, ClassVar

from server.config import Config
from server.linker import build_edge_set, posixpath_norm, resolve_link
from server.types import EdgeRow

_LAST_SYNC_FILE = "last_sync.json"
_DEFAULT_K = 8
_UNBOUNDED_K = 1_000_000  # graph seeds need the full ranking, k slices it later
_GRAPH_SEED_COUNT = 3
_GRAPH_DEPTH = 1
_KIND = "note-link"
_SEMANTIC_DOOR: dict[str, Any] = {"configured": False, "note": "connect modal-embedding-server later"}


def _edge_to_dict(edge: EdgeRow) -> dict[str, str]:
    """EdgeRow → wire dict, field-for-field the plan's EdgeRow contract."""
    return {"src": edge.src, "dst": edge.dst, "kind": edge.kind, "raw": edge.raw}


def edge_neighborhood(edges: list[EdgeRow], seeds: list[str], depth: int = 1) -> list[EdgeRow]:
    """Edges touching any seed within `depth` hops (note-link only, slice 1).

    Breadth-first: each hop grows the frontier through every endpoint of the
    edges touched so far. Output is deterministic (sorted by src, dst, raw).
    depth < 1 → no walk, empty reply.
    """
    if not seeds or depth < 1:
        return []
    frontier = {seed for seed in seeds if seed}
    if not frontier:
        return []
    touched: list[EdgeRow] = []
    seen: set[tuple[str, str, str, str]] = set()
    for _hop in range(depth):
        grew = False
        for edge in edges:
            if edge.kind != _KIND:
                continue
            if edge.src in frontier or edge.dst in frontier:
                key = (edge.src, edge.dst, edge.kind, edge.raw)
                if key not in seen:
                    seen.add(key)
                    touched.append(edge)
                    grew = True
        if not grew:
            break
        frontier = frontier | {edge.dst for edge in touched if edge.dst} | {edge.src for edge in touched if edge.src}
    return sorted(touched, key=lambda e: (e.src, e.dst, e.raw))


class VaultTools:
    """The read-only vault tool family behind the MCP server (Task 7 mounts these)."""

    TOOLS: ClassVar[list[dict]] = [
        {
            "name": "vault.search",
            "description": (
                "Keyword search over the vault clone. mode 'text' ranks notes live "
                "(every query token must match; title hits weigh double). mode "
                "'graph' seeds a depth-1 link-graph walk from the top-3 text "
                "matches and also returns the walked edges."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keywords, whitespace-separated (AND semantics)"},
                    "mode": {"type": "string", "enum": ["text", "graph"], "default": "text"},
                    "k": {"type": "integer", "default": 8, "minimum": 1},
                    "prefix": {"type": "string", "description": "Restrict to a vault-relative directory"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "vault.read",
            "description": (
                "Read one note's full text and frontmatter. Missing notes return "
                "exists:false, not an error."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Vault-relative note path"}},
                "required": ["path"],
            },
        },
        {
            "name": "vault.list",
            "description": (
                "List notes (path + frontmatter), optionally filtered by directory "
                "prefix and frontmatter tag."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "Restrict to a vault-relative directory"},
                    "tag": {"type": "string", "description": "Only notes whose frontmatter tags contain this"},
                },
            },
        },
        {
            "name": "vault.query_graph",
            "description": (
                "Link-graph neighborhood around one seed note within `depth` hops "
                "(kind 'note-link' only in slice 1)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "seed": {"type": "string", "description": "Seed note path or name (Obsidian-style lookup)"},
                    "depth": {"type": "integer", "default": 1, "minimum": 1},
                    "kind": {"type": "string", "default": "note-link", "enum": ["note-link"]},
                },
                "required": ["seed"],
            },
        },
        {
            "name": "vault.status",
            "description": (
                "Sync watermark, live note count, and the semantic-search door "
                "(honest about what is wired)."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]

    def __init__(self, cfg: Config, search: Any, sync: Any) -> None:
        self._cfg = cfg
        self._search = search
        self._sync = sync  # SyncService: accepted per plan; slice 1 status reads the watermark file

    async def call(self, name: str, arguments: dict | None = None) -> dict:
        """Dispatch by tool name; every path returns a dict (errors degrade honestly)."""
        arguments = arguments if arguments is not None else {}
        if name == "vault.search":
            return await self._search_tool(arguments)
        if name == "vault.read":
            return await self._read_tool(arguments)
        if name == "vault.list":
            return await self._list_tool(arguments)
        if name == "vault.query_graph":
            return await self._query_graph_tool(arguments)
        if name == "vault.status":
            return await self._status_tool(arguments)
        return {"error": f"unknown tool: {name}"}

    # -- vault.search ---------------------------------------------------------

    async def _search_tool(self, arguments: dict) -> dict:
        query = str(arguments.get("query", ""))
        requested_mode = arguments.get("mode", "text")
        effective_mode = requested_mode if requested_mode in ("text", "graph") else "text"
        k_raw = arguments.get("k", _DEFAULT_K)
        try:
            k = int(k_raw) if k_raw is not None else _DEFAULT_K
        except (TypeError, ValueError):
            k = _DEFAULT_K
        prefix = str(arguments.get("prefix", ""))

        # Graph mode needs the FULL ranking for "top-3 seeds" (k stays a display
        # window), so scan once unbounded and slice here. Text mode scans with
        # k directly.
        if requested_mode == "graph" and effective_mode == "graph":
            all_ranked = self._search.scan_vault(self._cfg.data_dir, query, k=_UNBOUNDED_K, prefix=prefix)
            results = all_ranked[: max(k, 0)]
        else:
            all_ranked = results = self._search.scan_vault(self._cfg.data_dir, query, k=max(k, 0), prefix=prefix)

        note: str | None = None
        if requested_mode not in ("text", "graph"):
            # Honest degrade: unknown modes fall back to a plain text scan.
            note = f"mode {requested_mode!r} unsupported in slice 1; ran text search instead"
        elif not query.strip():
            note = "empty query: nothing to match"

        reply: dict[str, Any] = {"results": results, "mode": effective_mode, "note": note}
        if effective_mode == "graph":
            # Seeds are the top-3 of the full ranking, independent of k.
            seeds = [entry["id"] for entry in all_ranked[:_GRAPH_SEED_COUNT]]
            if seeds:
                edges = edge_neighborhood(self._build_edges(), seeds, depth=_GRAPH_DEPTH)
            else:
                edges = []
                note = note or "graph mode: no text matches to seed the walk"
            reply["edges"] = [_edge_to_dict(edge) for edge in edges]
            reply["seeds"] = seeds
            reply["note"] = note
        return reply

    def _build_edges(self) -> list[EdgeRow]:
        """Live edge set over the whole clone (linker is the single graph source)."""
        paths = [note_row["path"] for note_row in self._search.collect_notes(self._cfg.data_dir)]
        return build_edge_set(self._cfg.data_dir, paths)

    # -- vault.read -----------------------------------------------------------

    async def _read_tool(self, arguments: dict) -> dict:
        """Full text + frontmatter for one note; missing/escaping paths: exists:false."""
        rel = str(arguments.get("path", ""))
        safe_rel = posixpath_norm(rel)
        row = {"path": rel, "text": "", "frontmatter": {}, "exists": False}
        if not safe_rel:
            return row
        path = self._cfg.data_dir / safe_rel
        if not path.is_file():
            return row
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return row
        return {"path": safe_rel, "text": text, "frontmatter": self._search.frontmatter_of(text), "exists": True}

    # -- vault.list -----------------------------------------------------------

    async def _list_tool(self, arguments: dict) -> dict:
        """Path+frontmatter rows over the whole vault or a prefix, optionally tag-filtered."""
        prefix = str(arguments.get("prefix", ""))
        tag = arguments.get("tag")
        tag_want = str(tag).casefold() if tag is not None else None
        rows = [
            {"path": note_row["path"], "frontmatter": note_row["frontmatter"]}
            for note_row in self._search.collect_notes(self._cfg.data_dir, prefix=prefix)
            if tag_want is None or tag_want in _tags_of(note_row["frontmatter"])
        ]
        return {"notes": rows, "count": len(rows)}

    # -- vault.query_graph ----------------------------------------------------

    async def _query_graph_tool(self, arguments: dict) -> dict:
        """Edge neighborhood around one seed; unknown kind is an explicit error reply."""
        seed = str(arguments.get("seed", ""))
        kind = str(arguments.get("kind", _KIND))
        if kind != _KIND:
            return {"error": "unknown graph kind"}
        depth_raw = arguments.get("depth", 1)
        try:
            depth = max(int(depth_raw), 1)
        except (TypeError, ValueError):
            depth = 1

        note_rows = self._search.collect_notes(self._cfg.data_dir)
        paths = [note_row["path"] for note_row in note_rows]
        names = _note_names(paths)
        edges_all = self._build_edges()
        seeds = _resolve_seed(seed, paths, names)
        if not seeds:
            return {"edges": [], "seeds": []}
        edges = edge_neighborhood(edges_all, seeds, depth=depth)
        return {"edges": [_edge_to_dict(edge) for edge in edges], "seeds": seeds}

    # -- vault.status ---------------------------------------------------------

    async def _status_tool(self, arguments: dict) -> dict:
        """Sync watermark + live note count + the semantic door (honest slice-1 state)."""
        note_count = len(self._search.collect_notes(self._cfg.data_dir))
        return {
            "sync": self._sync_state(),
            "notes": note_count,
            "semantic": dict(_SEMANTIC_DOOR),
        }

    def _sync_state(self) -> dict[str, Any]:
        """last_sync.json in state_dir; missing/corrupt degrades to honest defaults.

        Shape: {"mode", "last_sync_at", "ok"}. The watermark carries "ok"; until
        the boot flow (Task 7) writes it, the file is absent → ok:false,
        last_sync_at:None.
        """
        record: dict[str, Any] | None = None
        try:
            raw = (self._cfg.state_dir / _LAST_SYNC_FILE).read_text(encoding="utf-8")
            parsed = json.loads(raw) if raw.strip() else None
            if isinstance(parsed, dict):
                record = parsed
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            record = None
        last_sync_at = record.get("last_sync_at") if record else None
        ok = record.get("ok") if record else None
        return {
            "mode": self._cfg.sync_mode,
            "last_sync_at": last_sync_at if isinstance(last_sync_at, str) else None,
            "ok": bool(ok) if isinstance(ok, bool) else False,
        }


def _tags_of(frontmatter: dict[str, Any]) -> list[str]:
    """frontmatter `tags` as a flat casefolded string list.

    Accepts a YAML sequence, a single scalar, and Obsidian's inline comma form
    (`tags: ideas, planning` parses as one string; split on commas). Nested
    lists flatten one level; non-string scalars stringify.
    """
    raw = frontmatter.get("tags")
    if raw is None:
        return []
    if isinstance(raw, str):
        parts: list[Any] = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        parts = []
        for entry in raw:
            parts.extend(entry.split(",") if isinstance(entry, str) else [entry])
    else:
        parts = [raw]
    return [str(part).strip().casefold() for part in parts if str(part).strip()]


def _note_names(paths: list[str]) -> dict[str, str]:
    """Lookup map for seeds: canonical path (± .md), full stem, lowercased variants.

    Reuses linker's resolution semantics by building the same names-shape map
    linker itself uses, scoped to what a seed can answer to.
    """
    names: dict[str, str] = {}
    for path in sorted(set(paths)):
        norm = posixpath_norm(path)
        if not norm:
            continue
        lowered = norm.lower()
        names.setdefault(lowered, norm)
        if lowered.endswith(".md"):
            names.setdefault(lowered[: -len(".md")], norm)
        stem = posixpath.basename(norm)
        if stem.lower().endswith(".md"):
            stem = stem[: -len(".md")]
        names.setdefault(stem.lower(), norm)
    return names


def _resolve_seed(seed: str, paths: list[str], names: dict[str, str]) -> list[str]:
    """Seed text to canonical note path(s); [] when nothing answers.

    Exact path/stem matches win (all matches when a stem is ambiguous); a bare
    unmatched seed falls back to linker's resolve_link, which may still land a
    suffix-path match. Empty/escaping seeds → [].
    """
    lowered = seed.strip().removeprefix("/").casefold()
    if not lowered:
        return []
    direct: list[str] = []
    seen: set[str] = set()
    for path in paths:
        norm = posixpath_norm(path)
        if not norm:
            continue
        for candidate in (norm.lower(), posixpath.basename(norm).lower()):
            if candidate == lowered and path not in seen:
                seen.add(path)
                direct.append(path)
    if direct:
        return sorted(set(direct))
    fallback = resolve_link(seed, names)
    return [fallback] if fallback else []
