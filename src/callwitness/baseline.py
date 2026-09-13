"""Emit a baseline document from what this machine actually recorded.

`census/baseline.py` turns the published census into callwitness.baseline.v1.
This turns a local recording into the same document, so a tool that consumes
the public baseline can consume yours without a second reader.

That is the whole point. A benchmark that hardcodes someone else's percentiles
is calibrated against someone else's servers. The same benchmark pointed at a
baseline generated from your own traffic is calibrated against yours, and the
only thing that had to change was a path.

Naming follows contribute.py exactly: public packages are named, everything
else becomes "unlisted" with pseudonymous tool names. So the output of this
command is safe to hand to someone without reading it line by line first --
which matters, because the reason to generate it is to give it to a tool.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .contribute import anonymise_tools, package_of, percentile

SCHEMA = "callwitness.baseline.v1"
PUBLIC = "https://callwitness.tech/baseline/v1.json"


def spread(values: List[int]) -> Dict[str, int]:
    """Same five numbers the published baseline reports, n included.

    contribute.spread deliberately omits n -- a contribution's call count lives
    elsewhere in that payload. Here n travels inside every distribution,
    because a consumer reading one server's percentile needs to know it came
    from three calls without looking anywhere else.
    """
    if not values:
        return {"n": 0, "min": 0, "p50": 0, "p95": 0, "max": 0}
    return {"n": len(values), "min": int(min(values)),
            "p50": percentile(values, 0.50), "p95": percentile(values, 0.95),
            "max": int(max(values))}



def _declared_by_package(home: Path) -> Dict[str, int]:
    """Largest tools/list this machine saw from each package.

    Largest, not latest: a server that answered once with a partial listing and
    once in full declared the full one, and the ratio should be measured
    against what it can put in front of a model, not against its smallest day.
    """
    db = Path(home) / "callwitness.db"
    if not db.is_file():
        return {}
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT s.command AS command, e.payload AS payload "
            "FROM events e JOIN sessions s ON s.session_id = e.session_id "
            "WHERE e.kind = 'server:tools/list'").fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    found: Dict[str, int] = {}
    for row in rows:
        try:
            declared = int(json.loads(row["payload"]).get("declared_bytes") or 0)
        except Exception:
            continue
        package = package_of(row["command"] or "")
        if declared > found.get(package, 0):
            found[package] = declared
    return found


def _rows(home: Path, since: Optional[str]) -> List[sqlite3.Row]:
    db = Path(home) / "callwitness.db"
    if not db.is_file():
        return []
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        query = (
            "SELECT c.tool, c.args_bytes, c.result_bytes, c.duration_ms, "
            "       c.is_error, c.ts, s.command AS command "
            "FROM calls c LEFT JOIN sessions s ON s.session_id = c.session_id"
        )
        params: tuple = ()
        if since:
            query += " WHERE c.ts > ?"
            params = (since,)
        return conn.execute(query + " ORDER BY c.ts", params).fetchall()
    except Exception:
        return []
    finally:
        conn.close()


def build(home: Path, since: Optional[str] = None) -> Dict[str, Any]:
    rows = _rows(home, since)
    declared_sizes = _declared_by_package(home)

    grouped: Dict[str, List[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(package_of(row["command"] or ""), []).append(row)

    servers = []
    every_returned: List[int] = []
    every_argument: List[int] = []

    for package in sorted(grouped):
        calls = [c for c in grouped[package] if not c["is_error"]]
        public = package != "unlisted"
        pseudonym = {} if public else anonymise_tools(
            [c["tool"] or "?" for c in calls])

        declared = int(declared_sizes.get(package, 0))
        returned = [int(c["result_bytes"] or 0) for c in calls]
        arguments = [int(c["args_bytes"] or 0) for c in calls]
        every_returned.extend(returned)
        every_argument.extend(arguments)

        servers.append({
            "server": package,
            "package": package,
            # Measured from the tools/list this machine actually saw. Still 0
            # when no listing was recorded -- a server wrapped before v0.2.2,
            # or a client that never asked -- and 0 means unknown, never zero.
            "declared_bytes": declared,
            "tool_count": len({c["tool"] for c in calls}),
            "returned_bytes": spread(returned),
            "argument_bytes": spread(arguments),
            "calls": [
                {"tool": (c["tool"] if public else pseudonym.get(c["tool"] or "?")),
                 "arguments_bytes": int(c["args_bytes"] or 0),
                 "returned_bytes": int(c["result_bytes"] or 0),
                 "ms": int(c["duration_ms"] or 0)}
                for c in calls
            ],
        })
        # The ratio only exists when both halves were measured. Computed here
        # rather than left to each consumer, so the finding means the same
        # thing wherever it is read.
        if declared and returned:
            servers[-1]["returned_over_declared"] = {
                "p50": round(percentile(returned, 0.50) / declared, 2),
                "max": round(max(returned) / declared, 2),
            }

    stamps = [r["ts"] for r in rows if r["ts"]]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "schema": SCHEMA,
        # Same schema as the published baseline, different provenance. A
        # consumer must be able to tell which it is holding without guessing
        # from the numbers.
        "origin": "local",
        "generated_at": now,
        "generated_by": "callwitness {}".format(__version__),
        "public_baseline": PUBLIC,
        "window": {"from": min(stamps) if stamps else now,
                   "to": max(stamps) if stamps else now},
        "sample": {
            "servers_started": len(servers),
            "servers_called": len([s for s in servers
                                   if s["returned_bytes"]["n"]]),
            "tools_declared": sum(s["tool_count"] for s in servers),
            "calls": len(every_returned),
        },
        "caveat": (
            "Generated from {} calls recorded on this machine. These are your "
            "servers and your traffic, which makes them the right numbers to "
            "calibrate against and not comparable with anyone else's. "
            "declared_bytes is measured from the tools/list each server "
            "sent; 0 means no listing was recorded for it."
        ).format(len(every_returned)),
        "returned_bytes_all": sorted(every_returned),
        "overall": {
            "returned_bytes": spread(every_returned),
            "argument_bytes": spread(every_argument),
        },
        "servers": servers,
    }