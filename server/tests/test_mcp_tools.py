"""Task 6 tests: VaultTools wire contract — vault.search/read/list/query_graph/status."""

from pathlib import Path
from typing import Any

import pytest

from server import search_scan
from server.config import Config
from server.mcp_tools import VaultTools
from server.sync_service import SyncService

MCP_NOTES = {
    "Ideas.md": "---\ntags: ideas, planning\n---\nIdea body mentions alpha and links [[Projects/Roadmap]].\n",
    "Projects/Roadmap.md": "---\ntitle: Roadmap\ntags: [planning, active]\nstatus: active\n---\n"
    "Roadmap body links [[Ideas]] and [up](../Ideas.md).\n",
    "Inbox.md": "plain inbox note about alpha.\n",
    "Archive/Old.md": "[[Projects/Roadmap]]\n",
    ".trash/discarded.md": "discarded alpha note.\n",
    ".obsidian/workspace.json": "{}\n",
}


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    for rel, text in MCP_NOTES.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def _tools(vault_dir: Path) -> VaultTools:
    cfg = Config(api_token="t", data_dir=vault_dir, state_dir=vault_dir / "state")
    sync = SyncService(workspace_dir=vault_dir, state_dir=vault_dir / "state")
    return VaultTools(cfg=cfg, search=search_scan, sync=sync)


class TestToolsRegistry:
    """TOOLS carries all five tools with name/description/inputSchema."""

    def test_all_five_tools_with_required_contract(self) -> None:
        names = {tool["name"] for tool in VaultTools.TOOLS}
        assert names == {"vault.search", "vault.read", "vault.list", "vault.query_graph", "vault.status"}
        for tool in VaultTools.TOOLS:
            assert set(tool) == {"name", "description", "inputSchema"}
            assert isinstance(tool["description"], str) and tool["description"]
            assert tool["inputSchema"]["type"] == "object"

    def test_required_fields_per_tool(self) -> None:
        schemas = {tool["name"]: tool["inputSchema"] for tool in VaultTools.TOOLS}
        assert schemas["vault.search"]["required"] == ["query"]
        assert schemas["vault.search"]["properties"]["mode"]["enum"] == ["text", "graph"]
        assert schemas["vault.read"]["required"] == ["path"]
        assert schemas["vault.query_graph"]["required"] == ["seed"]
        assert "required" not in schemas["vault.list"]
        assert schemas["vault.status"]["properties"] == {}


class TestSearchTool:
    """vault.search: text mode = live scan; graph mode = top-3 seeds + depth-1 edge walk."""

    async def test_text_mode_ranked_shape_and_prune(self, vault: Path) -> None:
        out = await _tools(vault).call("vault.search", {"query": "alpha"})
        assert out["mode"] == "text"
        assert out["note"] is None
        assert [(r["id"], r["score"]) for r in out["results"]] == [("Ideas.md", 1.0), ("Inbox.md", 1.0)]
        for result in out["results"]:
            assert set(result) == {"id", "text", "frontmatter", "score"}

    async def test_graph_mode_walks_from_top3_seeds(self, vault: Path) -> None:
        out = await _tools(vault).call("vault.search", {"query": "alpha", "mode": "graph"})
        assert out["mode"] == "graph"
        assert out["seeds"] == ["Ideas.md", "Inbox.md"]
        assert out["results"] == (await _tools(vault).call("vault.search", {"query": "alpha"}))["results"]
        edges = {(e["src"], e["dst"]) for e in out["edges"]}
        assert edges == {("Ideas.md", "Projects/Roadmap.md"), ("Projects/Roadmap.md", "Ideas.md")}
        for edge in out["edges"]:
            assert set(edge) == {"src", "dst", "kind", "raw"}
            assert edge["kind"] == "note-link"

    async def test_graph_mode_edges_restricted_to_seed_neighborhood(self, vault: Path) -> None:
        # Archive/Old.md links to Projects/Roadmap.md but matches no seed: its edge must stay out.
        out = await _tools(vault).call("vault.search", {"query": "alpha", "mode": "graph"})
        assert not any(edge["src"] == "Archive/Old.md" for edge in out["edges"])

    async def test_graph_mode_no_text_match_degrades_honestly(self, vault: Path) -> None:
        out = await _tools(vault).call("vault.search", {"query": "zzz-absent", "mode": "graph"})
        assert out == {"results": [], "mode": "graph", "note": out["note"], "edges": [], "seeds": []}
        assert out["note"]

    async def test_unknown_mode_degrades_to_text_with_note(self, vault: Path) -> None:
        out = await _tools(vault).call("vault.search", {"query": "alpha", "mode": "vector"})
        assert out["mode"] == "text"
        assert out["note"] is not None and "vector" in out["note"]
        assert len(out["results"]) == 2

    async def test_empty_query_reports_empty_results(self, vault: Path) -> None:
        out = await _tools(vault).call("vault.search", {"query": "   "})
        assert out == {"results": [], "mode": "text", "note": out["note"]}
        assert out["note"]

    async def test_k_caps_results_even_in_graph_mode(self, vault: Path) -> None:
        out = await _tools(vault).call("vault.search", {"query": "alpha", "mode": "graph", "k": 1})
        assert [r["id"] for r in out["results"]] == ["Ideas.md"]
        assert out["seeds"] == ["Ideas.md", "Inbox.md"]  # seeds stay top-3 regardless of k


