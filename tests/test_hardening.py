"""Regressions for the hardening pass.

Each test here corresponds to a defect found by probing the running code, not
by reading it. The comments record what the defect was, because a test whose
purpose is forgotten is a test someone eventually deletes.
"""

import json
import io
import tempfile
import time
from pathlib import Path

import pytest

from callwitness.analyze import EMAIL_RE, MAX_SCAN_CHARS, extract_entities, extract_signals
from callwitness.proxy import Proxy
from callwitness.record import SCHEMA_VERSION, Recorder
from callwitness.redact import RedactionStats, redact_structure, redact_value


def _proxy(**kw):
    rec = Recorder(Path(tempfile.mkdtemp()), "sess", "test")
    return Proxy(["true"], rec, **kw), rec


# --------------------------------------------------------------------------
# 1. ReDoS in the email pattern
#
# The original [\w.+-]+@... was quadratic on a long word-character run: 20KB of
# base64 cost ~1.8s, 80KB ~30s. Reachable by a large tool payload -- precisely
# the payload the product exists to flag -- so an attacker could make the
# recorder spend minutes of CPU instead of raising an alert.
# --------------------------------------------------------------------------

def test_email_scan_is_linear_not_quadratic():
    blob = "Q" * 60_000
    start = time.perf_counter()
    EMAIL_RE.findall(blob)
    elapsed = time.perf_counter() - start
    # The old pattern took >10s here. Generous bound so a slow CI box does not
    # produce a flake, while still failing hard if quadratic behaviour returns.
    assert elapsed < 1.0, "email scan went superlinear again: {:.2f}s".format(elapsed)


def test_email_scan_scales_subquadratically():
    def timed(n):
        blob = "Q" * n
        t0 = time.perf_counter()
        EMAIL_RE.findall(blob)
        return time.perf_counter() - t0

    small, large = timed(20_000), timed(80_000)
    # 4x the input under a quadratic pattern is ~16x the time. Allow generous
    # headroom for timer noise on tiny durations; 16x would still fail.
    assert large < max(small * 8, 0.5)


@pytest.mark.parametrize("text,expected", [
    ("mail ops@acme.com now", ["ops@acme.com"]),
    ("a.b+tag@sub.domain.co.uk", ["a.b+tag@sub.domain.co.uk"]),
    ("x@y.io,z@w.org", ["x@y.io", "z@w.org"]),
    ("no-at-here", []),
    ("user@host", []),
])
def test_email_pattern_still_finds_real_addresses(text, expected):
    """The fix must not cost recall -- it is the destination signal."""
    assert EMAIL_RE.findall(text) == expected


def test_entity_scan_is_capped():
    huge = "word " * 200_000
    assert len(huge) > MAX_SCAN_CHARS
    start = time.perf_counter()
    extract_entities(huge)
    assert time.perf_counter() - start < 1.0


def test_large_payload_does_not_stall_observation():
    """A 2MB argument used to hang on_client_message indefinitely."""
    proxy, _ = _proxy()
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "upload", "arguments": {"blob": "A" * 2_000_000}}}
    start = time.perf_counter()
    proxy.on_client_message(msg)
    assert time.perf_counter() - start < 2.0


# --------------------------------------------------------------------------
# 2. JSON-RPC batches were dropped entirely
#
# _pump gated on text.startswith("{"). A batch is a top-level array, so every
# call inside one was recorded as zero calls -- an undercount that grew with
# the busiest clients.
# --------------------------------------------------------------------------

def test_batched_calls_are_all_recorded():
    proxy, rec = _proxy()
    batch = [
        {"jsonrpc": "2.0", "id": i, "method": "tools/call",
         "params": {"name": "send_email", "arguments": {"to": "u{}@x.com".format(i)}}}
        for i in range(3)
    ]
    line = (json.dumps(batch) + "\n").encode()
    proxy._pump(io.BytesIO(line), io.BytesIO(), proxy.on_client_message)
    assert len(proxy.pending) == 3

    for i in range(3):
        proxy.on_server_message({"id": i, "result": {"ok": True}})
    assert len(rec.all_calls()) == 3


def test_malformed_batch_member_does_not_lose_the_rest():
    proxy, _ = _proxy()
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "a", "arguments": {}}},
        "not an object",
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "b", "arguments": {}}},
    ]
    line = (json.dumps(batch) + "\n").encode()
    proxy._pump(io.BytesIO(line), io.BytesIO(), proxy.on_client_message)
    assert len(proxy.pending) == 2


# --------------------------------------------------------------------------
# 3. Secrets were written to disk in plaintext
#
# stored = args went straight into args_json and calls.jsonl. Every install
# became a credential store that did not exist before Callwitness was installed.
# --------------------------------------------------------------------------

