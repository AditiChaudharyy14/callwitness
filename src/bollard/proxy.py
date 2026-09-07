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
    ) -> None:
        self.command = command
        self.rec = recorder
        self.max_arg_bytes = max_arg_bytes
        self.no_args = no_args
        self.echo = echo
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

        truncated = False
        if self.no_args:
            stored: Any = shape_only(args)
        elif args_bytes > self.max_arg_bytes:
            stored = {"_truncated": raw[: self.max_arg_bytes]}
            truncated = True
        else:
            stored = args

        entry = {
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

        if self.echo:
            dest_map = entry["signals"].get("destinations", {})
            dest = ", ".join(dest_map.get("hosts", []) + dest_map.get("emails", [])) or "-"
            print(f"[bollard] -> {tool} ({args_bytes}B) dest={dest}",
                  file=sys.stderr, flush=True)

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
