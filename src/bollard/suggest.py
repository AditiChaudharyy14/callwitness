"""Derive proposed policy from what was actually observed.

Why this exists
---------------
The roadmap said the next tier was YAML rules a human writes. But a human
writing `max_payload: 8KB` for `send_email` is guessing at a number they have
no way to know -- which is the exact thing this project says the industry is
doing wrong. Enforcement without data is guessing with extra steps, and a rule
language is not data.

So the rules come out of the observation tier rather than going into the policy
tier. Watch for two weeks, then propose the policy from what was seen. The
operator reviews a diff instead of inventing thresholds.

What it will not do
-------------------
Absence of evidence is the whole difficulty here, and it is stated rather than
papered over:

  * A destination never seen is not a destination that is forbidden. It may
    simply not have happened yet. Every destination suggestion says what
    fraction of traffic it covers and over what window, so the reader can judge.
  * A ceiling derived from n=4 is an anecdote. Tools below MIN_SAMPLE get
    reported as insufficient rather than given a confident-looking number.
  * High variance means we do not know the shape. Those tools are flagged for a
    human instead of being handed a threshold that will fire constantly.

A suggestion is a hypothesis with its evidence attached, not a finding.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Below this, a distribution is an anecdote. Chosen so a p99 has something to
# stand on; stated in the output rather than hidden.
MIN_SAMPLE = 30

# Headroom over the observed p99. A ceiling set exactly at the largest thing
# ever seen fires on the next ordinary day.
CEILING_HEADROOM = 1.5

# Above this ratio of distinct destinations to calls, the tool is not a fixed
# integration -- it is a general fetcher, and an allowlist is the wrong shape.
DEST_VARIANCE_LIMIT = 0.35


def _connect(home: Path) -> sqlite3.Connection:
    db = Path(home) / "bollard.db"
    if not db.exists():
        raise FileNotFoundError(db)
    return sqlite3.connect(str(db))


def _percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int(math.ceil((p / 100.0) * len(ordered))) - 1
    return ordered[max(0, min(len(ordered) - 1, idx))]


def _human_bytes(n: float) -> str:
    if n < 1024:
        return "{:.0f}B".format(n)
    if n < 1024 * 1024:
        return "{:.1f}KB".format(n / 1024).replace(".0KB", "KB")
    return "{:.1f}MB".format(n / (1024 * 1024)).replace(".0MB", "MB")


def collect(home: Path, since_days: Optional[float] = None) -> Dict[str, Any]:
    """Gather the per-tool evidence a suggestion would rest on."""
    con = _connect(home)
    query = ("SELECT tool, ts, args_bytes, signals_json, is_error FROM calls")
    params: tuple = ()
    if since_days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
        query += " WHERE ts >= ?"
        params = (cutoff,)
    rows = con.execute(query, params).fetchall()
    con.close()

    tools: Dict[str, Dict[str, Any]] = {}
    stamps: List[str] = []

    for tool, ts, args_bytes, signals, is_error in rows:
        entry = tools.setdefault(tool, {
            "n": 0, "errors": 0, "sizes": [], "hosts": {}, "emails": {}, "times": [],
        })
        entry["n"] += 1
        entry["errors"] += int(is_error or 0)
        entry["sizes"].append(args_bytes or 0)
        if ts:
            entry["times"].append(ts)
            stamps.append(ts)
        try:
            dest = json.loads(signals or "{}").get("destinations", {})
        except Exception:
            continue
        for kind in ("hosts", "emails"):
            for value in dest.get(kind, []):
                entry[kind][value] = entry[kind].get(value, 0) + 1

    window = None
    if stamps:
        window = (min(stamps), max(stamps))
    return {"tools": tools, "window": window, "total": len(rows)}


def _window_hours(entry: Dict[str, Any]) -> float:
    times = sorted(entry.get("times") or [])
    if len(times) < 2:
        return 0.0
    try:
        start = datetime.fromisoformat(times[0])
        end = datetime.fromisoformat(times[-1])
        return max((end - start).total_seconds() / 3600.0, 0.0)
    except Exception:
        return 0.0


def propose(evidence: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Turn evidence into rule proposals, each carrying its own basis."""
    out: List[Dict[str, Any]] = []

    for tool in sorted(evidence["tools"]):
        entry = evidence["tools"][tool]
        n = entry["n"]

        if n < MIN_SAMPLE:
            out.append({
                "tool": tool, "rule": "insufficient_data", "value": None,
                "basis": "only {} call{} observed; {} needed before a threshold "
                         "means anything".format(n, "" if n == 1 else "s", MIN_SAMPLE),
                "confident": False,
            })
            continue

        sizes = entry["sizes"]
        p99 = _percentile(sizes, 99)
        ceiling = max(int(p99 * CEILING_HEADROOM), 1024)
        out.append({
            "tool": tool, "rule": "max_payload", "value": ceiling,
            "basis": "p99 observed {} over n={}; {}x headroom".format(
                _human_bytes(p99), n, CEILING_HEADROOM),
            "confident": True,
        })

        for kind in ("hosts", "emails"):
            seen = entry[kind]
            if not seen:
                continue
            distinct = len(seen)
            total = sum(seen.values())
            variance = distinct / float(n)
            if variance > DEST_VARIANCE_LIMIT:
                out.append({
                    "tool": tool, "rule": "destinations_" + kind, "value": None,
                    "basis": "{} distinct {} across {} calls -- too varied for an "
                             "allowlist; this reads as a general-purpose fetcher, "
                             "review by hand".format(distinct, kind, n),
                    "confident": False,
                })
                continue
            top = sorted(seen.items(), key=lambda kv: -kv[1])
            covered = sum(c for _, c in top) / float(total or 1)
            out.append({
                "tool": tool, "rule": "destinations_" + kind,
                "value": [v for v, _ in top],
                "basis": "{} distinct {} covering {:.0%} of observed traffic "
                         "over n={}".format(distinct, kind, covered, n),
                "confident": True,
            })

        hours = _window_hours(entry)
        if hours >= 1.0:
            per_hour = n / hours
            limit = max(int(math.ceil(per_hour * 2)), 1)
            out.append({
                "tool": tool, "rule": "rate_limit_per_hour", "value": limit,
                "basis": "{:.1f}/hour average over {:.1f} hours; 2x headroom".format(
                    per_hour, hours),
                "confident": True,
            })

    return out


