"""Durable storage for observed tool calls.

Design rule, and the one that must never be relaxed: recording is best-effort
and must never affect the proxied stream. Every public method here swallows its
own exceptions. A corrupt database is an acceptable outcome; a broken agent is
not.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    label      TEXT,
    command    TEXT,
    started_at TEXT,
    ended_at   TEXT,
    exit_code  INTEGER
);

CREATE TABLE IF NOT EXISTS calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT,
    label          TEXT,
    ts             TEXT,
    tool           TEXT,
    args_json      TEXT,
    args_bytes     INTEGER,
    args_truncated INTEGER DEFAULT 0,
    signals_json   TEXT,
    redaction_json TEXT,
    duration_ms    REAL,
    is_error       INTEGER DEFAULT 0,
    result_bytes   INTEGER,
    result_preview TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    ts         TEXT,
    kind       TEXT,
    payload    TEXT
);

CREATE INDEX IF NOT EXISTS idx_calls_session ON calls(session_id);
CREATE INDEX IF NOT EXISTS idx_calls_tool    ON calls(tool);
"""

_MAX_EVENT_BYTES = 20_000

# Bump when the shape of the tables changes. Stored in PRAGMA user_version so an
# upgraded Bollard can tell a v1 database from a v2 one instead of failing on a
# missing column and losing a user's history.
SCHEMA_VERSION = 2


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Recorder:
    """Writes call records to SQLite and an append-only JSONL stream."""

    def __init__(self, home: Path, session_id: str, label: str) -> None:
        self.home = Path(home)
        self.session_id = session_id
        self.label = label
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = self.home / "bollard.db"
        self.jsonl_path = self.home / "calls.jsonl"
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        try:
            # WAL lets the reader commands run while a session is recording,
            # and NORMAL stops us paying an fsync per tool call. We already
            # accept that a hard crash may lose the last few records -- losing
            # them is fine, blocking the agent to prevent it is not.
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        self._db.executescript(SCHEMA)
        self._migrate()
        self._db.commit()
        # Held open rather than reopened per call: an open() and a close() on
        # every tool call is most of the recorder's cost for none of its value.
        try:
            self._jsonl = open(self.jsonl_path, "a", encoding="utf-8")
        except Exception:
            self._jsonl = None

    def _migrate(self) -> None:
        """Add columns introduced after v1. Never destructive."""
        try:
            current = self._db.execute("PRAGMA user_version").fetchone()[0]
            if current < 2:
                cols = {r[1] for r in self._db.execute("PRAGMA table_info(calls)")}
                if "redaction_json" not in cols:
                    self._db.execute("ALTER TABLE calls ADD COLUMN redaction_json TEXT")
            self._db.execute("PRAGMA user_version={}".format(SCHEMA_VERSION))
        except Exception:
            pass

    # -- lifecycle ---------------------------------------------------------

    def start_session(self, command: List[str]) -> None:
        try:
            with self._lock:
                self._db.execute(
                    "INSERT OR REPLACE INTO sessions "
                    "(session_id, label, command, started_at) VALUES (?,?,?,?)",
                    (self.session_id, self.label, " ".join(command), utcnow()),
                )
                self._db.commit()
        except Exception:
            pass

    def end_session(self, exit_code: Optional[int]) -> None:
        try:
            with self._lock:
                self._db.execute(
                    "UPDATE sessions SET ended_at=?, exit_code=? WHERE session_id=?",
                    (utcnow(), exit_code, self.session_id),
                )
                self._db.commit()
        except Exception:
            pass

    def close(self) -> None:
        try:
            if self._jsonl is not None:
                self._jsonl.close()
        except Exception:
            pass
        try:
            self._db.close()
        except Exception:
            pass

    # -- writes ------------------------------------------------------------

    def event(self, kind: str, payload: Any) -> None:
        try:
            blob = json.dumps(payload, default=str)[:_MAX_EVENT_BYTES]
            with self._lock:
                self._db.execute(
                    "INSERT INTO events (session_id, ts, kind, payload) VALUES (?,?,?,?)",
                    (self.session_id, utcnow(), kind, blob),
                )
                self._db.commit()
        except Exception:
            pass

    def call(self, rec: Dict[str, Any]) -> None:
        try:
            with self._lock:
                self._db.execute(
                    "INSERT INTO calls (session_id, label, ts, tool, args_json, "
                    "args_bytes, args_truncated, signals_json, redaction_json, "
                    "duration_ms, is_error, result_bytes, result_preview) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        self.session_id,
                        self.label,
                        rec.get("ts"),
                        rec.get("tool"),
                        json.dumps(rec.get("args"), default=str),
                        rec.get("args_bytes", 0),
                        1 if rec.get("args_truncated") else 0,
                        json.dumps(rec.get("signals", {})),
                        json.dumps(rec.get("redaction", {})),
                        rec.get("duration_ms"),
                        1 if rec.get("is_error") else 0,
                        rec.get("result_bytes", 0),
                        rec.get("result_preview"),
                    ),
                )
                self._db.commit()
                if self._jsonl is not None:
                    self._jsonl.write(json.dumps(
                        {"session_id": self.session_id, "label": self.label, **rec},
                        default=str,
                    ) + "\n")
                    self._jsonl.flush()
        except Exception:
            pass

    # -- reads (used by the report commands and by tests) ------------------

    def all_calls(self) -> List[sqlite3.Row]:
        self._db.row_factory = sqlite3.Row
        try:
            return self._db.execute("SELECT * FROM calls ORDER BY id").fetchall()
        except Exception:
            return []
