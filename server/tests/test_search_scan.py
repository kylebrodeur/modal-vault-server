"""Task 6 tests: search_scan.py — live ranked keyword scan, note collection, frontmatter parsing."""

from pathlib import Path

from server.search_scan import collect_notes, frontmatter_of, scan_vault


def _write_vault(vault: Path, notes: dict[str, str]) -> None:
    for note_path, text in notes.items():
        path = vault / note_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


SCAN_NOTES = {
    "alpha.md": "alpha here, alpha there, alpha everywhere.\n",
    "gamma.md": "alpha and gamma together.\n",
    "beta/beta.md": "a gamma mention sits here.\n",
    "alpha2.md": "alpha.\n",
    "delta.md": "unrelated delta content.\n",
}


class TestFrontmatterOf:
    """Leading --- block parses to a dict; absent/invalid degrades to {}."""

    def test_absent_frontmatter_is_empty_dict(self) -> None:
        assert frontmatter_of("# Just a body\nplain words, no block\n") == {}

    def test_non_leading_delimiter_is_not_frontmatter(self) -> None:
        assert frontmatter_of("body first\n---\ntitle: T\n") == {}

    def test_valid_block_parses(self) -> None:
        assert frontmatter_of("---\ntitle: T\ntags: [a, b]\n---\nbody here\n") == {"title": "T", "tags": ["a", "b"]}

    def test_malformed_yaml_is_empty_dict(self) -> None:
        assert frontmatter_of("---\ntitle: [unclosed\n---\nbody\n") == {}

    def test_non_mapping_yaml_is_empty_dict(self) -> None:
        assert frontmatter_of("---\n- a\n- b\n---\nbody\n") == {}

    def test_empty_block_is_empty_dict(self) -> None:
        assert frontmatter_of("---\n---\nbody\n") == {}

    def test_unclosed_block_is_empty_dict(self) -> None:
        assert frontmatter_of("---\ntags: [a]\nbody without close delimiter\n") == {}

    def test_crlf_block_parses(self) -> None:
        assert frontmatter_of("---\r\ntitle: T\r\n---\r\nbody\n") == {"title": "T"}

    def test_yaml_dates_are_json_safe_iso_strings(self) -> None:
        frontmatter = frontmatter_of(
            "---\ncreated: 2026-10-10\nupdated: 2026-10-10T12:34:56Z\nnested:\n  published: 2026-10-11\n---\nbody\n"
        )

        assert frontmatter == {
            "created": "2026-10-10",
            "updated": "2026-10-10T12:34:56+00:00",
            "nested": {"published": "2026-10-11"},
        }


class TestCollectNotes:
    """Every .md under the vault, .obsidian/ and .trash/ pruned, {"path","text","frontmatter"} rows."""

    def test_collects_all_notes_recursively_sorted(self, tmp_path: Path) -> None:
        _write_vault(tmp_path, COLLECT_PRUNE_NOTES)
        assert [n["path"] for n in collect_notes(tmp_path)] == ["nested/deep.md", "pruned-kept.md"]

    def test_prunes_obsidian_and_trash_dirs(self, tmp_path: Path) -> None:
        _write_vault(tmp_path, COLLECT_PRUNE_NOTES)
        paths = {n["path"] for n in collect_notes(tmp_path)}
        assert not any(path.startswith(".obsidian/") or path.startswith(".trash/") for path in paths)

    def test_ignores_non_markdown_files(self, tmp_path: Path) -> None:
        _write_vault(tmp_path, {"a.md": "markdown\n", "notes/plain.txt": "not markdown\n", "b.png": "binary-ish\n"})
        assert [n["path"] for n in collect_notes(tmp_path)] == ["a.md"]

    def test_note_shape_and_frontmatter(self, tmp_path: Path) -> None:
        _write_vault(tmp_path, {"a.md": "---\ntitle: A\n---\nbody line\n"})
        (note,) = collect_notes(tmp_path)
        assert set(note) == {"path", "text", "frontmatter"}
        assert note["path"] == "a.md"
        assert note["text"] == "---\ntitle: A\n---\nbody line\n"
        assert note["frontmatter"] == {"title": "A"}

    def test_prefix_restricts_to_subtree(self, tmp_path: Path) -> None:
        _write_vault(tmp_path, {"nested/deep.md": "deep\n", "root.md": "root\n"})
        assert [n["path"] for n in collect_notes(tmp_path, prefix="nested/")] == ["nested/deep.md"]

    def test_empty_prefix_returns_everything(self, tmp_path: Path) -> None:
        _write_vault(tmp_path, {"a.md": "x\n", "b/b.md": "y\n"})
        assert collect_notes(tmp_path, prefix="") == collect_notes(tmp_path)
        assert len(collect_notes(tmp_path)) == 2

    def test_escaping_prefix_collects_nothing(self, tmp_path: Path) -> None:
        # An invalid/escaping prefix must NOT fall back to the whole vault.
        _write_vault(tmp_path, {"a.md": "x\n", "b/b.md": "y\n"})
        assert collect_notes(tmp_path, prefix="../x") == []
        assert collect_notes(tmp_path, prefix="/etc") == []
        assert collect_notes(tmp_path, prefix="..") == []

    def test_empty_vault(self, tmp_path: Path) -> None:
        assert collect_notes(tmp_path) == []

    def test_missing_vault(self, tmp_path: Path) -> None:
        assert collect_notes(tmp_path / "does-not-exist") == []


