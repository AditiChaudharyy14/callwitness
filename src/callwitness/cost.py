"""What your tools are spending, in the unit people think in.

Why this exists
---------------
The recorder measures bytes, exactly, because bytes are a fact. Nobody budgets
in bytes. They budget in tokens, and the gap between "get-env returned 9 KB"
and "get-env spent about 2,300 tokens of your context" is the difference
between a number and a decision.

The conversion is an estimate and says so on every run. Tokenisation is
model-specific and this deliberately does not pretend otherwise: four bytes per
token is a reasonable middle for JSON, worse for dense punctuation and
non-ASCII, better for prose. The ratio is printed, and it is a flag, so anyone
who knows their tokeniser can put in the right number rather than argue with a
hidden constant.

What it does not count, and says so
-----------------------------------
Only what tools returned. Not the schemas your client sends at startup, not
your prompts, not the model's own output, and not anything an agent did
through built-in tools that never touched MCP. A cost report that quietly
implies it covers everything would be worse than none, because someone would
compare it to their bill.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_NAME = "callwitness.db"

# A middle figure for JSON. Printed on every run, and overridable, because a
# constant nobody can see is a constant nobody can correct.
BYTES_PER_TOKEN = 4.0

SHOWN = 15
_WINDOW = re.compile(r"^\s*(\d+)\s*([dhw])\s*$", re.I)


def since_moment(window: Optional[str]) -> Optional[datetime]:
    """Turn 7d / 24h / 2w into a UTC cutoff. Anything else means no cutoff."""
    if not window:
        return None
    match = _WINDOW.match(str(window))
    if not match:
        return None
    count, unit = int(match.group(1)), match.group(2).lower()
    hours = {"h": 1, "d": 24, "w": 24 * 7}[unit] * count
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)


def summarise(home: Path, window: Optional[str] = None,
              session: Optional[str] = None) -> Dict[str, Any]:
    """Bytes returned per tool, newest window first."""
    path = Path(home) / DB_NAME
    if not path.is_file():
        return {"tools": [], "calls": 0, "bytes": 0, "window": window}

    where, params = ["is_error = 0"], []
    cutoff = since_moment(window)
    if cutoff is not None:
        where.append("ts >= ?")
        params.append(cutoff.isoformat())
    if session:
        where.append("session_id LIKE ?")
        params.append(str(session) + "%")

    connection = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT tool, COUNT(*) AS calls, "
            "SUM(result_bytes) AS total, MAX(result_bytes) AS largest "
            "FROM calls WHERE " + " AND ".join(where) +
            " GROUP BY tool ORDER BY total DESC", params).fetchall()
        tools = [dict(r) for r in rows]
    except sqlite3.Error:
        tools = []
    finally:
        connection.close()

    return {
        "tools": tools,
        "calls": sum(int(t["calls"] or 0) for t in tools),
        "bytes": sum(int(t["total"] or 0) for t in tools),
        "window": window,
        "session": session,
    }


def tokens(byte_count: int, ratio: float = BYTES_PER_TOKEN) -> int:
    if ratio <= 0:
        ratio = BYTES_PER_TOKEN
    return int(round(byte_count / ratio))


def human(size: int) -> str:
    if size >= 1024 * 1024:
        return "{:.1f} MB".format(size / (1024.0 * 1024.0))
    if size >= 1024:
        return "{:.1f} KB".format(size / 1024.0)
    return "{} B".format(size)


def _count(value: int) -> str:
    return "{:,}".format(value)


def render(summary: Dict[str, Any], ratio: float = BYTES_PER_TOKEN) -> str:
    tools = summary["tools"]
    if not tools:
        return ("\n  Nothing recorded in that window.\n\n"
                "  Try:  callwitness demo\n")

    scope = []
    if summary.get("window"):
        scope.append("last " + str(summary["window"]))
    if summary.get("session"):
        scope.append("session " + str(summary["session"]))
    heading = "  Tool output that entered the context window"
    if scope:
        heading += " (" + ", ".join(scope) + ")"

    lines = ["", heading, ""]
    lines.append("  {:<28} {:>6} {:>10} {:>12} {:>12}".format(
        "tool", "calls", "returned", "~tokens", "worst call"))

    for tool in tools[:SHOWN]:
        total = int(tool["total"] or 0)
        lines.append("  {:<28} {:>6} {:>10} {:>12} {:>12}".format(
            str(tool["tool"])[:28], int(tool["calls"] or 0), human(total),
            "~" + _count(tokens(total, ratio)),
            human(int(tool["largest"] or 0))))

    if len(tools) > SHOWN:
        lines.append("  {:<28} {:>6} {:>10} {:>12}".format(
            "... and {} more tools".format(len(tools) - SHOWN), "", "", ""))

    lines.append("")
    lines.append("  {:<28} {:>6} {:>10} {:>12}".format(
        "total", summary["calls"], human(summary["bytes"]),
        "~" + _count(tokens(summary["bytes"], ratio))))
    lines.append("")
    lines.append("  Tokens are an estimate at {:.0f} bytes per token."
                 " Pass --bytes-per-token".format(ratio))
    lines.append("  to use your own tokeniser's ratio.")
    lines.append("")
    lines.append("  This counts what tools returned, and nothing else: not the")
    lines.append("  schemas your client sends, not your prompts, not the model's")
    lines.append("  replies, and not work an agent did without going through MCP.")
    lines.append("")
    return "\n".join(lines)
