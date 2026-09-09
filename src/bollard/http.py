"""Streamable HTTP transport: the same recorder, in front of a remote server.

Why this exists
---------------
`bollard run` wraps a stdio subprocess, which is the local development shape --
Claude Desktop, Cursor, a filesystem server on a laptop. Production agents
mostly talk to REMOTE MCP servers over Streamable HTTP. Recording only stdio
means recording only developers, and the calls worth watching are the other
ones.

The design rule is unchanged and, with the background observer, stronger than
it was: every byte is relayed before anything looks at it, and observation now
happens on a different thread entirely, so it cannot delay traffic even when it
is slow.

Shape
-----
    MCP client  ->  http://127.0.0.1:PORT/...  ->  https://upstream/mcp

Point the client at the local URL instead of the remote one; everything else is
unchanged. POST carries JSON-RPC messages. The response is either a single JSON
body or an SSE stream carrying several. GET opens a server-initiated SSE stream.
DELETE ends a session. All four are relayed verbatim, headers included, so the
session handshake (`Mcp-Session-Id`) works without us understanding it.

Not covered in v1: the deprecated two-endpoint HTTP+SSE transport, and TLS on
the listener (it binds loopback by default -- put it behind something else if
you move it off localhost).
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, List, Optional, Tuple

from .observer import BackgroundObserver
from .record import Recorder
from .tracker import CallTracker

# Headers that describe one hop and must not be passed to the next one.
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
})

CHUNK = 8192
MAX_OBSERVED_BUFFER = 4 * 1024 * 1024


class MessageSink:
    """Accumulates relayed bytes and emits whole JSON-RPC messages.

    Handles both response shapes: a plain JSON body, and an SSE stream whose
    `data:` lines carry the messages. Deliberately forgiving -- anything it
    cannot parse is discarded rather than raised, because this only ever sees a
    copy of bytes that have already been delivered.
    """

    def __init__(self, emit: Callable[[dict], None], sse: bool) -> None:
        self.emit = emit
        self.sse = sse
        self.buf = b""

    def feed(self, chunk: bytes) -> None:
        try:
            self.buf += chunk
            if len(self.buf) > MAX_OBSERVED_BUFFER:
                # Bounded like the queue, and for the same reason: a stream that
                # never completes must not become unbounded memory.
                self.buf = self.buf[-MAX_OBSERVED_BUFFER:]
            if self.sse:
                self._drain_sse()
        except Exception:
            pass

    def _drain_sse(self) -> None:
        while b"\n\n" in self.buf:
            frame, self.buf = self.buf.split(b"\n\n", 1)
            data = b"".join(
                line.split(b":", 1)[1].strip()
                for line in frame.split(b"\n")
                if line.startswith(b"data:")
            )
            self._emit_json(data)

    def close(self) -> None:
        try:
            if self.sse:
                self._drain_sse()
            elif self.buf:
                self._emit_json(self.buf)
            self.buf = b""
        except Exception:
            pass

    def _emit_json(self, raw: bytes) -> None:
        if not raw:
            return
        try:
            parsed = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return
        for msg in parsed if isinstance(parsed, list) else [parsed]:
            if isinstance(msg, dict):
                try:
                    self.emit(msg)
                except Exception:
                    pass


def _client_headers(handler: BaseHTTPRequestHandler) -> List[Tuple[str, str]]:
    return [(k, v) for k, v in handler.headers.items()
            if k.lower() not in HOP_BY_HOP]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bollard"

    # Injected by HttpProxy.
    upstream: str = ""
    tracker: Optional[CallTracker] = None
    observer: Optional[BackgroundObserver] = None
    echo: bool = False

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.echo:
            sys.stderr.write("[bollard] %s\n" % (fmt % args))

    # -- verbs -------------------------------------------------------------

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length else b""
        # Observe the request only after we have it; forwarding happens next and
        # is not gated on this, because submit() never blocks.
        self._observe(body, sse=False, to_client=False)
        self._relay(body)

    def do_GET(self) -> None:
        self._relay(None)

    def do_DELETE(self) -> None:
        self._relay(None, method="DELETE")

    # -- relay -------------------------------------------------------------

    def _relay(self, body: Optional[bytes], method: Optional[str] = None) -> None:
        url = self.upstream.rstrip("/") + self.path if self.path != "/" else self.upstream
        req = urllib.request.Request(
            url, data=body, method=method or self.command,
            headers=dict(_client_headers(self)),
        )
        try:
            # No timeout: a GET here is a long-lived SSE stream by design.
            upstream = urllib.request.urlopen(req, timeout=None)
        except urllib.error.HTTPError as exc:
            upstream = exc  # an error response is still a response; relay it
        except Exception as exc:
            self.send_error(502, "upstream unreachable: {}".format(exc))
            return

        try:
            self._pump_response(upstream)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    def _pump_response(self, upstream: Any) -> None:
        status = getattr(upstream, "status", None) or upstream.getcode()
        headers = [(k, v) for k, v in upstream.headers.items()
                   if k.lower() not in HOP_BY_HOP]
        ctype = (upstream.headers.get("Content-Type") or "").lower()
        is_sse = "text/event-stream" in ctype
        declared = upstream.headers.get("Content-Length")

        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        if is_sse or declared is None:
            # Unknown length, possibly infinite: frame it ourselves so the
            # connection can stay open and the client sees events as they land.
            self.send_header("Transfer-Encoding", "chunked")
            chunked = True
        else:
            self.send_header("Content-Length", declared)
            chunked = False
        self.end_headers()

        sink = self._sink(sse=is_sse, to_client=True)
        while True:
            chunk = upstream.read(CHUNK)
            if not chunk:
                break
            # Relay first, always. Everything else happens to a copy.
            if chunked:
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
            else:
                self.wfile.write(chunk)
            self.wfile.flush()
            if sink is not None and self.observer is not None:
                self.observer.submit(("feed", sink, chunk))
        if chunked:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        if sink is not None and self.observer is not None:
            self.observer.submit(("close", sink, None))

    # -- observation -------------------------------------------------------

    def _sink(self, sse: bool, to_client: bool) -> Optional[MessageSink]:
        if self.tracker is None or self.observer is None:
            return None
        emit = self.tracker.on_server_message if to_client else self.tracker.on_client_message
        return MessageSink(emit, sse=sse)

    def _observe(self, body: bytes, sse: bool, to_client: bool) -> None:
        if not body:
            return
        sink = self._sink(sse=sse, to_client=to_client)
        if sink is None or self.observer is None:
            return
        self.observer.submit(("feed", sink, body))
        self.observer.submit(("close", sink, None))


def _dispatch(item: Tuple[str, MessageSink, Optional[bytes]]) -> None:
    op, sink, payload = item
    if op == "feed" and payload is not None:
        sink.feed(payload)
    elif op == "close":
        sink.close()


class HttpProxy:
    """Serves a local endpoint that relays to, and records, a remote MCP server."""

    def __init__(self, upstream: str, recorder: Recorder, host: str = "127.0.0.1",
                 port: int = 8100, echo: bool = False, **tracker_kwargs: Any) -> None:
        self.upstream = upstream
        self.rec = recorder
        self.host = host
        self.port = port
        self.echo = echo
        self.tracker = CallTracker(recorder, echo=echo, **tracker_kwargs)
        self.observer = BackgroundObserver(_dispatch)
        self._server: Optional[ThreadingHTTPServer] = None

    def _handler_class(self) -> type:
        return type("_BoundHandler", (_Handler,), {
            "upstream": self.upstream,
            "tracker": self.tracker,
            "observer": self.observer,
            "echo": self.echo,
        })

    def serve_forever(self) -> int:
        self.rec.start_session(["http", self.upstream])
        self._server = ThreadingHTTPServer((self.host, self.port), self._handler_class())
        self._server.daemon_threads = True
        bound = self._server.server_address
        print("[bollard] recording {} -> {}".format(
            "http://{}:{}".format(bound[0], bound[1]), self.upstream),
            file=sys.stderr, flush=True)
        code = 0
        try:
            self._server.serve_forever()
        except KeyboardInterrupt:
            pass
        except Exception:
            code = 1
        finally:
            self.shutdown()
        return code

    def shutdown(self) -> None:
        try:
            if self._server is not None:
                threading.Thread(target=self._server.shutdown, daemon=True).start()
                self._server.server_close()
        except Exception:
            pass
        try:
            self.observer.close()
            stats = self.observer.stats
            if stats["dropped"] and self.echo:
                print("[bollard] dropped {} observations under load".format(
                    stats["dropped"]), file=sys.stderr, flush=True)
            self.rec.event("observer:stats", stats)
        except Exception:
            pass
        try:
            self.rec.end_session(0)
            self.rec.close()
        except Exception:
            pass
