"""Transport-independent observation of a JSON-RPC conversation.

Extracted from the stdio proxy so the HTTP transport records identically. Any
transport that can hand this class parsed client and server messages gets the
same rows, the same signals and the same redaction -- there is deliberately no
second implementation of "what a call looks like" to drift out of step.

These methods are pure with respect to the stream: they read a parsed message
and write to the recorder. They never mutate or drop anything, which is what
makes them testable without a socket or a subprocess.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, Optional

from .analyze import extract_signals, shape_only
from .record import Recorder, utcnow
from .redact import redact_structure

MAX_ARG_BYTES_DEFAULT = 8192
RESULT_PREVIEW_BYTES = 512
PENDING_TTL_DEFAULT = 900.0


class CallTracker:
    """Correlates tools/call requests with their responses and records them."""

    def __init__(
        self,
        recorder: Recorder,
        max_arg_bytes: int = MAX_ARG_BYTES_DEFAULT,
        no_args: bool = False,
        echo: bool = False,
        redact: bool = True,
        pending_ttl: float = PENDING_TTL_DEFAULT,
    ) -> None:
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

    # -- observation -------------------------------------------------------

    def on_client_message(self, msg: Dict[str, Any]) -> None:
        method = msg.get("method")

        if method in ("initialize", "tools/list"):
            try:
                self.rec.event("client:{}".format(method), msg.get("params", {}))
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

        self._echo_request(entry, args_bytes, tool)

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

        self._echo_response(entry)

    # -- echo (overridden by transports that write elsewhere) --------------

    def _echo_request(self, entry: Dict[str, Any], args_bytes: int, tool: str) -> None:
        if not self.echo:
            return
        import sys
        dest_map = entry["signals"].get("destinations", {})
        dest = ", ".join(dest_map.get("hosts", []) + dest_map.get("emails", [])) or "-"
        print("[bollard] -> {} ({}B) dest={}".format(tool, args_bytes, dest),
              file=sys.stderr, flush=True)

    def _echo_response(self, entry: Dict[str, Any]) -> None:
        if not self.echo:
            return
        import sys
        flag = "ERR" if entry["is_error"] else "ok"
        duration: Optional[float] = entry.get("duration_ms")
        shown = "{:.0f}ms".format(duration) if duration is not None else "-"
        print("[bollard] <- {} {} {}B {}".format(
            entry["tool"], flag, entry["result_bytes"], shown),
            file=sys.stderr, flush=True)
