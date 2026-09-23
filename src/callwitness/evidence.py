"""A one-page evidence report for one recorded run.

Why this exists
---------------
The recorder answers "what did the agent do?" for the person who ran it. The
person who usually asks -- a manager, a customer, compliance, finance -- will
not install anything or read a terminal. They need one page they can open,
read, and keep. This builds it from the local store and nothing else.

What it includes, and what it deliberately leaves out
-----------------------------------------------------
Tool names, timings, sizes, failures, destinations, and every record's hash,
plus the chain's verdict and head hash. Not arguments and not result previews:
a report exists to be handed to someone, and whatever is in it leaves the
machine with it. The hashes let anyone holding the full data check it against
this page without the page having to carry it.

Nothing is sent anywhere. The report is a file on disk; sharing it is a
decision the user makes, not one the tool makes for them.
"""

from __future__ import annotations

import html
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .chain import verify_store

DB_NAME = "callwitness.db"


def _connect(home: Path) -> Optional[sqlite3.Connection]:
    path = Path(home) / DB_NAME
    if not path.is_file():
        return None
    connection = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def resolve(home: Path, session: Optional[str]) -> Optional[str]:
    """Full session id from a prefix, or the newest session that made calls."""
    connection = _connect(home)
    if connection is None:
        return None
    try:
        if session:
            rows = connection.execute(
                "SELECT DISTINCT session_id FROM calls WHERE session_id LIKE ?",
                (session + "%",)).fetchall()
            return rows[0][0] if len(rows) == 1 else None
        row = connection.execute(
            "SELECT session_id FROM calls ORDER BY ts DESC LIMIT 1").fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def _destinations(blob: Optional[str]) -> List[str]:
    try:
        found = (json.loads(blob or "{}").get("destinations") or {})
    except (ValueError, AttributeError):
        return []
    out: List[str] = []
    for values in found.values():
        if isinstance(values, list):
            out.extend(str(v) for v in values)
    return out


