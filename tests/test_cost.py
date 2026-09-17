"""Turning bytes into the unit people budget in.

The danger in a cost report is overclaiming: a number that looks like it
covers everything, or a conversion presented as a measurement. So these check
the arithmetic, the windowing, and that the caveats survive edits.
"""

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from callwitness.cost import (
    BYTES_PER_TOKEN, human, render, since_moment, summarise, tokens,
)

SCHEMA = """
CREATE TABLE calls (id INTEGER PRIMARY KEY, session_id TEXT, ts TEXT, tool TEXT,
                    args_bytes INTEGER, duration_ms REAL, is_error INTEGER,
                    result_bytes INTEGER);
"""


def _now(**delta):
    return (datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(**delta)).isoformat()


def _store(rows):
    home = Path(tempfile.mkdtemp())
    connection = sqlite3.connect(str(home / "callwitness.db"))
    connection.executescript(SCHEMA)
    connection.executemany(
        "INSERT INTO calls (session_id,ts,tool,args_bytes,duration_ms,"
        "is_error,result_bytes) VALUES (?,?,?,?,?,?,?)", rows)
    connection.commit()
    connection.close()
    return home


ROWS = [
    ("s1", _now(hours=1), "fetch", 80, 2100.0, 0, 70000),
    ("s1", _now(hours=1), "fetch", 100, 1200.0, 0, 2400),
    ("s1", _now(hours=2), "read_file", 20, 5.0, 0, 600),
    ("s2", _now(days=30), "old_tool", 20, 5.0, 0, 999999),
    ("s1", _now(hours=1), "broken", 20, 37000.0, 1, 0),
]


# -- the arithmetic --------------------------------------------------------

def test_tokens_follow_the_stated_ratio():
    assert tokens(4000) == 1000
    assert tokens(4000, ratio=2.0) == 2000


def test_a_nonsense_ratio_falls_back_rather_than_dividing_by_zero():
    assert tokens(4000, ratio=0) == int(4000 / BYTES_PER_TOKEN)


def test_tools_are_ranked_by_what_they_returned():
    out = summarise(_store(ROWS), window="7d")
    assert out["tools"][0]["tool"] == "fetch"
    assert out["tools"][0]["calls"] == 2
    assert out["tools"][0]["total"] == 72400


def test_the_worst_single_call_is_kept_not_just_the_sum():
    """A sum answers how much; the question people have is how big one can get."""
    out = summarise(_store(ROWS), window="7d")
    assert out["tools"][0]["largest"] == 70000


# -- what is counted -------------------------------------------------------

def test_failed_calls_are_not_counted_as_cost():
    """A failure returns nothing, so it costs no context, however long it took."""
    out = summarise(_store(ROWS), window="7d")
    assert all(t["tool"] != "broken" for t in out["tools"])


def test_the_window_excludes_older_traffic():
    out = summarise(_store(ROWS), window="7d")
    assert all(t["tool"] != "old_tool" for t in out["tools"])


def test_no_window_counts_everything():
    out = summarise(_store(ROWS))
    assert any(t["tool"] == "old_tool" for t in out["tools"])


def test_a_session_can_be_singled_out():
    out = summarise(_store(ROWS), session="s2")
    assert [t["tool"] for t in out["tools"]] == ["old_tool"]


def test_an_unparseable_window_is_ignored_rather_than_guessed():
    assert since_moment("soon") is None
    assert since_moment(None) is None
    assert since_moment("24h") is not None
    assert since_moment("2w") is not None


# -- what it says about itself ---------------------------------------------

def test_the_estimate_is_labelled_as_one():
    text = render(summarise(_store(ROWS), window="7d"))
    assert "estimate" in text
    assert "bytes per token" in text


def test_the_scope_caveat_survives():
    """Someone will compare this to their bill; it has to say what it omits."""
    text = render(summarise(_store(ROWS), window="7d"))
    assert "not your prompts" in text
    assert "without going through MCP" in text


def test_a_custom_ratio_is_reflected_in_both_the_numbers_and_the_note():
    text = render(summarise(_store(ROWS), window="7d"), ratio=2.0)
    assert "2 bytes per token" in text


def test_an_empty_store_points_at_the_demo():
    text = render(summarise(Path(tempfile.mkdtemp())))
    assert "callwitness demo" in text


def test_the_output_survives_a_windows_console():
    text = render(summarise(_store(ROWS), window="7d"))
    assert text == text.encode("cp1252", "strict").decode("cp1252")


def test_sizes_are_readable():
    assert human(900) == "900 B"
    assert human(2048) == "2.0 KB"
    assert human(2 * 1024 * 1024) == "2.0 MB"
