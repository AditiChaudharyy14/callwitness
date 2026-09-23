"""The two verifier holes reported on 22-23 Sep 2026, kept as regressions."""
import sqlite3

from callwitness.chain import verify_rows, verify_store
from callwitness.record import Recorder


def _store(home, label, duration_ms):
    r = Recorder(home, "sess", label)
    r.start_session([label])
    for i in range(4):
        r.call({"ts": "2026-09-22T00:0%d:00Z" % i, "tool": "read_file",
                "args": {"path": "f%d" % i}, "args_bytes": 10, "signals": {},
                "redaction": {}, "duration_ms": duration_ms, "is_error": False,
                "result_bytes": 20, "result_preview": "harmless"})
    r.end_session(0)
    r.close()


def _forge(home, seq):
    con = sqlite3.connect(str(home / "callwitness.db"))
    con.execute(
        "INSERT INTO calls (session_id,label,ts,tool,args_json,args_bytes,"
        "args_truncated,signals_json,redaction_json,seq,prev_hash,hash,"
        "duration_ms,is_error,result_bytes,result_preview) VALUES "
        "('sess','A','2026-09-22T00:09:00Z','send_email',"
        "'{\"to\":\"attacker@example.com\"}',30,0,'{}','{}',?,NULL,NULL,"
        "9.0,0,2,'ok')", (seq,))
    con.commit()
    con.close()


def test_unhashed_row_appended_to_a_chained_session_is_a_break(tmp_path):
    home = tmp_path / "a"
    _store(home, "A", 5.0)
    _forge(home, 5)
    rep = verify_store(home)[0][2]
    assert rep["intact"] is False
    assert rep["break"]["seq"] == 5


def test_unhashed_row_placed_before_the_chain_is_a_break(tmp_path):
    home = tmp_path / "b"
    _store(home, "A", 5.0)
    _forge(home, 0)
    rep = verify_store(home)[0][2]
    assert rep["intact"] is False
    assert rep["break"]["seq"] == 0


def test_a_wholly_pre_chain_session_still_reads_as_older():
    rows = [{"seq": None, "id": i, "hash": None} for i in range(3)]
    rep = verify_rows(rows)
    assert rep["intact"] is True
    assert rep["unchained"] == 3
    assert rep["verified"] == 0


def test_int_duration_verifies_on_an_untouched_store(tmp_path):
    home = tmp_path / "c"
    _store(home, "B", 5)          # an int, not 5.0
    rep = verify_store(home)[0][2]
    assert rep["intact"] is True, rep["break"]
    assert rep["verified"] == 4


def test_float_duration_still_verifies(tmp_path):
    home = tmp_path / "d"
    _store(home, "C", 5.25)
    rep = verify_store(home)[0][2]
    assert rep["intact"] is True
    assert rep["verified"] == 4
