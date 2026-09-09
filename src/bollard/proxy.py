"""The proxy itself: a transparent relay between an MCP client and server.

Correctness rule
----------------
Every byte that arrives is forwarded, in order, whether or not it parses.
Observation happens on a copy, inside a try. A bug in the recorder must never
corrupt the protocol stream — that is what makes this safe to put in front of
production traffic, which is the only reason anyone will let us watch.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

from .analyze import extract_signals, shape_only
from .record import Recorder, utcnow
from .redact import redact_structure

MAX_ARG_BYTES_DEFAULT = 8192
RESULT_PREVIEW_BYTES = 512


class Proxy:
    """Relays newline-delimited JSON-RPC between an MCP client and server."""

    def __init__(
        self,
        command: List[str],
        recorder: Recorder,
        max_arg_bytes: int = MAX_ARG_BYTES_DEFAULT,
        no_args: bool = False,
        echo: bool = False,
        redact: bool = True,
        pending_ttl: float = 900.0,
    ) -> None:
        self.command = command
        self.rec = recorder
        self.max_arg_bytes = max_arg_bytes
        self.no_args = no_args
        self.echo = echo
        # Redaction defaults ON. Full-fidelity capture is the deliberate
        # opt-out, not the accident.
        self.redact = redact
        self.pending_ttl = pending_ttl
        self.pending: Dict[str, Dict[str, Any]] = {}
        self.pending_lock = threading.Lock()
        self.proc: Optional[subprocess.Popen] = None

    # -- observation -------------------------------------------------------
    # These two are pure with respect to the stream: they read a parsed message
    # and write to the recorder. They never mutate or drop anything, which is
    # what makes them directly testable without spawning a process.

    def on_client_message(self, msg: Dict[str, Any]) -> None:
        method = msg.get("method")

        if method in ("initialize", "tools/list"):
            try:
                self.rec.event(f"client:{method}", msg.get("params", {}))
            except Exception:
                pass
            return
        if method != "tools/call":
            return

        request_id = msg.get("id")
        if request_id is None:
            return  # a notification: no response will correlate to it

        params = msg.get("params") or {}
        tool = params.get("name", "<unknown>")
        args = params.get("arguments", {})

        raw = json.dumps(args, ensure_ascii=False, default=str)
        args_bytes = len(raw.encode("utf-8"))

        # Redact before anything reaches storage, and note the true size first.
        # Tool arguments routinely carry API keys, bearer tokens and connection
        # strings; without this every install is a plaintext credential store
        # that did not exist before we were installed. Scrubbing on read would
        # not help -- the plaintext would already be on disk.
        redaction: Dict[str, Any] = {}
        if self.redact:
            args_for_storage, rstats = redact_structure(args)
            if rstats.total:
                redaction = rstats.as_dict()
        else:
            args_for_storage = args

        raw_stored = json.dumps(args_for_storage, ensure_ascii=False, default=str)

        truncated = False
        if self.no_args:
            stored: Any = shape_only(args)
        elif len(raw_stored.encode("utf-8")) > self.max_arg_bytes:
            stored = {"_truncated": raw_stored[: self.max_arg_bytes]}
            truncated = True
        else:
            stored = args_for_storage

        entry = {
            "redaction": redaction,
            "ts": utcnow(),
            "_t0": time.perf_counter(),
            "tool": tool,
            "args": stored,
            "args_bytes": args_bytes,
            "args_truncated": truncated,
            "signals": extract_signals(args),
        }
        with self.pending_lock:
            self.pending[str(request_id)] = entry
            self._reap_locked()

        if self.echo:
            dest_map = entry["signals"].get("destinations", {})
            dest = ", ".join(dest_map.get("hosts", []) + dest_map.get("emails", [])) or "-"
            print(f"[bollard] -> {tool} ({args_bytes}B) dest={dest}",
                  file=sys.stderr, flush=True)

    def _reap_locked(self) -> None:
        """Drop requests that never got a response. Caller holds pending_lock.

        A server that dies mid-call, or a client that gives up, leaves an entry
        behind forever. Over a long-lived session that is an unbounded map --
        a slow leak in a process meant to sit beside production traffic for
        weeks. Reaped calls are still recorded, flagged, so a disappearing
        server shows up in the data instead of vanishing from it.
        """
        if len(self.pending) <= 64:
            return
        cutoff = time.perf_counter() - self.pending_ttl
        stale = [k for k, e in self.pending.items() if e.get("_t0", 0) < cutoff]
        for key in stale:
            entry = self.pending.pop(key, None)
            if entry is None:
                continue
            entry.pop("_t0", None)
            entry.update({"duration_ms": None, "is_error": True,
                          "result_bytes": 0, "result_preview": "<no_response>"})
            try:
                self.rec.call(entry)
            except Exception:
                pass

    def on_server_message(self, msg: Dict[str, Any]) -> None:
        request_id = msg.get("id")
        if request_id is None:
            return

        with self.pending_lock:
            entry = self.pending.pop(str(request_id), None)
        if entry is None:
            return  # a response to something that wasn't a tool call

        duration_ms = (time.perf_counter() - entry.pop("_t0")) * 1000.0

        error = msg.get("error")
        result = msg.get("result")
        is_error = error is not None
        if not is_error and isinstance(result, dict) and result.get("isError"):
            is_error = True

        payload = error if is_error else result
        raw = json.dumps(payload, ensure_ascii=False, default=str) if payload is not None else ""

        entry.update({
            "duration_ms": round(duration_ms, 2),
            "is_error": is_error,
            "result_bytes": len(raw.encode("utf-8")),
            "result_preview": raw[:RESULT_PREVIEW_BYTES],
        })
        try:
            self.rec.call(entry)
        except Exception:
            pass  # observation must never reach the stream (see design rule)

        if self.echo:
            flag = "ERR" if is_error else "ok"
            print(f"[bollard] <- {entry['tool']} {flag} "
                  f"{entry['result_bytes']}B {entry['duration_ms']:.0f}ms",
                  file=sys.stderr, flush=True)

    # -- transport ---------------------------------------------------------

    def _pump(self, src, dst, handler) -> None:
        """Forward every line, then observe a copy of it. Order matters."""
        try:
            for line in iter(src.readline, b""):
                try:
                    dst.write(line)
                    dst.flush()
                except (BrokenPipeError, ValueError, OSError):
                    break
                try:
                    text = line.decode("utf-8", "replace").strip()
                    if text.startswith("{"):
                        handler(json.loads(text))
                    elif text.startswith("["):
                        # JSON-RPC permits a batch: one line, many messages.
                        # Gating on '{' alone silently recorded nothing for
                        # these, so a client that batches looked idle rather
                        # than busy -- an undercount that grows with exactly
                        # the clients making the most calls.
                        for item in json.loads(text):
                            if isinstance(item, dict):
                                try:
                                    handler(item)
                                except Exception:
                                    pass
                except Exception:
                    pass  # never let observation break the relay
        except Exception:
            pass
        finally:
            try:
                dst.close()
            except Exception:
                pass

    @staticmethod
    def _pump_stderr(src) -> None:
        try:
            for line in iter(src.readline, b""):
                sys.stderr.buffer.write(line)
                sys.stderr.buffer.flush()
        except Exception:
            pass

    def run(self) -> int:
        self.rec.start_session(self.command)
        try:
            self.proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except (FileNotFoundError, PermissionError) as exc:
            print(f"bollard: cannot start server {self.command[0]!r}: {exc}",
                  file=sys.stderr)
            self.rec.end_session(127)
            self.rec.close()
            return 127

        threads = [
            threading.Thread(target=self._pump, daemon=True,
                             args=(sys.stdin.buffer, self.proc.stdin,
                                   self.on_client_message)),
            threading.Thread(target=self._pump, daemon=True,
                             args=(self.proc.stdout, sys.stdout.buffer,
                                   self.on_server_message)),
            threading.Thread(target=self._pump_stderr, daemon=True,
                             args=(self.proc.stderr,)),
        ]
        for t in threads:
            t.start()

        code: Optional[int] = None
        try:
            code = self.proc.wait()
        except KeyboardInterrupt:
            try:
                self.proc.terminate()
                code = self.proc.wait(timeout=5)
            except Exception:
                pass
        for t in threads:
            t.join(timeout=1.0)

        self.rec.end_session(code)
        self.rec.close()
        return code or 0
