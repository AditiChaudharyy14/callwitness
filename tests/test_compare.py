"""Comparing your numbers against everyone else's.

The risk here is not a crash, it is a confident wrong comparison: a row that
says p99 because it was measured against the wrong distribution, or a network
failure that takes the local numbers down with it. So these lean on the
matching levels and on the ways fetching can fail.
"""

import json
import tempfile
from pathlib import Path

from callwitness.compare import (
    EXACT, GLOBAL, PACKAGE, compare, fetch, forms, human, index, render, where,
)

PUBLIC = {
    "schema": "callwitness.baseline.v1",
    "origin": "census",
    "sample": {"servers_called": 2, "calls": 6},
    "returned_bytes_all": [100, 200, 300, 400, 500, 60000],
    "overall": {"returned_bytes": {"n": 6, "min": 100, "p50": 300,
                                   "p95": 60000, "max": 60000}},
    "servers": [
        {"package": "deepwiki", "declared_bytes": 774,
         "calls": [{"tool": "read_wiki", "returned_bytes": 60000},
                   {"tool": "read_wiki", "returned_bytes": 500}]},
        {"package": "sympy", "declared_bytes": 63050,
         "calls": [{"tool": "simplify", "returned_bytes": 100},
                   {"tool": "solve", "returned_bytes": 200},
                   {"tool": "solve", "returned_bytes": 300},
                   {"tool": "integrate", "returned_bytes": 400}]},
    ],
}


def _local(servers):
    return {"schema": "callwitness.baseline.v1", "origin": "local",
            "servers": servers}


# -- which distribution a row was measured against -------------------------

def test_the_same_tool_is_preferred_over_the_package():
    rows = compare(_local([{"package": "deepwiki",
                            "calls": [{"tool": "read_wiki", "returned_bytes": 700}]}]),
                   PUBLIC)
    assert rows[0]["level"] == EXACT


def test_an_unseen_tool_falls_back_to_the_package():
    rows = compare(_local([{"package": "sympy",
                            "calls": [{"tool": "factor", "returned_bytes": 250}]}]),
                   PUBLIC)
    assert rows[0]["level"] == PACKAGE


def test_an_unseen_package_falls_back_to_everything():
    rows = compare(_local([{"package": "nobody-has-this",
                            "calls": [{"tool": "x", "returned_bytes": 250}]}]),
                   PUBLIC)
    assert rows[0]["level"] == GLOBAL


def test_the_level_is_reported_so_a_weak_comparison_looks_weak():
    """A global-distribution row must never be presentable as a same-tool row."""
    rows = compare(_local([{"package": "nope", "calls": [{"tool": "x", "returned_bytes": 1}]}]),
                   PUBLIC)
    assert GLOBAL in render(rows, PUBLIC, "cached")


# -- the arithmetic --------------------------------------------------------

def test_percentile_is_a_rank_not_an_interpolation():
    assert where(100, [100, 200, 300, 400]) == 25
    assert where(400, [100, 200, 300, 400]) == 100
    assert where(0, [100, 200]) == 0


def test_an_empty_reference_does_not_divide_by_zero():
    assert where(5, []) == 0


def test_the_worst_row_is_printed_first():
    rows = compare(_local([{"package": "sympy",
                            "calls": [{"tool": "solve", "returned_bytes": 100000},
                                      {"tool": "simplify", "returned_bytes": 1}]}]),
                   PUBLIC)
    assert rows[0]["tool"] == "solve"


# -- failing safely --------------------------------------------------------

def test_no_network_and_no_cache_returns_no_document_rather_than_raising():
    home = Path(tempfile.mkdtemp())
    document, source = fetch(home, url="https://example.invalid/none.json")
    assert document is None
    assert "unavailable" in source


def test_a_corrupt_cache_is_treated_as_a_miss():
    home = Path(tempfile.mkdtemp())
    (home / "baseline-public-v1.json").write_text("{not json", encoding="utf-8")
    document, source = fetch(home, url="https://example.invalid/none.json")
    assert document is None


