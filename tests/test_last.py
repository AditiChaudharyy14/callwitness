"""Reading one run back out.

The failure that matters here is a quiet one: showing the wrong run, or a
tidy summary that omits the failure the person opened this to find. So these
lean on ordering and on what appears when things go wrong.
"""

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from callwitness.last import human, render, render_sessions, sessions, summarise

SCHEMA = """
CREATE TABLE sessions (session_id TEXT, label TEXT, command TEXT,
                       started_at TEXT, ended_at TEXT, exit_code INTEGER);
CREATE TABLE calls (id INTEGER PRIMARY KEY, session_id TEXT, label TEXT, ts TEXT,
                    tool TEXT, args_json TEXT, args_bytes INTEGER,
                    args_truncated INTEGER, signals_json TEXT, redaction_json TEXT,
                    seq INTEGER, prev_hash TEXT, hash TEXT, duration_ms REAL,
                    is_error INTEGER, result_bytes INTEGER, result_preview TEXT);
"""


def _store(sessions_rows, calls_rows):
    home = Path(tempfile.mkdtemp())
    connection = sqlite3.connect(str(home / "callwitness.db"))
    connection.executescript(SCHEMA)
    connection.executemany(
        "INSERT INTO sessions (session_id,label,command,started_at,ended_at,exit_code)"
        " VALUES (?,?,?,?,?,?)", sessions_rows)
    connection.executemany(
        "INSERT INTO calls (session_id,ts,tool,args_bytes,duration_ms,is_error,"
        "result_bytes,result_preview,signals_json) VALUES (?,?,?,?,?,?,?,?,?)",
        calls_rows)
    connection.commit()
    connection.close()
    return home


OLD = ("s-old", "filesystem", "npx fs", "2026-09-16T10:00:00",
       "2026-09-16T10:05:00", 0)
NEW = ("s-new", "playwright", "npx pw", "2026-09-17T11:09:00",
       "2026-09-17T11:13:12", 0)

CALLS = [
    ("s-old", "2026-09-16T10:01:00", "read_file", 10, 5.0, 0, 200, None, None),
    ("s-new", "2026-09-17T11:10:00", "deepwiki_fetch", 40, 9646.0, 0, 499999,
     None, '{"destinations": {"hosts": ["example.com"]}}'),
    ("s-new", "2026-09-17T11:11:00", "browser_find", 2, 8.0, 1, 0,
     '{"message": "element not found"}', None),
    ("s-new", "2026-09-17T11:11:30", "browser_find", 2, 17.0, 1, 0,
     '{"message": "element not found"}', None),
    ("s-new", "2026-09-17T11:12:00", "take_screenshot", 13, 2179.0, 0, 50140,
     None, '{"destinations": {"hosts": ["example.com"]}}'),
]


# -- which run ------------------------------------------------------------

def test_the_newest_run_is_the_one_reported():
    out = summarise(_store([OLD, NEW], CALLS))
    assert out["session"]["session_id"] == "s-new"
    assert out["calls"] == 4


def test_a_named_run_can_be_asked_for():
    out = summarise(_store([OLD, NEW], CALLS), session_id="s-old")
    assert out["calls"] == 1


def test_a_run_that_recorded_nothing_is_not_the_last_run():
    """A process that started and made no calls is not what anyone means."""
    empty = ("s-empty", "quiet", "npx x", "2026-09-17T12:00:00",
             "2026-09-17T12:00:01", 0)
    out = summarise(_store([OLD, NEW, empty], CALLS))
    assert out["session"]["session_id"] == "s-new"


# -- what it leads with ---------------------------------------------------

def test_failures_are_counted_and_given_a_reason():
    out = summarise(_store([NEW], CALLS))
    assert out["failed"] == 2
    assert out["failures"][0]["tool"] == "browser_find"
    assert out["failures"][0]["count"] == 2
    assert "element not found" in out["failures"][0]["why"]


def test_a_failure_appears_in_the_output_above_everything_else():
    text = render(summarise(_store([NEW], CALLS)), Path("."))
    assert text.index("failed") < text.index("biggest responses")


def test_failed_calls_are_not_offered_as_the_biggest_response():
    """A failure returning 0 bytes is not news; it would push out a real one."""
    out = summarise(_store([NEW], CALLS))
    assert all(not c["is_error"] for c in out["biggest"])
    assert out["biggest"][0]["tool"] == "deepwiki_fetch"


def test_the_slowest_does_not_repeat_the_biggest():
    """On a small run the same calls top both, and the report looked padded."""
    out = summarise(_store([NEW], CALLS))
    assert out["biggest"][0]["tool"] == "deepwiki_fetch"
    assert out["slowest"] == []


def test_a_slow_call_too_small_to_be_biggest_still_gets_surfaced():
    """The case the slowest list exists for: 54 seconds to return 120 bytes."""
    calls = [("s-new", "2026-09-17T11:1{}:00".format(i), "big_{}".format(i),
              2, 5.0, 0, 90000 - i, None, None) for i in range(6)]
    calls.append(("s-new", "2026-09-17T11:19:00", "small_slow", 2, 54000.0,
                  0, 120, None, None))
    out = summarise(_store([NEW], calls))
    assert "small_slow" not in [c["tool"] for c in out["biggest"]]
    assert out["slowest"][0]["tool"] == "small_slow"


def test_the_date_is_not_printed_twice_on_one_line():
    text = render(summarise(_store([NEW], CALLS)), Path("."))
    assert "11:09 -> 11:13" in text