COLLECT_PRUNE_NOTES = {
    "pruned-kept.md": "kept note.\n",
    "nested/deep.md": "deep kept note.\n",
    ".obsidian/plugin.md": "config-area note, pruned.\n",
    ".trash/discarded.md": "trash note, pruned.\n",
}


class TestScanVault:
    """Live ranked scan: every token must match (AND), case-insensitive, title occurrences weigh 2x body."""

    def test_multi_token_requires_every_token(self, tmp_path: Path) -> None:
        _write_vault(tmp_path, SCAN_NOTES)
        results = scan_vault(tmp_path, "alpha gamma")
        assert [r["id"] for r in results] == ["gamma.md"]
        assert results[0]["score"] == 4.0  # alpha:1 body + gamma:1 body + gamma:1 title(2x)

    def test_case_insensitive(self, tmp_path: Path) -> None:
        _write_vault(tmp_path, SCAN_NOTES)
        results = scan_vault(tmp_path, "ALPHA")
        assert [(r["id"], r["score"]) for r in results] == [
            ("alpha.md", 5.0),  # body 3x + title "alpha" 1x(2x)
            ("alpha2.md", 3.0),  # body 1x + title "alpha2" 1x(2x)
            ("gamma.md", 1.0),
        ]

    def test_title_occurrence_outranks_body_only(self, tmp_path: Path) -> None:
        _write_vault(tmp_path, {"a.md": "needle\n", "cneedle.md": "needle\n"})
        results = scan_vault(tmp_path, "needle")
        assert [(r["id"], r["score"]) for r in results] == [("cneedle.md", 3.0), ("a.md", 1.0)]

    def test_repeat_occurrences_outweigh_single(self, tmp_path: Path) -> None:
        _write_vault(tmp_path, {"one.md": "needle needle needle\n", "two.md": "needle\n"})
        results = scan_vault(tmp_path, "needle")
        assert [(r["id"], r["score"]) for r in results] == [("one.md", 3.0), ("two.md", 1.0)]

    def test_k_caps_results(self, tmp_path: Path) -> None:
        _write_vault(tmp_path, SCAN_NOTES)
        assert len(scan_vault(tmp_path, "alpha", k=2)) == 2
        assert len(scan_vault(tmp_path, "alpha")) == 3  # default k=8
        assert scan_vault(tmp_path, "alpha", k=1)[0]["id"] == "alpha.md"

    def test_prefix_prunes_candidates(self, tmp_path: Path) -> None:
        _write_vault(tmp_path, SCAN_NOTES)
        results = scan_vault(tmp_path, "gamma", prefix="beta/")
        assert [r["id"] for r in results] == ["beta/beta.md"]

    def test_escaping_prefix_matches_nothing(self, tmp_path: Path) -> None:
        # An invalid/escaping prefix must NOT fall back to the whole vault.
        _write_vault(tmp_path, SCAN_NOTES)
        assert scan_vault(tmp_path, "gamma", prefix="../x") == []
        assert scan_vault(tmp_path, "gamma", prefix="/etc") == []

    def test_no_match_returns_empty(self, tmp_path: Path) -> None:
        _write_vault(tmp_path, SCAN_NOTES)
        assert scan_vault(tmp_path, "zzz-nothing") == []

    def test_empty_query_returns_empty(self, tmp_path: Path) -> None:
        _write_vault(tmp_path, SCAN_NOTES)
        assert scan_vault(tmp_path, "") == []
        assert scan_vault(tmp_path, "   ") == []

    def test_result_shape(self, tmp_path: Path) -> None:
        _write_vault(tmp_path, {"a.md": "---\ntags: [x]\n---\nalpha body line\n"})
        (result,) = scan_vault(tmp_path, "alpha")
        assert set(result) == {"id", "text", "frontmatter", "score"}
        assert result["id"] == "a.md"
        assert result["text"] == "---\ntags: [x]\n---\nalpha body line\n"
        assert result["frontmatter"] == {"tags": ["x"]}
        assert result["score"] == 1.0  # title "a" contains no "alpha"

    def test_equal_scores_break_ties_by_path(self, tmp_path: Path) -> None:
        _write_vault(tmp_path, {"m1.md": "tie token\n", "m2.md": "tie token\n", "sub/m0.md": "tie token\n"})
        results = scan_vault(tmp_path, "tie token")
        assert [(r["id"], r["score"]) for r in results] == [
            ("m1.md", 2.0),
            ("m2.md", 2.0),
            ("sub/m0.md", 2.0),
        ]