def test_a_fresh_cache_is_used_without_the_network():
    home = Path(tempfile.mkdtemp())
    (home / "baseline-public-v1.json").write_text(json.dumps(PUBLIC), encoding="utf-8")
    document, source = fetch(home, url="https://example.invalid/none.json")
    assert source == "cached"
    assert document["origin"] == "census"


def test_nothing_recorded_says_so_instead_of_printing_an_empty_table():
    assert "Nothing recorded yet" in render([], PUBLIC, "cached")


def test_the_output_states_that_nothing_was_sent():
    """The one line that has to survive every future edit to this file."""
    rows = compare(_local([{"package": "deepwiki",
                            "calls": [{"tool": "read_wiki", "returned_bytes": 700}]}]),
                   PUBLIC)
    assert "Nothing about your traffic was sent" in render(rows, PUBLIC, "fetched")


# -- presentation ----------------------------------------------------------

def test_sizes_are_readable():
    assert human(512) == "512 B"
    assert human(2048) == "2.0 KB"
    assert human(3 * 1024 * 1024) == "3.0 MB"


def test_a_server_with_no_calls_produces_no_rows():
    assert compare(_local([{"package": "quiet", "calls": []}]), PUBLIC) == []


def test_the_index_survives_a_document_with_no_precomputed_list():
    trimmed = dict(PUBLIC)
    trimmed.pop("returned_bytes_all")
    assert index(trimmed)["all"]


# -- joining two documents that name the same package differently ----------
#
# The first run of --compare matched nothing at package level: every row fell
# through to the global distribution and still printed a percentile, so a
# total join failure looked like a working comparison. These pin the shapes
# that actually occur in the two documents.

def _joins(a, b):
    return bool(set(forms(a)) & set(forms(b)))


def test_an_ecosystem_prefix_does_not_stop_a_match():
    assert _joins("npm:chrome-devtools-mcp", "chrome-devtools")
    assert _joins("pypi:arxiv-mcp-server", "arxiv")


def test_an_mcp_affix_does_not_stop_a_match():
    assert _joins("pypi:mcp-server-calculator", "calculator")
    assert _joins("npm:@modelcontextprotocol/server-memory", "memory")


def test_a_scope_is_the_name_when_the_rest_is_generic():
    """@playwright/mcp reduces to 'mcp', which would otherwise match anything."""
    assert _joins("npm:@playwright/mcp", "playwright")
    assert "mcp" not in forms("npm:@playwright/mcp")


def test_generic_words_are_never_keys():
    assert forms("npm:mcp") == []
    assert forms("server") == []


def test_unrelated_packages_still_do_not_match():
    assert not _joins("npm:chrome-devtools-mcp", "sympy")
    assert not _joins("pypi:arxiv-mcp-server", "deepwiki")


def test_the_words_never_contradict_the_percentile():
    """`p0  about typical` appeared in the first run and read as a bug."""
    from callwitness.compare import _scale
    assert _scale({"percentile": 0, "ratio": 0.7, "level": PACKAGE}) == "low end"
    assert _scale({"percentile": 99, "ratio": 1.2, "level": PACKAGE}) == "high end"
    assert _scale({"percentile": 50, "ratio": 1.0, "level": PACKAGE}) == "about typical"


def test_a_large_difference_still_gets_a_number():
    from callwitness.compare import _scale
    assert "x the" in _scale({"percentile": 99, "ratio": 80.0, "level": EXACT})
    assert "smaller" in _scale({"percentile": 1, "ratio": 0.05, "level": EXACT})


def test_a_local_name_finds_its_public_twin():
    """End to end: the row should say same tool, not all servers."""
    public = dict(PUBLIC, servers=[
        {"package": "deepwiki",
         "calls": [{"tool": "read_wiki", "returned_bytes": 60000}]}])
    rows = compare(_local([{"package": "npm:deepwiki-mcp",
                            "calls": [{"tool": "read_wiki", "returned_bytes": 700}]}]),
                   public)
    assert rows[0]["level"] == EXACT