def build(home: Path, session_id: str) -> Optional[Dict[str, Any]]:
    """Everything the page shows, read-only from the store."""
    connection = _connect(home)
    if connection is None:
        return None
    try:
        srow = connection.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        cols = {r[1] for r in connection.execute("PRAGMA table_info(calls)")}
        want = ["seq", "ts", "tool", "duration_ms", "is_error", "result_bytes",
                "args_bytes", "signals_json", "hash", "result_sha256"]
        select = ", ".join(c if c in cols else "NULL AS {}".format(c) for c in want)
        order = "seq, id" if "seq" in cols else "id"
        calls = [dict(r) for r in connection.execute(
            "SELECT {} FROM calls WHERE session_id = ? ORDER BY {}".format(select, order),
            (session_id,)).fetchall()]
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    if not calls:
        return None

    verdict = None
    for sid, _label, rep in verify_store(home):
        if sid == session_id:
            verdict = rep
            break

    destinations: Dict[str, int] = {}
    for call in calls:
        for d in _destinations(call.get("signals_json")):
            destinations[d] = destinations.get(d, 0) + 1

    dropped = 0
    connection = _connect(home)
    if connection is not None:
        try:
            for (payload,) in connection.execute(
                    "SELECT payload FROM events WHERE session_id = ? AND "
                    "kind = 'observer:stats'", (session_id,)).fetchall():
                try:
                    dropped += int(json.loads(payload or "{}").get("dropped") or 0)
                except (ValueError, TypeError, AttributeError):
                    pass
        except sqlite3.Error:
            pass
        finally:
            connection.close()

    session = dict(srow) if srow else {"session_id": session_id}
    return {
        "session": session,
        "calls": calls,
        "failed": sum(1 for c in calls if c.get("is_error")),
        "returned": sum(int(c.get("result_bytes") or 0) for c in calls),
        "spent_ms": sum(float(c.get("duration_ms") or 0) for c in calls),
        "destinations": sorted(destinations.items(), key=lambda kv: -kv[1]),
        "verdict": verdict,
        "dropped": dropped,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


def human(size: int) -> str:
    size = int(size or 0)
    if size >= 1024 * 1024:
        return "{:.1f} MB".format(size / (1024.0 * 1024.0))
    if size >= 1024:
        return "{:.1f} KB".format(size / 1024.0)
    return "{} B".format(size)


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _short(stamp: Optional[str]) -> str:
    return _e((stamp or "")[:19].replace("T", " "))


def _verdict_block(verdict: Optional[Dict[str, Any]]) -> str:
    if not verdict:
        return ('<div class="badge warn"><b>Not verified</b>'
                '<span>This run could not be checked.</span></div>')
    if verdict.get("break"):
        b = verdict["break"]
        return ('<div class="badge bad"><b>Chain broken at record {}</b>'
                '<span>{}</span></div>').format(_e(b.get("seq")), _e(b.get("reason")))
    if verdict.get("verified", 0) == 0:
        return ('<div class="badge warn"><b>Recorded before chaining existed</b>'
                '<span>{} records, none hash-chained.</span></div>').format(
                    _e(verdict.get("records")))
    return ('<div class="badge ok"><b>Chain intact</b><span>{} of {} records verified. '
            'Nothing was edited, removed, reordered or inserted since they were '
            'written.</span></div>').format(_e(verdict.get("verified")),
                                           _e(verdict.get("records")))


STYLE = """
:root{--navy:#182646;--cream:#f4ede2;--gold:#a47a35;--ink:#1b2233;--sub:#5b6475;
--line:#e3ddd2;--ok:#1f7a4d;--bad:#b3261e;--warn:#9a6700}
*{box-sizing:border-box}body{margin:0;background:#fff;color:var(--ink);
font:15px/1.5 -apple-system,"Segoe UI",Helvetica,Arial,sans-serif}
header{background:var(--navy);color:var(--cream);padding:28px 40px}
header h1{margin:0;font-size:22px;letter-spacing:.2px}
header p{margin:4px 0 0;color:#cbbfae;font-size:13px}
main{max-width:1000px;margin:0 auto;padding:28px 40px 48px}
.badge{border-radius:10px;padding:16px 18px;margin:0 0 24px;border:1px solid}
.badge b{display:block;font-size:17px}.badge span{color:var(--sub)}
.ok{border-color:var(--ok);background:#eef7f1}.ok b{color:var(--ok)}
.bad{border-color:var(--bad);background:#fbeceb}.bad b{color:var(--bad)}
.warn{border-color:var(--warn);background:#fbf5e6}.warn b{color:var(--warn)}
.facts{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:0 0 28px}
.fact{border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.fact small{color:var(--sub);display:block;font-size:12px}
.fact b{font-size:20px}h2{font-size:16px;margin:28px 0 10px;color:var(--navy)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--sub);font-weight:600}td.n{text-align:right;white-space:nowrap}
code,.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
.err{color:var(--bad);font-weight:600}.head{word-break:break-all;background:var(--cream);
padding:10px 12px;border-radius:8px;display:block}
.meta{color:var(--sub);font-size:13px}footer{color:var(--sub);font-size:12px;
border-top:1px solid var(--line);margin-top:36px;padding-top:14px}
@media (max-width:700px){.facts{grid-template-columns:repeat(2,1fr)}
header,main{padding-left:16px;padding-right:16px}}
@media print{header{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
"""


def render_html(data: Dict[str, Any], version: str = "") -> str:
    s = data["session"]
    v = data.get("verdict") or {}
    calls = data["calls"]
    rows = []
    for c in calls:
        status = '<span class="err">failed</span>' if c.get("is_error") else "ok"
        digest = c.get("result_sha256")
        rows.append(
            "<tr><td class='n'>{}</td><td>{}</td><td>{}</td><td>{}</td>"
            "<td class='n'>{}</td><td class='n'>{:.0f} ms</td>"
            "<td class='mono'>{}</td></tr>".format(
                _e(c.get("seq")), _short(c.get("ts")), _e(c.get("tool")), status,
                human(c.get("result_bytes")), float(c.get("duration_ms") or 0),
                _e(digest[:16] + "…") if digest else "—"))
    dest = "".join("<tr><td>{}</td><td class='n'>{}</td></tr>".format(_e(d), n)
                   for d, n in data["destinations"]) or \
        "<tr><td colspan='2' class='meta'>No destinations were found in any call.</td></tr>"
    head = v.get("head") or ""
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Callwitness evidence report</title><style>{style}</style></head><body>
<header><h1>Callwitness evidence report</h1>
<p>Verifiable records of AI agent tool interactions</p></header><main>
{verdict}
<p class="meta">Run <code>{sid}</code>{label} &middot; started {start} &middot; ended {end}</p>
<p class="meta">Command: <code>{cmd}</code></p>
<div class="facts">
<div class="fact"><small>Tool calls</small><b>{n}</b></div>
<div class="fact"><small>Failed</small><b>{failed}</b></div>
<div class="fact"><small>Returned</small><b>{returned}</b></div>
<div class="fact"><small>Time in tools</small><b>{spent}</b></div>
</div>
<h2>Head hash</h2>
<code class="head">{head}</code>
<p class="meta">This fingerprint covers every record below, in order. Keep a copy
somewhere the operator of this machine cannot edit. If it ever stops matching
<code>callwitness verify</code>, the record was changed after the fact.</p>
<h2>Every call</h2>
<table><thead><tr><th>#</th><th>Time (UTC)</th><th>Tool</th><th>Status</th>
<th>Returned</th><th>Took</th><th>Response hash</th></tr></thead>
<tbody>{rows}</tbody></table>
<h2>Where calls pointed</h2>
<table><thead><tr><th>Destination</th><th>Calls</th></tr></thead><tbody>{dest}</tbody></table>
<footer>Generated {generated} by Callwitness {version}. Built from the local
store only; nothing was sent anywhere. Arguments and response contents are not
included, only their sizes and hashes. Tamper-evident, not tamper-proof:
someone with write access to the store could rewrite the whole chain, which is
why the head hash should be kept elsewhere. Re-check with
<code>callwitness verify</code>.</footer>
</main></body></html>
""".format(
        style=STYLE, verdict=_verdict_block(v) + (
            '<div class="badge warn"><b>{} observations dropped under load</b>'
            '<span>Those messages were forwarded to the agent unchanged but not '
            'recorded, so this page lists fewer calls than were made.</span>'
            '</div>'.format(int(data.get("dropped") or 0))
            if data.get("dropped") else ""),
        sid=_e(s.get("session_id")),
        label=(" (" + _e(s.get("label")) + ")") if s.get("label") else "",
        start=_short(s.get("started_at")) or "unknown",
        end=_short(s.get("ended_at")) or "not recorded",
        cmd=_e(s.get("command") or "unknown"), n=len(calls),
        failed=data["failed"], returned=human(data["returned"]),
        spent=("{:.0f} ms".format(data["spent_ms"]) if data["spent_ms"] < 1000 else "{:.1f} s".format(data["spent_ms"] / 1000.0)), head=_e(head) or "none",
        rows="".join(rows), dest=dest, generated=_e(data["generated"]),
        version=_e(version))