def format_suggestions(home: Path, since_days: Optional[float] = None) -> str:
    evidence = collect(home, since_days)
    if not evidence["total"]:
        return ("No calls recorded in that window.\n"
                "  bollard run -- <mcp server command>\n")

    proposals = propose(evidence)
    lines: List[str] = []
    window = evidence["window"]
    header = "{} calls".format(evidence["total"])
    if window:
        header += "  {}  ->  {}".format(window[0][:19], window[1][:19])
    lines.append(header)
    lines.append("")

    width = max((len(p["tool"]) for p in proposals), default=4)
    for p in proposals:
        mark = " " if p["confident"] else "?"
        value = p["value"]
        if isinstance(value, list):
            shown = "{} allowed".format(len(value))
        elif value is None:
            shown = "--"
        elif p["rule"] == "max_payload":
            shown = _human_bytes(value)
        else:
            shown = str(value)
        lines.append("{} {:<{w}}  {:<22} {:<12} # {}".format(
            mark, p["tool"], p["rule"], shown, p["basis"], w=width))

    lines.append("")
    lines.append("Lines marked ? are not proposals -- they are places the data "
                 "cannot support one yet.")
    lines.append("A destination not seen is not a destination that is forbidden; "
                 "it may simply not have")
    lines.append("happened yet. Review before enforcing anything here.")
    return "\n".join(lines) + "\n"


def format_yaml(home: Path, since_days: Optional[float] = None) -> str:
    """A machine-readable policy draft. Deliberately commented, not clean."""
    evidence = collect(home, since_days)
    proposals = propose(evidence)
    lines = ["# Generated by `bollard suggest` from observed traffic.",
             "# These are hypotheses with evidence attached, not verified limits.",
             "# Review every line before enforcing it.", "", "version: 1", "rules:"]

    by_tool: Dict[str, List[Dict[str, Any]]] = {}
    for p in proposals:
        by_tool.setdefault(p["tool"], []).append(p)

    for tool in sorted(by_tool):
        confident = [p for p in by_tool[tool] if p["confident"]]
        if not confident:
            lines.append("  # {}: {}".format(tool, by_tool[tool][0]["basis"]))
            continue
        lines.append("  {}:".format(tool))
        for p in confident:
            lines.append("    # {}".format(p["basis"]))
            if isinstance(p["value"], list):
                lines.append("    {}:".format(p["rule"]))
                for item in p["value"]:
                    lines.append("      - {}".format(item))
            else:
                lines.append("    {}: {}".format(p["rule"], p["value"]))
        for p in by_tool[tool]:
            if not p["confident"]:
                lines.append("    # UNRESOLVED {}: {}".format(p["rule"], p["basis"]))
    return "\n".join(lines) + "\n"
