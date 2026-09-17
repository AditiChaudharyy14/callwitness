"""What happened the last time an agent ran.

Why this exists
---------------
Everything needed to answer "what did my agent just do, and what went wrong"
has been in the database since the beginning: every call carries a session_id,
and every wrapped process is a row in `sessions` with a start, an end and an
exit code. None of it was ever shown. `stats` flattens months of runs into one
table sorted by call count, which answers which tools exist -- a question
nobody has -- and buries the two that people do have: what failed, and what
was enormous.

So this reads one run and answers those. It adds no columns and records
nothing new; it is a query that should have existed already.

What it leads with
------------------
Failures first, then the biggest responses, then the slowest. That order is
not cosmetic: a person runs this because something went wrong or something was
slow, and a report that opens with a tidy summary of everything that went fine
makes them scroll. If nothing failed it says so in one line and moves on.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_NAME = "callwitness.db"
SHOWN = 5

# After this long with no activity, an unclosed session is not running.
#
# The recorder already closes sessions properly -- in a finally, on spawn
# failure and on normal exit -- so a row with no end means the process was
# killed outright, which cannot be caught on any platform. That residue is
# permanent and unavoidable. What is avoidable is reporting a process that
# died four days ago as still running, which is the tool stating something
# false about its own records.
STALE_AFTER = 3600.0


def _connect(home: Path) -> Optional[sqlite3.Connection]:
    """Read-only. This command must never be able to damage a recording."""
    path = Path(home) / DB_NAME
    if not path.is_file():
        return None
    connection = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _moment(stamp: Optional[str]) -> Optional[datetime]:
    """Parse a recorded timestamp to naive UTC.

    The store holds UTC, but not uniformly: some rows carry an offset and some
    do not, depending on which version wrote them. Mixing the two raises
    "can't subtract offset-naive and offset-aware datetimes" the moment you do
    arithmetic, so everything is flattened to naive UTC on the way in and
    compared against utcnow, never now.
    """
    if not stamp:
        return None
    text = str(stamp).strip()
    if text.endswith("Z"):
        text = text[:-1]
    try:
        when = datetime.fromisoformat(text)
    except Exception:
        return None
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    return when


def _clock(stamp: Optional[str]) -> str:
    when = _moment(stamp)
    return when.strftime("%d %b %H:%M") if when else "?"


def _elapsed(started: Optional[str], ended: Optional[str],
             last_seen: Optional[str] = None) -> str:
    """How long it ran, or an honest word about why that isn't known.

    `last_seen` is the newest call in the session. With no recorded end, it is
    the only evidence of whether anything is still happening: a session whose
    last call was days ago is finished, whatever the absent end says.
    """
    first, last = _moment(started), _moment(ended)
    if not first:
        return "?"
    if not last:
        latest = _moment(last_seen) or first
        idle = (datetime.utcnow() - latest).total_seconds()
        return "still running" if idle < STALE_AFTER else "no end recorded"
    seconds = max(0.0, (last - first).total_seconds())
    if seconds >= 3600:
        return "{:.0f}h {:.0f}m".format(seconds // 3600, (seconds % 3600) // 60)
    if seconds >= 60:
        return "{:.0f}m {:.0f}s".format(seconds // 60, seconds % 60)
    return "{:.1f}s".format(seconds)


def human(size: int) -> str:
    if size >= 1024 * 1024:
        return "{:.1f} MB".format(size / (1024.0 * 1024.0))
    if size >= 1024:
        return "{:.1f} KB".format(size / 1024.0)
    return "{} B".format(size)


def _ms(value: Optional[float]) -> str:
    value = float(value or 0)
    if value >= 1000:
        return "{:.1f} s".format(value / 1000.0)
    return "{:.0f} ms".format(value)


def sessions(home: Path, limit: int = 10) -> List[Dict[str, Any]]:
    """Recent runs, newest first."""
    connection = _connect(home)
    if connection is None:
        return []
    try:
        rows = connection.execute(
            "SELECT s.session_id, s.label, s.command, s.started_at, s.ended_at, "
            "s.exit_code, (SELECT MAX(ts) FROM calls c "
            "WHERE c.session_id = s.session_id) AS last_call "
            "FROM sessions s ORDER BY s.started_at DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        connection.close()


def summarise(home: Path, session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """One run, reduced to the things a person acts on."""
    connection = _connect(home)
    if connection is None:
        return None
    try:
        if session_id:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        else:
            # The newest session that actually recorded a call. A run that
            # started and made none is not what someone means by "last".
            row = connection.execute(
                "SELECT s.* FROM sessions s WHERE EXISTS "
                "(SELECT 1 FROM calls c WHERE c.session_id = s.session_id) "
                "ORDER BY s.started_at DESC LIMIT 1").fetchone()
        if row is None:
            return None

        session = dict(row)
        calls = [dict(r) for r in connection.execute(
            "SELECT ts, tool, args_bytes, duration_ms, is_error, result_bytes, "
            "result_preview, signals_json FROM calls WHERE session_id = ? "
            "ORDER BY ts", (session["session_id"],)).fetchall()]
    except sqlite3.Error:
        return None
    finally:
        connection.close()

    failures: Dict[str, Dict[str, Any]] = {}
    destinations: Dict[str, int] = {}
    for call in calls:
        if call.get("is_error"):
            seen = failures.setdefault(
                call["tool"], {"tool": call["tool"], "count": 0, "last": None,
                               "why": None})
            seen["count"] += 1
            seen["last"] = call["ts"]
            if not seen["why"]:
                seen["why"] = _why(call.get("result_preview"))
        for host in _destinations(call.get("signals_json")):
            destinations[host] = destinations.get(host, 0) + 1

    done = [c for c in calls if not c.get("is_error")]
    biggest = sorted(done, key=lambda c: -int(c.get("result_bytes") or 0))[:SHOWN]
    slowest = sorted(done, key=lambda c: -float(c.get("duration_ms") or 0))[:SHOWN]
    return {
        "session": session,
        "last_call": calls[-1]["ts"] if calls else None,
        "calls": len(calls),
        "failed": sum(1 for c in calls if c.get("is_error")),
        "returned": sum(int(c.get("result_bytes") or 0) for c in calls),
        "spent_ms": sum(float(c.get("duration_ms") or 0) for c in calls),
        "failures": sorted(failures.values(), key=lambda f: -f["count"]),
        "biggest": biggest,
        # Only what the biggest list did not already say. On a small run the
        # same two calls top both, and printing them twice makes the report
        # look padded -- which is how a reader decides nothing here is worth
        # reading.
        "slowest": [c for c in slowest if id(c) not in {id(b) for b in biggest}],
        "destinations": sorted(destinations.items(), key=lambda kv: -kv[1])[:SHOWN],
    }


def _why(preview: Optional[str]) -> Optional[str]:
    """A short reason out of an error preview, if there is one in there."""
    if not preview:
        return None
    text = str(preview)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            message = parsed.get("message") or (parsed.get("error") or {}).get("message")
            if message:
                text = str(message)
    except Exception:
        pass
    text = " ".join(text.split())
    return text[:58] + "..." if len(text) > 58 else text


def _destinations(blob: Optional[str]) -> List[str]:
    if not blob:
        return []
    try:
        signals = json.loads(blob)
    except Exception:
        return []
    found = (signals or {}).get("destinations") or {}
    out: List[str] = []
    for key in ("hosts", "emails"):
        values = found.get(key)
        if isinstance(values, list):
            out.extend(str(v) for v in values)
    return out


def render(summary: Optional[Dict[str, Any]], home: Path) -> str:
    if summary is None:
        return ("\n  Nothing recorded yet.\n\n"
                "  Try it with no agent at all:  callwitness demo\n"
                "  Or wrap the servers you have: callwitness install\n")

    session = summary["session"]
    lines: List[str] = [""]

    # The date once, then clock times. Two identical dates on one line is
    # noise in the place a reader looks first.
    finished = _moment(session.get("ended_at"))
    started = _moment(session.get("started_at"))
    until = (finished.strftime("%H:%M") if finished and started
             and finished.date() == started.date()
             else (_clock(session.get("ended_at")) if finished else "now"))
    ran_for = _elapsed(session.get("started_at"), session.get("ended_at"),
                       summary.get("last_call"))
    lines.append("  {}   {} -> {}   {}".format(
        session.get("label") or "unlabelled",
        _clock(session.get("started_at")), until, ran_for))

    exit_code = session.get("exit_code")
    ended = ("exited {}".format(exit_code) if exit_code not in (None, 0)
             else ("clean" if exit_code == 0 else ran_for))
    lines.append("  {} calls, {} failed, {} returned, {} in tools, {}".format(
        summary["calls"], summary["failed"], human(summary["returned"]),
        _ms(summary["spent_ms"]), ended))
    lines.append("")

    if summary["failures"]:
        lines.append("  failed")
        for failure in summary["failures"]:
            reason = "  {}".format(failure["why"]) if failure["why"] else ""
            lines.append("    {:<28} {:>3}x  {}{}".format(
                failure["tool"][:28], failure["count"],
                _clock(failure["last"]), reason))
        lines.append("")
    else:
        lines.append("  nothing failed")
        lines.append("")

    if summary["biggest"]:
        lines.append("  biggest responses")
        for call in summary["biggest"]:
            lines.append("    {:<28} {:>9}  {:>8}  {}".format(
                call["tool"][:28], human(int(call.get("result_bytes") or 0)),
                _ms(call.get("duration_ms")), _clock(call.get("ts"))))
        lines.append("")

    if summary["slowest"]:
        lines.append("  slowest")
        for call in summary["slowest"]:
            lines.append("    {:<28} {:>9}  {:>8}  {}".format(
                call["tool"][:28], human(int(call.get("result_bytes") or 0)),
                _ms(call.get("duration_ms")), _clock(call.get("ts"))))
        lines.append("")

    if summary["destinations"]:
        lines.append("  where it went")
        for host, count in summary["destinations"]:
            lines.append("    {:<28} {:>3}".format(str(host)[:28], count))
        lines.append("")

    lines.append("  Every call:  callwitness tail --session {}".format(
        str(session.get("session_id"))[:8]))
    lines.append("")
    return "\n".join(lines)


def render_sessions(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "\n  No runs recorded yet.\n"
    lines = ["", "  recent runs", ""]
    for row in rows:
        lines.append("    {:<10} {:<14} {:<14} {}".format(
            str(row.get("session_id"))[:8],
            (row.get("label") or "unlabelled")[:14],
            _clock(row.get("started_at")),
            _elapsed(row.get("started_at"), row.get("ended_at"),
                     row.get("last_call"))))
    lines.append("")
    return "\n".join(lines)
