"""Tamper evidence: what the chain catches, and what it honestly does not.

The negative tests carry as much weight as the positive ones. A verifier that
reports tampering on an untouched file is worse than no verifier -- nobody
believes the third alarm -- and one that claims more than it delivers is worse
still when the claim reaches an auditor.
"""

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from bollard.chain import GENESIS, digest, format_report, verify_rows, verify_store
from bollard.record import SCHEMA_VERSION, Recorder


def _store(n=12, home=None):
    home = home or Path(tempfile.mkdtemp())
    rec = Recorder(home, "sess-a", "demo")
    rec.start_session(["demo"])
    for i in range(n):
        rec.call({
            "ts": "2026-09-09T10:{:02d}:00+00:00".format(i),
            "tool": "send_email", "args": {"to": "ops@acme.com", "i": i},
            "args_bytes": 100 + i, "args_truncated": False,
            "signals": {}, "redaction": {}, "duration_ms": 1.0,
            "is_error": False, "result_bytes": 10, "result_preview": "ok",
        })
    rec.end_session(0)
    rec.close()
    return home


def _sql(home, statement, *params):
    con = sqlite3.connect(str(Path(home) / "bollard.db"))
    con.execute(statement, params)
    con.commit()
    con.close()


def _report(home):
    return {sid: rep for sid, _, rep in verify_store(home)}["sess-a"]


# -- it verifies what it should --------------------------------------------

def test_an_untouched_store_verifies():
    rep = _report(_store())
    assert rep["intact"] is True
    assert rep["verified"] == 12
    assert rep["break"] is None


def test_the_first_record_chains_from_genesis():
    home = _store(3)
    con = sqlite3.connect(str(Path(home) / "bollard.db"))
    row = con.execute("SELECT prev_hash FROM calls ORDER BY seq LIMIT 1").fetchone()
    assert row[0] == GENESIS


def test_the_head_is_stable_across_runs():
    """The head is what a person writes down; it must not drift on re-reading."""
    home = _store()
    assert _report(home)["head"] == _report(home)["head"]


def test_hashing_is_canonical_regardless_of_key_order():
    """A verifier that disagreed with the writer about layout would cry wolf."""
    a = {"tool": "x", "seq": 1, "ts": "t", "args_json": "{}"}
    b = {"args_json": "{}", "ts": "t", "seq": 1, "tool": "x"}
    assert digest(a, GENESIS) == digest(b, GENESIS)


def test_a_changed_predecessor_changes_the_hash():
    rec = {"tool": "x", "seq": 1}
    assert digest(rec, GENESIS) != digest(rec, "a" * 64)


# -- it catches what it should ---------------------------------------------

def test_editing_a_record_is_caught():
    """The classic cover-up: make an exfiltration look small after the fact."""
    home = _store()
    _sql(home, "UPDATE calls SET args_bytes=1 WHERE seq=5")
    rep = _report(home)
    assert rep["intact"] is False
    assert rep["break"]["seq"] == 5
    assert "edited" in rep["break"]["reason"]


def test_editing_the_stored_preview_is_caught():
    home = _store()
    _sql(home, "UPDATE calls SET result_preview='nothing to see' WHERE seq=7")
    assert _report(home)["break"]["seq"] == 7


def test_deleting_a_record_is_caught():
    home = _store()
    _sql(home, "DELETE FROM calls WHERE seq=6")
    rep = _report(home)
    assert rep["intact"] is False
    assert "removed, reordered or inserted" in rep["break"]["reason"]


def test_reordering_records_is_caught():
    home = _store()
    _sql(home, "UPDATE calls SET seq=999 WHERE seq=4")
    assert _report(home)["intact"] is False


def test_inserting_a_forged_record_is_caught():
    """A row with a plausible hash still has to chain to its neighbours."""
    home = _store()
    con = sqlite3.connect(str(Path(home) / "bollard.db"))
    con.execute(
        "INSERT INTO calls (session_id, label, ts, tool, args_json, args_bytes,"
        " args_truncated, signals_json, redaction_json, duration_ms, is_error,"
        " result_bytes, result_preview, seq, prev_hash, hash)"
        " VALUES ('sess-a','demo','t','forged','{}',1,0,'{}','{}',1,0,1,'',"
        " 5, 'ff', 'ee')")
    con.commit()
    con.close()
    assert _report(home)["intact"] is False


def test_the_break_is_reported_at_the_first_alteration_not_the_last():
    """Everything after a break is unverifiable; the location must be the start."""
    home = _store()
    _sql(home, "UPDATE calls SET args_bytes=1 WHERE seq=3")
    _sql(home, "UPDATE calls SET args_bytes=1 WHERE seq=9")
    assert _report(home)["break"]["seq"] == 3


# -- it does not claim what it cannot deliver ------------------------------

