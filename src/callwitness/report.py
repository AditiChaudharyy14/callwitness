"""Reading back what was observed."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Tuple


def _connect(home: Path) -> sqlite3.Connection:
    db = Path(home) / "callwitness.db"
    if not db.exists():
        raise FileNotFoundError(db)
    return sqlite3.connect(str(db))


def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int(round((p / 100.0) * (len(ordered) - 1)))
    return ordered[max(0, min(len(ordered) - 1, idx))]


def summarise(home: Path) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, int], List[tuple]]:
    """Return (per-tool stats, destination counts, largest payloads)."""
    con = _connect(home)
    rows = con.execute(
        "SELECT tool, duration_ms, is_error, args_bytes, result_bytes, signals_json "
        "FROM calls"
    ).fetchall()

    by_tool: Dict[str, Dict[str, Any]] = {}
    destinations: Dict[str, int] = {}

    for tool, duration, is_error, args_bytes, result_bytes, signals in rows:
        stats = by_tool.setdefault(
            tool, {"n": 0, "errors": 0, "durations": [], "arg_sizes": [], "out_bytes": 0}
        )
        stats["n"] += 1
        stats["errors"] += int(is_error or 0)
        if duration is not None:
            stats["durations"].append(duration)
        stats["arg_sizes"].append(args_bytes or 0)
        stats["out_bytes"] += result_bytes or 0

        try:
            parsed = json.loads(signals or "{}")
        except Exception:
            continue
        for kind in ("hosts", "emails", "ips"):
            for value in parsed.get("destinations", {}).get(kind, []):
                destinations[value] = destinations.get(value, 0) + 1

    largest = con.execute(
        "SELECT ts, tool, args_bytes, signals_json FROM calls "
        "ORDER BY args_bytes DESC LIMIT 5"
    ).fetchall()
    con.close()
    return by_tool, destinations, largest


# Enough rows to see the shape, few enough that the report ends.
SHOWN_TOOLS = 15


def _bytes(size) -> str:
    size = int(size or 0)
    if size >= 1024 * 1024:
        return "{:.1f}MB".format(size / (1024.0 * 1024.0))
    if size >= 1024:
        return "{:.1f}KB".format(size / 1024.0)
    return "{}B".format(size)


def _when(stamp) -> str:
    """One timestamp format, one zone, shared with `last`.

    Imported lazily so report.py keeps no import-time dependency on last.py.
    Nineteen characters, same as the slice it replaces, so every column that
    was aligned stays aligned.
    """
    try:
        from .last import local
        when = local(stamp)
    except Exception:
        when = None
    return when.strftime("%Y-%m-%d %H:%M:%S") if when else str(stamp)[:19]


def format_stats(home: Path) -> str:
    by_tool, destinations, largest = summarise(home)
    if not by_tool:
        return "No tool calls recorded yet."

    total = sum(s["n"] for s in by_tool.values())
    lines = ["", "{} tool calls across {} tools".format(total, len(by_tool)), ""]
    header = "{:<28}{:>7}{:>6}{:>9}{:>9}{:>11}{:>11}".format(
        "tool", "calls", "err", "p50ms", "p95ms", "returned", "worst")
    lines += [header, "-" * len(header)]

    # The worst single call per tool. `out_bytes` is a sum, which answers how
    # much in total; the question people have is how big one call can get,
    # because that is the one that ends a context window.
    from . import cost as _cost
    worst = {}
    try:
        worst = {t["tool"]: int(t["largest"] or 0)
                 for t in _cost.summarise(home)["tools"]}
    except Exception:
        pass

    ranked = sorted(by_tool.items(), key=lambda kv: -kv[1]["out_bytes"])
    for tool, s in ranked[:SHOWN_TOOLS]:
        lines.append("{:<28}{:>7}{:>6}{:>9.0f}{:>9.0f}{:>11}{:>11}".format(
            str(tool)[:27], s["n"], s["errors"],
            percentile(s["durations"], 50), percentile(s["durations"], 95),
            _bytes(s["out_bytes"]),
            _bytes(worst[tool]) if tool in worst else "-"))

    if len(ranked) > SHOWN_TOOLS:
        lines.append("... and {} more tools, smaller than these".format(
            len(ranked) - SHOWN_TOOLS))

    if destinations:
        lines += ["", "destinations named in arguments", "-" * 42]
        for dest, count in sorted(destinations.items(), key=lambda kv: -kv[1])[:20]:
            lines.append(f"  {count:>5}  {dest}")

    lines += ["", "largest arguments sent to tools", "-" * 42]
    for ts, tool, args_bytes, signals in largest:
        try:
            parsed = json.loads(signals or "{}").get("destinations", {})
            dest = ", ".join(parsed.get("hosts", []) + parsed.get("emails", [])) or "-"
        except Exception:
            dest = "-"
        lines.append(f"  {args_bytes:>9}B  {str(tool):<22} {dest[:40]:<42} {_when(ts)}")

    return "\n".join(lines) + "\n"


def format_tail(home: Path, n: int = 20) -> str:
    con = _connect(home)
    rows = con.execute(
        "SELECT ts, tool, args_bytes, result_bytes, duration_ms, is_error, signals_json "
        "FROM calls ORDER BY id DESC LIMIT ?", (n,)
    ).fetchall()
    con.close()

    out = []
    for ts, tool, args_bytes, result_bytes, duration, is_error, signals in reversed(rows):
        try:
            parsed = json.loads(signals or "{}").get("destinations", {})
            dest = ", ".join(parsed.get("hosts", []) + parsed.get("emails", []))
        except Exception:
            dest = ""
        flag = "ERR" if is_error else "ok "
        out.append(f"{_when(ts)}  {flag}  {str(tool):<22} "
                   f"in={args_bytes:<8} out={result_bytes:<9} "
                   f"{(duration or 0):>7.0f}ms  {dest[:44]}")
    return "\n".join(out) + ("\n" if out else "")


def export_jsonl(home: Path, out_path: Path) -> int:
    con = _connect(home)
    con.row_factory = sqlite3.Row
    count = 0
    with open(out_path, "w", encoding="utf-8") as fh:
        for row in con.execute("SELECT * FROM calls ORDER BY id"):
            record = dict(row)
            for key in ("args_json", "signals_json"):
                try:
                    record[key[:-5]] = json.loads(record.pop(key) or "null")
                except Exception:
                    record[key[:-5]] = None
            fh.write(json.dumps(record, default=str) + "\n")
            count += 1
    con.close()
    return count
