"""Tests for relay-list and exclude-list file readers in __main__.

These exercise the small file-parsing surface of __main__ without
touching orchestration or argparse:
  - read_relay_list: list of files -> ordered, deduplicated relay names
  - read_exclude_list: file of "src -> dst" pairs -> set of tuples
"""
from __future__ import annotations

import pytest

from chatmail_prober.__main__ import read_exclude_list, read_relay_list


class TestReadRelayList:
    def test_reads_domains(self, tmp_path):
        f = tmp_path / "relays.txt"
        f.write_text("a.example\nb.example\nc.example\n")
        assert read_relay_list([str(f)]) == ["a.example", "b.example", "c.example"]

    def test_ignores_comments_and_blanks(self, tmp_path):
        f = tmp_path / "relays.txt"
        f.write_text("# comment\na.example\n\n  \n# another\nb.example\n")
        assert read_relay_list([str(f)]) == ["a.example", "b.example"]

    def test_strips_whitespace(self, tmp_path):
        f = tmp_path / "relays.txt"
        f.write_text("  a.example  \n  b.example\t\n")
        assert read_relay_list([str(f)]) == ["a.example", "b.example"]

    def test_empty_file_exits(self, tmp_path):
        f = tmp_path / "relays.txt"
        f.write_text("# only comments\n\n")
        with pytest.raises(SystemExit):
            read_relay_list([str(f)])

    def test_single_relay(self, tmp_path):
        f = tmp_path / "relays.txt"
        f.write_text("a.example\n")
        assert read_relay_list([str(f)]) == ["a.example"]

    def test_multiple_files_merged_and_deduplicated(self, tmp_path):
        f1 = tmp_path / "a.txt"
        f1.write_text("a.example\nb.example\n")
        f2 = tmp_path / "b.txt"
        f2.write_text("b.example\nc.example\n")
        assert read_relay_list([str(f1), str(f2)]) == [
            "a.example", "b.example", "c.example"
        ]


class TestReadExcludeList:
    def test_parses_pairs(self, tmp_path):
        f = tmp_path / "exclude.txt"
        f.write_text("a.example -> b.example\nc.example->d.example\n")
        result = read_exclude_list(str(f))
        assert result == {("a.example", "b.example"), ("c.example", "d.example")}

    def test_ignores_comments_and_blanks(self, tmp_path):
        f = tmp_path / "exclude.txt"
        f.write_text("# a comment\n\na.example -> b.example\n  \n")
        result = read_exclude_list(str(f))
        assert result == {("a.example", "b.example")}

    def test_skips_malformed_lines(self, tmp_path):
        f = tmp_path / "exclude.txt"
        f.write_text("a.example -> b.example\nno_arrow_here\n")
        result = read_exclude_list(str(f))
        assert result == {("a.example", "b.example")}