def test_a_clean_run_says_so_rather_than_printing_an_empty_heading():
    clean = [("s-new", "2026-09-17T11:10:00", "read_file", 5, 3.0, 0, 100, None, None)]
    text = render(summarise(_store([NEW], clean)), Path("."))
    assert "nothing failed" in text


# -- totals ----------------------------------------------------------------

def test_the_totals_are_of_this_run_only():
    out = summarise(_store([OLD, NEW], CALLS))
    assert out["returned"] == 499999 + 50140
    assert round(out["spent_ms"]) == round(9646.0 + 8.0 + 17.0 + 2179.0)


def test_destinations_are_collected_from_the_run():
    out = summarise(_store([NEW], CALLS))
    assert out["destinations"][0] == ("example.com", 2)


# -- nothing there ---------------------------------------------------------

def test_an_empty_store_tells_you_what_to_run():
    home = Path(tempfile.mkdtemp())
    assert summarise(home) is None
    text = render(None, home)
    assert "callwitness demo" in text


def test_a_store_with_no_sessions_does_not_raise():
    assert summarise(_store([], [])) is None
    assert sessions(_store([], [])) == []


# -- listing runs ----------------------------------------------------------

def test_runs_are_listed_newest_first():
    rows = sessions(_store([OLD, NEW], CALLS))
    assert [r["session_id"] for r in rows] == ["s-new", "s-old"]


def test_a_run_that_started_moments_ago_is_still_running():
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    running = ("s-run", "pw", "npx pw", now, None, None)
    text = render_sessions(sessions(_store([running], [])))
    assert "still running" in text


def test_timestamps_carrying_an_offset_do_not_crash():
    """The real store has both shapes, and mixing them raises on subtraction.

    Rows written by different versions carry '+00:00' or nothing. Fixtures
    used only the naive form, so this went out and crashed on first contact
    with a real database.
    """
    stale = ("s-tz", "census", "npx x", "2026-09-13T08:27:00+00:00", None, None)
    text = render_sessions(sessions(_store([stale], [])))
    assert "no end recorded" in text


def test_a_z_suffix_is_also_understood():
    stale = ("s-z", "census", "npx x", "2026-09-13T08:27:00Z", None, None)
    assert "no end recorded" in render_sessions(sessions(_store([stale], [])))


def test_an_unclosed_run_from_days_ago_is_not_called_running():
    """The recorder closes sessions properly; a killed process cannot.

    Saying a process that died four days ago is still running is the tool
    stating something false about its own records.
    """
    stale = ("s-old2", "census", "npx x", "2026-09-13T08:27:00", None, None)
    text = render_sessions(sessions(_store([stale], [])))
    assert "no end recorded" in text
    assert "still running" not in text


def test_activity_decides_it_not_the_start_time():
    """A long run that is still making calls is running, however old it is."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    started = (now - timedelta(days=2)).isoformat(timespec="seconds")
    recent = now.isoformat(timespec="seconds")
    row = ("s-long", "pw", "npx pw", started, None, None)
    calls = [("s-long", recent, "read_file", 2, 1.0, 0, 100, None, None)]
    text = render_sessions(sessions(_store([row], calls)))
    assert "still running" in text


# -- presentation ----------------------------------------------------------

def test_sizes_are_readable():
    assert human(900) == "900 B"
    assert human(2048) == "2.0 KB"
    assert human(3 * 1024 * 1024) == "3.0 MB"


def test_the_output_survives_a_windows_console():
    text = render(summarise(_store([NEW], CALLS)), Path("."))
    assert text == text.encode("cp1252", "strict").decode("cp1252")


# -- drilling in -----------------------------------------------------------
#
# `last` printed "callwitness tail --session 6d4e47f7" before tail accepted
# --session, so the tool told people to run a command that did not exist.
# A command that prints an identifier has to accept the identifier it printed.

def test_a_session_is_matched_on_the_prefix_last_prints():
    from callwitness.last import calls
    rows = calls(_store([NEW], CALLS), session="s-ne")
    assert len(rows) == 4
    assert all(r["tool"] != "read_file" for r in rows)


def test_errors_only_shows_the_failures():
    from callwitness.last import calls
    rows = calls(_store([OLD, NEW], CALLS), errors_only=True)
    assert len(rows) == 2
    assert {r["tool"] for r in rows} == {"browser_find"}


def test_the_two_filters_compose():
    from callwitness.last import calls
    rows = calls(_store([OLD, NEW], CALLS), session="s-old", errors_only=True)
    assert rows == []


def test_calls_come_back_oldest_first_within_the_window():
    from callwitness.last import calls
    rows = calls(_store([NEW], CALLS), limit=2)
    assert [r["tool"] for r in rows] == ["browser_find", "take_screenshot"]


def test_a_failure_line_carries_its_reason():
    from callwitness.last import calls, render_calls
    text = render_calls(calls(_store([NEW], CALLS), errors_only=True),
                        errors_only=True)
    assert "ERR" in text
    assert "element not found" in text


def test_no_matches_says_so_rather_than_printing_nothing():
    from callwitness.last import render_calls
    assert "No errors" in render_calls([], errors_only=True)
    assert "No calls in session abc" in render_calls([], session="abc")


def test_the_drill_down_survives_a_windows_console():
    from callwitness.last import calls, render_calls
    text = render_calls(calls(_store([NEW], CALLS)))
    assert text == text.encode("cp1252", "strict").decode("cp1252")
