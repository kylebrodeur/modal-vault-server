from pathlib import Path

from server.linker import build_edge_set, parse_links
from server.types import EdgeRow

FIXTURE_NOTES = {
    "a.md": (
        "- [[B]]\n"
        "- [[B|the alias]]\n"
        "- [[Projects/Idea]]\n"
        "- [[IDEA]]\n"
        "- [md link](projects/idea.md)\n"
        "- [ext](https://example.com/x)\n"
        "- [proto](obsidian://open)\n"
        "- [self-anchor](#top)\n"
        "- [[#top]]\n"
        "- [[ghost]]\n"
        "- [back](b.md)\n"
        "- [[a]]\n"
    ),
    "b.md": "Plain note, no links.\n",
    "projects/idea.md": "- [[idea]]\n- [[nested]]\n",
    "projects/deep/nested.md": "- [up](../idea.md)\n- [[idea]]\n- [missing](../nope.md)\n- [[ghost]]\n",
}


def _write_vault(vault: Path, notes: dict[str, str]) -> None:
    for note_path, text in notes.items():
        path = vault / note_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class TestParseLinks:
    """Extraction stage: raw targets as written, in document order."""

    def test_document_order_mixed_forms(self) -> None:
        text = "[[B]] then [[B|the alias]] then [label](c.md) then ![[idea]] then [[ghost]]"
        assert parse_links(text) == ["B", "B", "c.md", "idea", "ghost"]

    def test_skips_external_schemes_bare_anchors_and_empties(self) -> None:
        text = (
            "[ext](https://example.com/x) [proto](obsidian://open?vault=v) "
            "[mail](mailto:a@b.example) [anchor](#top) [[#top]] [[|alias]] [[]] []()"
        )
        assert parse_links(text) == []

    def test_heading_fragments_stay_in_the_raw_target(self) -> None:
        assert parse_links("[[note#section]] and [x](note.md#section)") == [
            "note#section",
            "note.md#section",
        ]

    def test_target_whitespace_is_trimmed(self) -> None:
        assert parse_links("[[ spaced name ]] and [label]( c.md )") == ["spaced name", "c.md"]

    def test_no_links(self) -> None:
        assert parse_links("") == []
        assert parse_links("plain words, no links at all") == []

    def test_fixture_a_md_targets_in_document_order(self) -> None:
        assert parse_links(FIXTURE_NOTES["a.md"]) == [
            "B",
            "B",
            "Projects/Idea",
            "IDEA",
            "projects/idea.md",
            "ghost",
            "b.md",
            "a",
        ]


class TestBuildEdgeSet:
    """Resolution stage: exact EdgeRow tuples over a fixture vault on tmp_path."""

    def test_exact_edges_on_fixture_vault(self, tmp_path: Path) -> None:
        _write_vault(tmp_path, FIXTURE_NOTES)
        # note_paths deliberately unsorted: build_edge_set normalizes to sorted-note scan order.
        edges = build_edge_set(tmp_path, ["a.md", "b.md", "projects/idea.md", "projects/deep/nested.md"])
        assert [(edge.src, edge.dst, edge.kind, edge.raw) for edge in edges] == [
            ("a.md", "b.md", "note-link", "[[B]]"),
            ("a.md", "b.md", "note-link", "[[B|the alias]]"),
            ("a.md", "projects/idea.md", "note-link", "[[Projects/Idea]]"),
            ("a.md", "projects/idea.md", "note-link", "[[IDEA]]"),
            ("a.md", "projects/idea.md", "note-link", "[md link](projects/idea.md)"),
            ("a.md", "", "note-link", "[[ghost]]"),
            ("a.md", "b.md", "note-link", "[back](b.md)"),
            ("projects/deep/nested.md", "projects/idea.md", "note-link", "[up](../idea.md)"),
            ("projects/deep/nested.md", "projects/idea.md", "note-link", "[[idea]]"),
            ("projects/deep/nested.md", "", "note-link", "[missing](../nope.md)"),
            ("projects/deep/nested.md", "", "note-link", "[[ghost]]"),
            ("projects/idea.md", "projects/deep/nested.md", "note-link", "[[nested]]"),
        ]

    def test_self_links_produce_no_edges(self, tmp_path: Path) -> None:
        _write_vault(tmp_path, {"a.md": "self [[a]] ref [me](a.md) alias [[a|label]] case [[A]]\n"})
        assert build_edge_set(tmp_path, ["a.md"]) == []

    def test_missing_note_files_are_skipped_without_crashing(self, tmp_path: Path) -> None:
        _write_vault(tmp_path, {"a.md": "[[B]]\n", "b.md": "plain\n"})
        edges = build_edge_set(tmp_path, ["a.md", "missing.md", "b.md"])
        assert edges == [EdgeRow(src="a.md", dst="b.md", kind="note-link", raw="[[B]]")]

    def test_empty_note_paths(self, tmp_path: Path) -> None:
        assert build_edge_set(tmp_path, []) == []
