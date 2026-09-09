"""A bounded, non-blocking queue between the relay and the recorder.

Why this exists
---------------
The original design rule was "forward first, parse second". That guaranteed
correctness -- a recorder bug could not corrupt the stream -- but not liveness:
parsing happened on the forwarding thread, so a slow observation delayed the
NEXT message. A ReDoS in the email pattern turned that into a 4.2 second stall
on a 30KB payload. The regex is fixed, but the shape of the bug was structural,
and the fix for the shape is this file.

The rule is now stronger: observation cannot delay traffic at all, because the
relay thread only ever does a non-blocking put.

The queue is bounded on purpose. An unbounded one converts a slow recorder into
unbounded memory growth, which is a worse failure than losing observations. So
when it is full we drop, and we COUNT the drops -- data loss that nobody can see
is indistinguishable from data that never existed, and this tool's entire claim
is that it writes down what actually happened.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Callable, Dict

DEFAULT_MAXSIZE = 2048
_SENTINEL = object()


class BackgroundObserver:
    """Runs `handler` on submitted items, on its own thread, never blocking."""

    def __init__(self, handler: Callable[[Any], None],
                 maxsize: int = DEFAULT_MAXSIZE, name: str = "bollard-observer") -> None:
        self._handler = handler
        self._q: "queue.Queue" = queue.Queue(maxsize=maxsize)
        self._dropped = 0
        self._handled = 0
        self._errors = 0
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._closed = False
        self._thread.start()

    def submit(self, item: Any) -> bool:
        """Hand an item over. Returns False if it was dropped. Never blocks."""
        if self._closed:
            return False
        try:
            self._q.put_nowait(item)
            return True
        except queue.Full:
            with self._lock:
                self._dropped += 1
            return False
        except Exception:
            return False

    def drain(self, timeout: float = 5.0) -> bool:
        """Block until everything submitted so far has been handled.

        Used at stream boundaries, never during one. Observation stays off the
        relay's critical path while bytes are moving; this just makes "the
        stream ended" and "its observations are recorded" the same moment, so
        callers and tests do not have to poll for it.
        """
        if self._closed or not self._thread.is_alive():
            return True
        done = threading.Event()
        try:
            self._q.put_nowait(("__drain__", done))
        except Exception:
            return False
        return done.wait(timeout)

    def _run(self) -> None:
        while True:
            try:
                item = self._q.get()
            except Exception:
                return
            if item is _SENTINEL:
                return
            if isinstance(item, tuple) and len(item) == 2 and item[0] == "__drain__":
                try:
                    item[1].set()
                except Exception:
                    pass
                continue
            try:
                self._handler(item)
                with self._lock:
                    self._handled += 1
            except Exception:
                # A handler bug is contained here. It cannot reach the relay,
                # because the relay is not on this thread.
                with self._lock:
                    self._errors += 1

    def close(self, timeout: float = 2.0) -> None:
        """Stop accepting work and drain what is queued, within `timeout`."""
        if self._closed:
            return
        self._closed = True
        try:
            self._q.put_nowait(_SENTINEL)
        except Exception:
            pass
        try:
            self._thread.join(timeout=timeout)
        except Exception:
            pass

    @property
    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {"handled": self._handled, "dropped": self._dropped,
                    "errors": self._errors, "queued": self._q.qsize()}
