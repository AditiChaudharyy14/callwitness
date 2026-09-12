"""The stdio proxy: a transparent relay between an MCP client and server.

Correctness rule
----------------
Every byte that arrives is forwarded, in order, whether or not it parses. A bug
in the recorder must never corrupt the protocol stream -- that is what makes
this safe to put in front of production traffic, which is the only reason
anyone will let us watch.

Liveness rule (new)
-------------------
Observation also must never *delay* the stream. Forwarding used to be followed
by parsing on the same thread, so a slow parse held up the next message; a ReDoS
in the email pattern once turned that into seconds. Parsing now happens on a
background thread and the relay only ever does a non-blocking hand-off.

The observation logic itself lives in CallTracker, shared with the HTTP
transport so both record identically.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from typing import Any, Callable, Dict, List, Optional

from .observer import BackgroundObserver
from .record import Recorder
from .tracker import MAX_ARG_BYTES_DEFAULT, RESULT_PREVIEW_BYTES, CallTracker

__all__ = ["Proxy", "MAX_ARG_BYTES_DEFAULT", "RESULT_PREVIEW_BYTES"]


def resolve_program(command: List[str]) -> List[str]:
    """Look the program name up on PATH the way a shell would, before spawning.

    Popen does not do this itself on Windows. `npx` on PATH is really
    `npx.cmd`, and CreateProcess appends only the extensions the loader knows
    about -- not PATHEXT -- so Popen(["npx", ...]) raises WinError 2 for the
    very command that works at the prompt one line above.

    This is not a Windows nicety. Most MCP servers published today are npm
    packages launched with npx, and most of the desktops running MCP clients
    are Windows. Without this the proxy cannot wrap the majority of real
    servers on the majority of machines, which is the entire product.

    shutil.which applies the platform's own rules, including PATHEXT, and is a
    no-op for a path that is already absolute. When it finds nothing the
    original name is passed through untouched, so the error the user reads
    still names the command they actually typed rather than a resolved path
    they never wrote.
    """
    if not command:
        return list(command)
    found = shutil.which(command[0])
    if found is None:
        return list(command)
    return [found] + list(command[1:])



class Proxy(CallTracker):
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
        super().__init__(recorder, max_arg_bytes=max_arg_bytes, no_args=no_args,
                         echo=echo, redact=redact, pending_ttl=pending_ttl)
        self.command = command
        self.proc: Optional[subprocess.Popen] = None
        self.observer = BackgroundObserver(_dispatch)

    # -- transport ---------------------------------------------------------

    def _pump(self, src, dst, handler: Callable[[Dict[str, Any]], None]) -> None:
        """Forward every line, then hand a copy to the observer. Order matters."""
        try:
            for line in iter(src.readline, b""):
                try:
                    dst.write(line)
                    dst.flush()
                except (BrokenPipeError, ValueError, OSError):
                    break
                try:
                    self.observer.submit((handler, line))
                except Exception:
                    pass  # never let observation break the relay
        except Exception:
            pass
        finally:
            # Make "the stream ended" and "its observations are recorded" the
            # same moment. Off the critical path while bytes move; settled by
            # the time the pump returns.
            try:
                self.observer.drain()
            except Exception:
                pass
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
                resolve_program(self.command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except (FileNotFoundError, PermissionError) as exc:
            print("callwitness: cannot start server {!r}: {}".format(self.command[0], exc),
                  file=sys.stderr)
            self.rec.end_session(127)
            self.observer.close()
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

        self.observer.close()
        stats = self.observer.stats
        try:
            if stats["dropped"]:
                self.rec.event("observer:stats", stats)
        except Exception:
            pass
        self.rec.end_session(code)
        self.rec.close()
        return code or 0


def _dispatch(item) -> None:
    """Parse one relayed line and hand its messages to the tracker.

    Runs on the observer thread. Anything that throws here is contained by the
    observer and cannot reach the relay.
    """
    handler, line = item
    text = line.decode("utf-8", "replace").strip()
    if text.startswith("{"):
        handler(json.loads(text))
    elif text.startswith("["):
        # JSON-RPC permits a batch: one line, many messages. Gating on '{'
        # alone silently recorded nothing for these, so a client that batches
        # looked idle rather than busy -- an undercount that grew with exactly
        # the clients making the most calls.
        for member in json.loads(text):
            if isinstance(member, dict):
                try:
                    handler(member)
                except Exception:
                    pass
