"""The local baseline is generated in order to be given away.

That is what makes these tests matter more than they look. `contribute` asks
before anything leaves; `callwitness baseline` produces a file whose entire
purpose is to be handed to a benchmark, pasted into an issue, or committed to
someone else's repo. Nobody is going to read 20 KB of JSON first.

So: same leak test as contribute, against the same alarming database.
"""

import json
import sqlite3
from pathlib import Path

from callwitness import baseline as b

SECRETS = {
    "path": "/home/aditi/Documents/investors/term-sheet-draft.pdf",
    "host": "billing-prod-3.internal.acme.example",
    "tool": "approve_wire_transfer",
}


def seed(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(home / "callwitness.db"))
    db.executescript("""
        CREATE TABLE sessions (session_id TEXT PRIMARY KEY, label TEXT,
                               command TEXT, started_at TEXT, ended_at TEXT,
                               exit_code INTEGER);
        CREATE TABLE calls (id INTEGER PRIMARY KEY AUTOINCREMENT,
                            session_id TEXT, label TEXT, ts TEXT, tool TEXT,
                            args_json TEXT, args_bytes INTEGER,
                            args_truncated INTEGER, signals_json TEXT,
                            redaction_json TEXT, seq INTEGER, prev_hash TEXT,
                            hash TEXT, duration_ms REAL, is_error INTEGER,
                            result_bytes INTEGER, result_preview TEXT);
    """)
    db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)",
               ("s1", "fs", "npx -y @modelcontextprotocol/server-filesystem "
                + SECRETS["path"], "2026-09-13T09:00:00Z", None, 0))
    db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)",
               ("s2", "billing", "/opt/acme/bin/billing-mcp --host "
                + SECRETS["host"], "2026-09-13T10:00:00Z", None, 0))
    for i, size in enumerate([180, 4210, 334987]):
        db.execute(
            "INSERT INTO calls (session_id, label, ts, tool, args_bytes, "
            "duration_ms, is_error, result_bytes) VALUES (?,?,?,?,?,?,?,?)",
            ("s1", "fs", "2026-09-13T09:0{}:00Z".format(i), "list_directory",
             120, 12.0, 0, size))
    db.execute(
        "INSERT INTO calls (session_id, label, ts, tool, args_bytes, "
        "duration_ms, is_error, result_bytes) VALUES (?,?,?,?,?,?,?,?)",
        ("s2", "billing", "2026-09-13T10:00:00Z", SECRETS["tool"],
         240, 88.0, 0, 512))
    db.commit()
    db.close()


def test_nothing_private_reaches_a_document_meant_to_be_shared(tmp_path):
    seed(tmp_path)
    blob = json.dumps(b.build(tmp_path))
    for label, secret in SECRETS.items():
        assert secret not in blob, "leaked {}".format(label)
    for fragment in ("acme", "aditi", "Documents", "investors", "wire",
                     "approve", "billing", "prod-3"):
        assert fragment.lower() not in blob.lower(), "leaked {!r}".format(fragment)


def test_it_is_the_same_schema_as_the_published_baseline(tmp_path):
    """One reader must work on both files, or the whole idea fails."""
    seed(tmp_path)
    doc = b.build(tmp_path)
    assert doc["schema"] == "callwitness.baseline.v1"
    for key in ("sample", "caveat", "returned_bytes_all", "overall", "servers"):
        assert key in doc
    for key in ("n", "min", "p50", "p95", "max"):
        assert key in doc["overall"]["returned_bytes"]
    for server in doc["servers"]:
        for key in ("package", "declared_bytes", "tool_count",
                    "returned_bytes", "argument_bytes", "calls"):
            assert key in server


def test_origin_says_which_file_a_consumer_is_holding(tmp_path):
    """Local numbers and published numbers must never be silently interchangeable."""
    seed(tmp_path)
    assert b.build(tmp_path)["origin"] == "local"


def test_public_tool_names_survive_and_private_ones_do_not(tmp_path):
    seed(tmp_path)
    by_package = {s["package"]: s for s in b.build(tmp_path)["servers"]}
    public = by_package["npm:@modelcontextprotocol/server-filesystem"]
    assert [c["tool"] for c in public["calls"]] == ["list_directory"] * 3
    assert [c["tool"] for c in by_package["unlisted"]["calls"]] == ["tool_1"]


def test_sizes_are_preserved_because_they_are_the_point(tmp_path):
    seed(tmp_path)
    doc = b.build(tmp_path)
    assert doc["returned_bytes_all"] == [180, 512, 4210, 334987]
    assert doc["overall"]["returned_bytes"]["max"] == 334987


def test_no_recording_is_an_empty_document_not_a_crash(tmp_path):
    doc = b.build(tmp_path)
    assert doc["servers"] == []
    assert doc["sample"]["calls"] == 0
    assert doc["returned_bytes_all"] == []