class TestReadTool:
    """vault.read: full text + frontmatter; missing note is exists:false, never an error."""

    async def test_reads_existing_note_with_frontmatter(self, vault: Path) -> None:
        out = await _tools(vault).call("vault.read", {"path": "Ideas.md"})
        assert out == {
            "path": "Ideas.md",
            "text": MCP_NOTES["Ideas.md"],
            "frontmatter": {"tags": "ideas, planning"},
            "exists": True,
        }

    async def test_missing_note_is_exists_false(self, vault: Path) -> None:
        out = await _tools(vault).call("vault.read", {"path": "Nope.md"})
        assert out == {"path": "Nope.md", "text": "", "frontmatter": {}, "exists": False}

    async def test_vault_escaping_path_is_exists_false(self, vault: Path) -> None:
        out = await _tools(vault).call("vault.read", {"path": "../outside.md"})
        assert out["exists"] is False
        assert out["text"] == ""


class TestListTool:
    """vault.list: path + frontmatter rows, optional prefix and frontmatter-tags filter."""

    async def test_lists_all_notes_with_frontmatter_only(self, vault: Path) -> None:
        out = await _tools(vault).call("vault.list", {})
        assert out["count"] == 4
        assert [n["path"] for n in out["notes"]] == [
            "Archive/Old.md",
            "Ideas.md",
            "Inbox.md",
            "Projects/Roadmap.md",
        ]
        for note in out["notes"]:
            assert set(note) == {"path", "frontmatter"}

    async def test_tag_filter_matches_list_and_comma_string_tags(self, vault: Path) -> None:
        out = await _tools(vault).call("vault.list", {"tag": "planning"})
        assert out["count"] == 2
        assert {n["path"] for n in out["notes"]} == {"Ideas.md", "Projects/Roadmap.md"}

    async def test_tag_without_match_counts_zero(self, vault: Path) -> None:
        out = await _tools(vault).call("vault.list", {"tag": "nope"})
        assert out == {"notes": [], "count": 0}

    async def test_prefix_filter(self, vault: Path) -> None:
        out = await _tools(vault).call("vault.list", {"prefix": "Projects/"})
        assert [n["path"] for n in out["notes"]] == ["Projects/Roadmap.md"]


class TestQueryGraphTool:
    """vault.query_graph: seed-resolved edge neighborhood; unknown kind is an error reply."""

    async def test_depth1_neighborhood_in_both_directions(self, vault: Path) -> None:
        out = await _tools(vault).call("vault.query_graph", {"seed": "Ideas"})
        assert out["seeds"] == ["Ideas.md"]
        assert sorted(edge["raw"] for edge in out["edges"]) == [
            "[[Ideas]]",
            "[[Projects/Roadmap]]",
            "[up](../Ideas.md)",
        ]
        for edge in out["edges"]:
            assert "Ideas.md" in (edge["src"], edge["dst"])

    async def test_seed_canonicalizes_from_stem_and_full_path(self, vault: Path) -> None:
        out = await _tools(vault).call("vault.query_graph", {"seed": "Projects/Roadmap.md"})
        assert out["seeds"] == ["Projects/Roadmap.md"]
        assert out["edges"]

    async def test_unknown_seed_yields_empty_neighborhood(self, vault: Path) -> None:
        assert await _tools(vault).call("vault.query_graph", {"seed": "ghost"}) == {"edges": [], "seeds": []}

    async def test_unknown_kind_is_error_reply(self, vault: Path) -> None:
        out = await _tools(vault).call("vault.query_graph", {"seed": "Ideas", "kind": "vector"})
        assert out == {"error": "unknown graph kind"}

    async def test_walker_honors_requested_depth(self, tmp_path: Path) -> None:
        chain = tmp_path / "chain"
        chain.mkdir()
        for rel, text in {"a.md": "[[b]]\n", "b.md": "[[c]]\n", "c.md": "end\n"}.items():
            (chain / rel).write_text(text, encoding="utf-8")
        tools = _tools(chain)
        out1 = await tools.call("vault.query_graph", {"seed": "a", "depth": 1})
        assert sorted((e["src"], e["dst"]) for e in out1["edges"]) == [("a.md", "b.md")]
        out2 = await tools.call("vault.query_graph", {"seed": "a", "depth": 2})
        assert sorted((e["src"], e["dst"]) for e in out2["edges"]) == [("a.md", "b.md"), ("b.md", "c.md")]


class TestStatusTool:
    """vault.status: sync watermark state, live note count, and the semantic door."""

    async def test_before_first_sync_reports_honest_defaults(self, vault: Path) -> None:
        out: dict[str, Any] = await _tools(vault).call("vault.status", {})
        assert out == {
            "sync": {"mode": "pull-only", "last_sync_at": None, "ok": False},
            "notes": 4,
            "semantic": {"configured": False, "note": "connect modal-embedding-server later"},
        }

    async def test_reads_last_sync_watermark(self, vault: Path) -> None:
        state = vault / "state"
        state.mkdir()
        (state / "last_sync.json").write_text(
            '{"last_sync_at": "2026-10-06T00:00:00Z", "ok": true}', encoding="utf-8"
        )
        out = await _tools(vault).call("vault.status", {})
        assert out["sync"] == {"mode": "pull-only", "last_sync_at": "2026-10-06T00:00:00Z", "ok": True}

    async def test_corrupt_watermark_degrades_without_raising(self, vault: Path) -> None:
        state = vault / "state"
        state.mkdir()
        (state / "last_sync.json").write_text("not json", encoding="utf-8")
        out = await _tools(vault).call("vault.status", {})
        assert out["sync"] == {"mode": "pull-only", "last_sync_at": None, "ok": False}


class TestDispatch:
    """call() dispatches by name; unknown names are honest error replies, never raises."""

    async def test_unknown_tool_name(self, vault: Path) -> None:
        out = await _tools(vault).call("vault.nope", {})
        assert out == {"error": "unknown tool: vault.nope"}

    async def test_none_arguments_tolerated(self, vault: Path) -> None:
        out = await _tools(vault).call("vault.status", None)
        assert out["notes"] == 4
