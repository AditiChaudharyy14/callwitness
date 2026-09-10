"""The HTTP transport, tested against a real upstream server on a real socket.

These are end-to-end: a throwaway MCP-ish server, the proxy in front of it, and
an ordinary HTTP client. Nothing is mocked, because the properties that matter
here -- byte-identical relay, SSE arriving incrementally, headers surviving the
hop -- are exactly the ones a mock would assert away.
"""

import json
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from callwitness.http import HttpProxy, MessageSink
from callwitness.record import Recorder

pytestmark = pytest.mark.slow


# -- a minimal upstream ----------------------------------------------------

class _Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).seen.append((dict(self.headers), body))
        msg = json.loads(body.decode())
        ids = [m.get("id") for m in (msg if isinstance(msg, list) else [msg])]

        if self.path.endswith("/sse"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Mcp-Session-Id", "sess-abc")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for rid in ids:
                frame = ("data: " + json.dumps(
                    {"jsonrpc": "2.0", "id": rid,
                     "result": {"content": [{"type": "text", "text": "ok"}]}}
                ) + "\n\n").encode()
                self.wfile.write(b"%x\r\n" % len(frame) + frame + b"\r\n")
                self.wfile.flush()
                time.sleep(0.02)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return

        payload = json.dumps([
            {"jsonrpc": "2.0", "id": rid, "result": {"ok": True}} for rid in ids
        ] if isinstance(msg, list) else
            {"jsonrpc": "2.0", "id": ids[0], "result": {"ok": True}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Mcp-Session-Id", "sess-abc")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        frame = b'data: {"jsonrpc":"2.0","method":"notifications/message"}\n\n'
        self.wfile.write(b"%x\r\n" % len(frame) + frame + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def do_DELETE(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def stack():
    _Upstream.seen = []
    up = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    up.daemon_threads = True
    threading.Thread(target=up.serve_forever, daemon=True).start()
    up_url = "http://127.0.0.1:{}".format(up.server_address[1])

    rec = Recorder(Path(tempfile.mkdtemp()), "sess", "http")
    proxy = HttpProxy(up_url, rec, port=0)
    handler = proxy._handler_class()
    srv = ThreadingHTTPServer((proxy.host, 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:{}".format(srv.server_address[1])

    yield base, up_url, rec, proxy

    srv.shutdown()
    up.shutdown()
    proxy.observer.close()


def _recorded(rec, proxy, expected, timeout=10.0):
    """Wait until `expected` calls have been recorded, then return them.

    Observation is asynchronous by design: the relay hands work to a queue on
    another thread and never waits for it. Over HTTP every request is its own
    handler thread, so there is no stream boundary where a test can say "now
    everything is written" -- the client's read() returning only means the
    bytes reached the client, not that the server thread finished handing its
    copy to the observer.

    The original assertions called drain() and read immediately. drain() waits
    for what has already been SUBMITTED, so on a loaded runner it can return
    before the handler submitted anything at all. That is the macOS py3.13 CI
    failure: 0 calls, not 2 of 3. The test encoded a synchronous expectation of
    an asynchronous system; the product was correct the whole time.

    The contract is "recorded shortly", so that is what this waits for.
    """
    deadline = time.time() + timeout
    while True:
        proxy.observer.drain()
        rows = rec.all_calls()
        if len(rows) >= expected or time.time() > deadline:
            return rows
        time.sleep(0.02)


def _call(base, path, rid, tool="send_email", args=None, headers=None):
    body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                       "params": {"name": tool,
                                  "arguments": args if args is not None
                                  else {"to": "ops@acme.com"}}}).encode()
    req = urllib.request.Request(base + path, data=body, method="POST",
                                 headers=headers or {"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=10)


# -- tests -----------------------------------------------------------------

def test_records_a_call_over_plain_json(stack):
    base, _, rec, proxy = stack
    resp = _call(base, "/mcp", 1)
    assert json.loads(resp.read().decode())["result"] == {"ok": True}

    rows = _recorded(rec, proxy, 1)
    assert len(rows) == 1
    assert rows[0]["tool"] == "send_email"
    assert rows[0]["is_error"] == 0
    assert json.loads(rows[0]["signals_json"])["destinations"]["emails"] == ["ops@acme.com"]


def test_records_a_call_delivered_over_sse(stack):
    base, _, rec, proxy = stack
    resp = _call(base, "/sse", 7)
    assert b"data:" in resp.read()

    rows = _recorded(rec, proxy, 1)
    assert len(rows) == 1
    assert rows[0]["result_bytes"] > 0
    assert rows[0]["duration_ms"] is not None


def test_relayed_body_is_byte_identical(stack):
    """The proxy must not rewrite what either side sent."""
    base, up_url, _, _ = stack
    body = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                       "params": {"name": "t", "arguments": {"x": "ünïcode ✓"}}}).encode()

    def post(url):
        req = urllib.request.Request(url + "/mcp", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=10).read()

    assert post(base) == post(up_url)
    assert _Upstream.seen[0][1] == _Upstream.seen[1][1] == body


def test_session_header_survives_both_directions(stack):
    """Mcp-Session-Id is how the handshake works; dropping it breaks the client."""
    base, _, _, _ = stack
    resp = _call(base, "/mcp", 3, headers={"Content-Type": "application/json",
                                           "Mcp-Session-Id": "client-sent"})
    assert resp.headers.get("Mcp-Session-Id") == "sess-abc"
    assert _Upstream.seen[0][0].get("Mcp-Session-Id") == "client-sent"


def test_hop_by_hop_headers_are_not_forwarded(stack):
    base, _, _, _ = stack
    _call(base, "/mcp", 4, headers={"Content-Type": "application/json",
                                    "Connection": "keep-alive"})
    forwarded = {k.lower() for k in _Upstream.seen[0][0]}
    assert "connection" not in forwarded or _Upstream.seen[0][0].get("Connection") != "keep-alive"


def test_batched_calls_over_http_are_all_recorded(stack):
    base, _, rec, proxy = stack
    batch = [{"jsonrpc": "2.0", "id": i, "method": "tools/call",
              "params": {"name": "t", "arguments": {"i": i}}} for i in range(3)]
    req = urllib.request.Request(base + "/mcp", data=json.dumps(batch).encode(),
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()

    assert len(_recorded(rec, proxy, 3)) == 3


def test_secrets_are_redacted_over_http_too(stack):
    base, _, rec, proxy = stack
    # Read the response: a row is only written once the reply correlates, and
    # the reply is not fully relayed until the client consumes it.
    _call(base, "/mcp", 5, args={"api_key": "sk-ant-api03-" + "z" * 40,
                                 "to": "ops@acme.com"}).read()
    stored = _recorded(rec, proxy, 1)[0]["args_json"]
    assert "sk-ant-api03" not in stored
    assert "ops@acme.com" in stored  # the destination survives


def test_server_initiated_sse_stream_relays(stack):
    base, _, _, _ = stack
    resp = urllib.request.urlopen(base + "/mcp", timeout=10)
    assert b"notifications/message" in resp.read()


def test_delete_is_relayed(stack):
    base, _, _, _ = stack
    req = urllib.request.Request(base + "/mcp", method="DELETE")
    assert urllib.request.urlopen(req, timeout=10).status == 204


def test_unreachable_upstream_returns_502_not_a_crash(stack):
    _, _, rec, _ = stack
    dead = HttpProxy("http://127.0.0.1:1", rec, port=0)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dead._handler_class())
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:{}/mcp".format(srv.server_address[1])
    try:
        req = urllib.request.Request(url, data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=10)
        assert exc.value.code == 502
    finally:
        srv.shutdown()
        dead.observer.close()


def test_upstream_error_status_is_preserved(stack):
    """A 4xx from the server must reach the client as a 4xx, not a proxy error."""
    base, _, _, _ = stack
    req = urllib.request.Request(base + "/nope", method="PUT", data=b"{}")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code in (405, 501)


# -- the SSE parser on its own ---------------------------------------------

def test_sink_splits_multi_frame_sse():
    got = []
    sink = MessageSink(got.append, sse=True)
    sink.feed(b'data: {"id":1}\n\ndata: {"id":2}\n')
    sink.feed(b'\ndata: {"id":3}\n\n')
    sink.close()
    assert [m["id"] for m in got] == [1, 2, 3]


def test_sink_handles_multiline_data_fields():
    got = []
    sink = MessageSink(got.append, sse=True)
    sink.feed(b'event: message\ndata: {"id":\ndata: 9}\n\n')
    sink.close()
    assert got == [{"id": 9}]


def test_sink_ignores_unparseable_frames():
    got = []
    sink = MessageSink(got.append, sse=True)
    sink.feed(b"data: not json at all\n\n:heartbeat\n\n")
    sink.close()
    assert got == []


def test_sink_is_bounded_on_a_stream_that_never_completes():
    got = []
    sink = MessageSink(got.append, sse=True)
    for _ in range(80):
        sink.feed(b"x" * 100_000)
    assert len(sink.buf) <= 4 * 1024 * 1024 + 100_000
    assert got == []
