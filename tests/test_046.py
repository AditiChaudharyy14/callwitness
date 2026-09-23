"""0.4.6: the chain commits to the full response; demo and compare wording."""
import importlib
import json
import sqlite3

from callwitness.chain import result_digest, verify_store
from callwitness.compare import _pct, _scale
from callwitness.record import Recorder
from callwitness.tracker import CallTracker


def _record(home, payload):
    rec = Recorder(home, "s", "L")
    rec.start_session(["x"])
    t = CallTracker(rec)
    t.on_client_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "read", "arguments": {}}})
    t.on_server_message({"jsonrpc": "2.0", "id": 1, "result": payload})
    rec.end_session(0)
    rec.close()


def _db(home):
    return sqlite3.connect(str(home / "callwitness.db"))


def test_full_response_hash_is_stored_and_verifies(tmp_path):
    payload = {"content": [{"type": "text", "text": "x" * 5000}]}
    _record(tmp_path, payload)
    stored = _db(tmp_path).execute("SELECT result_sha256 FROM calls").fetchone()[0]
    assert stored == result_digest(payload)
    rep = verify_store(tmp_path)[0][2]
    assert rep["intact"] is True and rep["verified"] == 1


def test_hash_covers_more_than_the_preview(tmp_path):
    a = {"content": [{"type": "text", "text": "x" * 5000 + "A"}]}
    b = {"content": [{"type": "text", "text": "x" * 5000 + "B"}]}
    assert result_digest(a) != result_digest(b)


def test_editing_the_hash_breaks_the_chain(tmp_path):
    _record(tmp_path, {"ok": 1})
    con = _db(tmp_path)
    con.execute("UPDATE calls SET result_sha256 = ?", ("0" * 64,))
    con.commit()
    assert verify_store(tmp_path)[0][2]["intact"] is False


def test_removing_the_hash_breaks_the_chain(tmp_path):
    _record(tmp_path, {"ok": 1})
    con = _db(tmp_path)
    con.execute("UPDATE calls SET result_sha256 = NULL")
    con.commit()
    assert verify_store(tmp_path)[0][2]["intact"] is False


def test_rows_without_the_field_still_verify(tmp_path):
    r = Recorder(tmp_path, "s", "L")
    r.start_session(["x"])
    r.call({"ts": "t", "tool": "t", "args": {}, "args_bytes": 2, "signals": {},
            "redaction": {}, "duration_ms": 1.0, "is_error": False,
            "result_bytes": 2, "result_preview": "{}"})
    r.end_session(0)
    r.close()
    rep = verify_store(tmp_path)[0][2]
    assert rep["intact"] is True and rep["verified"] == 1


def test_p0_reads_as_below_every_sample():
    assert _pct(0) == "<p1"
    assert _pct(37) == "p37"
    row = {"percentile": 0, "ratio": 0.8, "level": "same tool"}
    assert _scale(row) == "low end"


def test_callwitness_home_env(monkeypatch, tmp_path):
    import callwitness.cli as cli
    monkeypatch.delenv("BOLLARD_HOME", raising=False)
    monkeypatch.setenv("CALLWITNESS_HOME", str(tmp_path / "a"))
    assert str(importlib.reload(cli).DEFAULT_HOME) == str(tmp_path / "a")
    monkeypatch.delenv("CALLWITNESS_HOME")
    monkeypatch.setenv("BOLLARD_HOME", str(tmp_path / "b"))
    assert str(importlib.reload(cli).DEFAULT_HOME) == str(tmp_path / "b")
    monkeypatch.delenv("BOLLARD_HOME")
    importlib.reload(cli)