def test_secrets_never_reach_storage():
    proxy, rec = _proxy()
    proxy.on_client_message({
        "id": "k", "method": "tools/call",
        "params": {"name": "deploy", "arguments": {
            "api_key": "sk-ant-api03-NOTAREALSECRET1234567890abcdefgh",
            "db": "postgres://admin:hunter2@db.internal:5432/prod",
        }},
    })
    proxy.on_server_message({"id": "k", "result": {"ok": True}})

    stored = rec.all_calls()[0]["args_json"]
    assert "NOTAREALSECRET" not in stored
    assert "hunter2" not in stored

    on_disk = (rec.home / "calls.jsonl").read_text()
    assert "NOTAREALSECRET" not in on_disk
    assert "hunter2" not in on_disk


def test_redaction_preserves_the_destination_signal():
    """Killing the credential must not kill the host -- the host is the point."""
    value, _ = redact_value("postgres://admin:hunter2@db.internal:5432/prod")
    assert "hunter2" not in value
    assert "db.internal" in value


def test_true_size_survives_redaction():
    proxy, rec = _proxy()
    args = {"token": "sk-ant-api03-" + "z" * 40, "note": "x" * 500}
    raw_bytes = len(json.dumps(args).encode())
    proxy.on_client_message({"id": "s", "method": "tools/call",
                             "params": {"name": "t", "arguments": args}})
    proxy.on_server_message({"id": "s", "result": {}})
    assert rec.all_calls()[0]["args_bytes"] == raw_bytes


def test_redaction_is_counted_by_reason():
    _, stats = redact_structure({
        "password": "anything",
        "url": "https://api.example.com?token=abc123def456ghi789",
    })
    assert stats.total >= 2
    assert "key_name" in stats.counts


def test_redaction_can_be_disabled_explicitly():
    proxy, rec = _proxy(redact=False)
    proxy.on_client_message({"id": "n", "method": "tools/call",
                             "params": {"name": "t",
                                        "arguments": {"note": "plain text"}}})
    proxy.on_server_message({"id": "n", "result": {}})
    assert "plain text" in rec.all_calls()[0]["args_json"]


def test_uuids_are_not_mistaken_for_secrets():
    value, changed = redact_value("550e8400-e29b-41d4-a716-446655440000")
    assert not changed


def test_ordinary_prose_is_left_alone():
    text = "Please forward the quarterly summary to the operations team today."
    value, changed = redact_value(text)
    assert not changed and value == text


def test_redactor_never_raises_on_hostile_input():
    """Contract: a redaction bug must not become a traffic bug."""
    class Hostile:
        def __str__(self):
            raise RuntimeError("boom")

    for bad in [Hostile(), {"k": Hostile()}, [Hostile()], float("nan")]:
        redact_structure(bad)  # must not raise


# --------------------------------------------------------------------------
# 4. Orphaned requests leaked forever
# --------------------------------------------------------------------------

def test_orphaned_requests_are_reaped_and_recorded():
    proxy, rec = _proxy(pending_ttl=0.0)
    for i in range(70):
        proxy.on_client_message({"id": i, "method": "tools/call",
                                 "params": {"name": "t", "arguments": {}}})
    assert len(proxy.pending) < 70
    reaped = [r for r in rec.all_calls() if r["result_preview"] == "<no_response>"]
    assert reaped, "reaped calls should be recorded, not silently dropped"


def test_pending_map_is_not_reaped_prematurely():
    proxy, _ = _proxy(pending_ttl=900.0)
    for i in range(70):
        proxy.on_client_message({"id": i, "method": "tools/call",
                                 "params": {"name": "t", "arguments": {}}})
    assert len(proxy.pending) == 70


# --------------------------------------------------------------------------
# 5. Storage: schema versioning and durability settings
# --------------------------------------------------------------------------

def test_schema_version_is_recorded():
    rec = Recorder(Path(tempfile.mkdtemp()), "s", "l")
    assert rec._db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_wal_mode_is_enabled():
    rec = Recorder(Path(tempfile.mkdtemp()), "s", "l")
    mode = rec._db.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_v1_database_migrates_without_data_loss():
    """An existing install must not lose its history on upgrade."""
    import sqlite3
    home = Path(tempfile.mkdtemp())
    db = sqlite3.connect(str(home / "callwitness.db"))
    db.executescript(
        "CREATE TABLE calls (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,"
        " label TEXT, ts TEXT, tool TEXT, args_json TEXT, args_bytes INTEGER,"
        " args_truncated INTEGER, signals_json TEXT, duration_ms REAL,"
        " is_error INTEGER, result_bytes INTEGER, result_preview TEXT);"
    )
    db.execute("INSERT INTO calls (tool) VALUES ('legacy_call')")
    db.commit()
    db.close()

    rec = Recorder(home, "s", "l")
    tools = [r["tool"] for r in rec.all_calls()]
    assert "legacy_call" in tools
    cols = {r[1] for r in rec._db.execute("PRAGMA table_info(calls)")}
    assert "redaction_json" in cols


def test_signals_still_split_destinations_from_content():
    """The core distinction must survive all of the above."""
    signals = extract_signals({
        "to": "ops@acme.com",
        "body": "cc " + " ".join("user{}@corp.com".format(i) for i in range(50)),
    })
    assert signals["destinations"]["emails"] == ["ops@acme.com"]
    assert signals["content_counts"]["emails"] == 50
