"""The declared half of the comparison.

The finding this project exists for is a ratio: what a server puts in front of
a model at runtime, over what it declared at install time. Until now only the
numerator was recorded, so the ratio could be computed from the census and
nowhere else.

These tests hold the two halves apart. A listing must be measured, must not be
mistaken for a call, must not drag the schemas into the database with it, and
must never be invented when it was not observed.
"""

import json
import sqlite3

from callwitness import baseline, contribute
from callwitness.record import Recorder
from callwitness.tracker import CallTracker


def listing(request_id, tools):
    return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}}


TOOLS = [
    {"name": "deepwiki_fetch", "description": "Fetch a repository wiki",
     "inputSchema": {"type": "object",
                     "properties": {"url": {"type": "string"}}}},
]


def record_a_server(home, session, command, tools, sizes, tool_name):
    rec = Recorder(home, session, "x")
    rec.start_session(command)
    tracker = CallTracker(rec)
    if tools is not None:
        tracker.on_client_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        tracker.on_server_message(listing(1, tools))
    for i, size in enumerate(sizes):
        tracker.on_client_message(
            {"jsonrpc": "2.0", "id": 100 + i, "method": "tools/call",
             "params": {"name": tool_name, "arguments": {"url": "x"}}})
        tracker.on_server_message(
            {"jsonrpc": "2.0", "id": 100 + i,
             "result": {"content": [{"type": "text", "text": "y" * size}]}})
    rec.end_session(0)
    rec.close()


def test_a_listing_is_measured(tmp_path):
    record_a_server(tmp_path, "s1", ["npx", "-y", "mcp-deepwiki@latest"],
                    TOOLS, [200], "deepwiki_fetch")
    db = sqlite3.connect(str(tmp_path / "callwitness.db"))
    rows = db.execute(
        "SELECT payload FROM events WHERE kind = 'server:tools/list'").fetchall()
    db.close()
    assert len(rows) == 1
    payload = json.loads(rows[0][0])
    assert payload["tool_count"] == 1
    assert payload["declared_bytes"] > 0
    assert payload["tools"] == ["deepwiki_fetch"]


def test_a_listing_does_not_become_a_call(tmp_path):
    """Nothing was invoked. A row in `calls` would be a fabricated call."""
    record_a_server(tmp_path, "s1", ["npx", "-y", "mcp-deepwiki@latest"],
                    TOOLS, [200], "deepwiki_fetch")
    db = sqlite3.connect(str(tmp_path / "callwitness.db"))
    count = db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    db.close()
    assert count == 1


def test_the_schemas_are_not_kept(tmp_path):
    """Sizes, not a copy of somebody's interface."""
    record_a_server(tmp_path, "s1", ["npx", "-y", "mcp-deepwiki@latest"],
                    TOOLS, [200], "deepwiki_fetch")
    db = sqlite3.connect(str(tmp_path / "callwitness.db"))
    blob = " ".join(r[0] or "" for r in db.execute("SELECT payload FROM events"))
    db.close()
    assert "inputSchema" not in blob
    assert "Fetch a repository wiki" not in blob


def test_the_ratio_appears_when_both_halves_were_measured(tmp_path):
    record_a_server(tmp_path, "s1", ["npx", "-y", "mcp-deepwiki@latest"],
                    TOOLS, [200000], "deepwiki_fetch")
    server = {s["package"]: s
              for s in baseline.build(tmp_path)["servers"]}["npm:mcp-deepwiki"]
    assert server["declared_bytes"] > 0
    assert server["returned_over_declared"]["max"] > 100


def test_no_ratio_is_invented_when_nothing_was_declared(tmp_path):
    """0 means unknown. A ratio against an unknown denominator is a fiction."""
    record_a_server(tmp_path, "s2", ["/opt/acme/bin/billing-mcp"],
                    None, [512], "approve_wire_transfer")
    server = {s["package"]: s
              for s in baseline.build(tmp_path)["servers"]}["unlisted"]
    assert server["declared_bytes"] == 0
    assert "returned_over_declared" not in server


def test_the_contributed_payload_carries_the_declared_size(tmp_path):
    """It was 0 for every record until now, which removed half the comparison."""
    record_a_server(tmp_path, "s1", ["npx", "-y", "mcp-deepwiki@latest"],
                    TOOLS, [200000], "deepwiki_fetch")
    payload = contribute.build_payload(
        tmp_path, "00000000-0000-4000-8000-000000000000")
    server = {s["package"]: s for s in payload["servers"]}["npm:mcp-deepwiki"]
    assert server["declared_bytes"] > 0
    assert contribute.audit(payload) == []