def test_records_written_before_chaining_are_not_called_tampering():
    """An upgraded install must not be told its history was altered."""
    home = Path(tempfile.mkdtemp())
    con = sqlite3.connect(str(home / "bollard.db"))
    con.executescript(
        "CREATE TABLE calls (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,"
        " label TEXT, ts TEXT, tool TEXT, args_json TEXT, args_bytes INTEGER,"
        " args_truncated INTEGER, signals_json TEXT, duration_ms REAL,"
        " is_error INTEGER, result_bytes INTEGER, result_preview TEXT);"
        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, label TEXT,"
        " command TEXT, started_at TEXT, ended_at TEXT, exit_code INTEGER);"
        "INSERT INTO sessions (session_id, label) VALUES ('old', 'legacy');"
        "INSERT INTO calls (session_id, tool) VALUES ('old', 'legacy_call');"
    )
    con.commit()
    con.close()

    Recorder(home, "new", "l").close()  # triggers migration
    rep = {sid: r for sid, _, r in verify_store(home)}["old"]
    assert rep["intact"] is True
    assert rep["unchained"] == 1
    assert rep["verified"] == 0
    assert "before chaining" in format_report(verify_store(home))


def test_migration_adds_the_columns_without_touching_old_rows():
    home = Path(tempfile.mkdtemp())
    con = sqlite3.connect(str(home / "bollard.db"))
    con.executescript(
        "CREATE TABLE calls (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,"
        " label TEXT, ts TEXT, tool TEXT, args_json TEXT, args_bytes INTEGER,"
        " args_truncated INTEGER, signals_json TEXT, duration_ms REAL,"
        " is_error INTEGER, result_bytes INTEGER, result_preview TEXT);"
        "INSERT INTO calls (session_id, tool) VALUES ('old', 'kept');"
    )
    con.commit()
    con.close()

    rec = Recorder(home, "new", "l")
    cols = {r[1] for r in rec._db.execute("PRAGMA table_info(calls)")}
    assert {"seq", "prev_hash", "hash"} <= cols
    assert rec._db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    tools = [r["tool"] for r in rec.all_calls()]
    assert "kept" in tools


def test_the_report_says_it_is_evidence_not_proof():
    """The honesty is part of the feature. An auditor will read this line."""
    out = format_report(verify_store(_store()))
    assert "tamper-evident, not tamper-proof" in out
    assert "recompute every hash" in out


def test_the_report_prints_a_head_worth_anchoring():
    out = format_report(verify_store(_store()))
    head = _report(_store())["head"]
    assert len(head) == 64
    assert "this machine cannot reach" in out


def test_a_wholesale_rewrite_is_not_detected_and_we_do_not_pretend_it_is():
    """The honest limit, pinned so nobody later claims more than this delivers.

    An attacker who edits a record AND recomputes every subsequent hash produces
    a chain that verifies. Only an external copy of the head defeats that, which
    is why the report prints one and tells you to store it elsewhere.
    """
    home = _store(6)
    con = sqlite3.connect(str(home / "bollard.db"))
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM calls ORDER BY seq")]
    original_head = verify_rows(rows)["head"]

    rows[2]["args_bytes"] = 999999           # the alteration
    prev = GENESIS
    for row in rows:                          # then re-chain the whole thing
        row["prev_hash"] = prev
        row["hash"] = digest(row, prev)
        con.execute("UPDATE calls SET args_bytes=?, prev_hash=?, hash=? WHERE id=?",
                    (row["args_bytes"], row["prev_hash"], row["hash"], row["id"]))
        prev = row["hash"]
    con.commit()
    con.close()

    rep = _report(home)
    assert rep["intact"] is True, "a fully recomputed chain verifies -- as documented"
    assert rep["head"] != original_head, (
        "but the head moves, which is the whole reason to write it down elsewhere")


# -- plumbing --------------------------------------------------------------

def test_verify_store_reports_calls_whose_session_row_never_landed():
    """A crash between the first call and end_session must not hide records."""
    home = _store(4)
    _sql(home, "DELETE FROM sessions WHERE session_id='sess-a'")
    assert [sid for sid, _, _ in verify_store(home)] == ["sess-a"]


def test_missing_store_raises_for_the_cli_to_handle():
    with pytest.raises(FileNotFoundError):
        verify_store(Path(tempfile.mkdtemp()) / "nope")


def test_empty_store_reads_as_empty_not_broken():
    home = Path(tempfile.mkdtemp())
    Recorder(home, "s", "l").close()
    assert "No sessions recorded yet" in format_report(verify_store(home))


def test_the_jsonl_carries_the_chain_too():
    """The database is not the only copy; the stream has to be checkable."""
    home = _store(3)
    lines = [json.loads(l) for l in (home / "calls.jsonl").read_text().splitlines()]
    assert all("hash" in r and "prev_hash" in r and "seq" in r for r in lines)
    assert lines[0]["prev_hash"] == GENESIS
    assert lines[1]["prev_hash"] == lines[0]["hash"]
