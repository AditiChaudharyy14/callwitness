"""Tamper-evident recording: each call carries the hash of the one before it.

Why this exists
---------------
An append-only JSONL and a SQLite table are trivially editable by anyone with
filesystem access -- including a compromised agent running as the same user. A
log that can be silently rewritten is a convenience, not evidence, and evidence
is the thing anyone would ever pay for.

Chaining fixes that specific hole. Every record commits to its predecessor, so
editing, deleting, reordering or inserting a call breaks the chain from that
point on, and `bollard verify` says exactly where.

What this does NOT give you, stated plainly
-------------------------------------------
Tamper-EVIDENT, not tamper-proof. Someone with write access can still delete the
whole database, or truncate the chain and recompute every hash after the point
they altered -- nothing here stops that, because the verifier and the attacker
read the same file and neither knows what the head hash was supposed to be.

Closing that requires an anchor the operator does not control: publishing the
head hash somewhere append-only, or countersigning it off the machine. That is
a deployment decision, not a library one, so it is deliberately not invented
here. `bollard verify` prints the head hash for exactly that purpose -- write it
down somewhere the machine cannot reach, and the remaining hole closes.

Scope: the chain is per session. Within a session, nothing can be altered
undetected. Deleting an entire session is detectable only against an external
record of what sessions existed, which is the same anchoring problem.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

# The genesis value for a session's first record. Not secret, not random --
# it just has to be a fixed, recognisable starting point.
GENESIS = "0" * 64

# Fields that are covered by the hash. Changing this list changes every hash,
# so it is versioned with the schema. Anything a reader would rely on when
# answering "what did the agent do" belongs here; anything derived does not.
COVERED = (
    "session_id", "seq", "ts", "tool", "args_json", "args_bytes",
    "args_truncated", "signals_json", "redaction_json", "duration_ms",
    "is_error", "result_bytes", "result_preview",
)


def digest(record: Dict[str, Any], prev_hash: str) -> str:
    """Hash one record against its predecessor.

    Serialisation is canonical (sorted keys, no whitespace variance, ASCII) so
    the same record hashes the same on any machine, any Python, any platform.
    A verifier that disagrees with the writer about byte layout would report
    tampering on an untouched file, which is worse than no verifier at all.
    """
    payload = {key: record.get(key) for key in COVERED}
    payload["prev"] = prev_hash
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def verify_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Walk one session's records in sequence order and check every link.

    Returns a report rather than raising: a broken chain is a finding to be
    read, not an error to be handled, and the caller wants to know where the
    break is even though everything after it is unverifiable.
    """
    checked = 0
    unchained = 0
    prev = GENESIS
    break_at: Optional[Dict[str, Any]] = None
    head = GENESIS

    for row in rows:
        stored = row.get("hash")
        if not stored:
            # Written before chaining existed. Not evidence of tampering --
            # evidence of an older version, and saying otherwise would cry wolf
            # on every upgraded install.
            unchained += 1
            continue

        if break_at is None:
            expected_prev = row.get("prev_hash") or GENESIS
            recomputed = digest(row, expected_prev)
            if expected_prev != prev:
                break_at = {"seq": row.get("seq"), "id": row.get("id"),
                            "reason": "predecessor does not match: a record was "
                                      "removed, reordered or inserted here"}
            elif recomputed != stored:
                break_at = {"seq": row.get("seq"), "id": row.get("id"),
                            "reason": "content does not match its hash: this "
                                      "record was edited after it was written"}
            else:
                checked += 1
        prev = stored
        head = stored

    return {
        "records": len(rows),
        "verified": checked,
        "unchained": unchained,
        "intact": break_at is None,
        "break": break_at,
        "head": head,
    }


def format_report(sessions: List[Tuple[str, str, Dict[str, Any]]]) -> str:
    """Render one line per session, then the heads worth writing down."""
    if not sessions:
        return ("No sessions recorded yet.\n"
                "  bollard run -- <mcp server command>\n")

    lines: List[str] = []
    broken = 0
    unchained_total = 0
    verified_total = 0

    width = max(len(label or sid) for sid, label, _ in sessions)
    for sid, label, rep in sessions:
        verified_total += rep["verified"]
        unchained_total += rep["unchained"]
        name = label or sid
        if rep["break"]:
            broken += 1
            lines.append("BROKEN  {:<{w}}  {} records, breaks at seq {}".format(
                name, rep["records"], rep["break"]["seq"], w=width))
            lines.append("        {:<{w}}  {}".format("", rep["break"]["reason"], w=width))
        elif rep["verified"] == 0 and rep["unchained"]:
            lines.append("older   {:<{w}}  {} records written before chaining".format(
                name, rep["unchained"], w=width))
        else:
            note = ""
            if rep["unchained"]:
                note = " ({} predate chaining)".format(rep["unchained"])
            lines.append("ok      {:<{w}}  {} records verified{}".format(
                name, rep["verified"], note, w=width))

    lines.append("")
    if broken:
        lines.append("{} session{} did not verify. Everything after a break is "
                     "unverifiable, not".format(broken, "" if broken == 1 else "s"))
        lines.append("necessarily altered -- the chain simply cannot vouch for it.")
    else:
        lines.append("{} records verified across {} session{}.".format(
            verified_total, len(sessions), "" if len(sessions) == 1 else "s"))
        if unchained_total:
            lines.append("{} records predate chaining and are outside its "
                         "scope.".format(unchained_total))

    lines.append("")
    lines.append("This is tamper-evident, not tamper-proof: anyone who can write "
                 "to this file can also")
    lines.append("recompute every hash after a change. To close that, copy the "
                 "head hash below somewhere")
    lines.append("this machine cannot reach, and compare it next time.")
    lines.append("")
    for sid, label, rep in sessions:
        lines.append("  {}  {}".format(rep["head"], label or sid))
    return "\n".join(lines) + "\n"


def verify_store(home) -> List[Tuple[str, str, Dict[str, Any]]]:
    """Verify every session in a store. Returns (session_id, label, report)."""
    import sqlite3
    from pathlib import Path

    db = Path(home) / "bollard.db"
    if not db.exists():
        raise FileNotFoundError(db)
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        sessions = con.execute(
            "SELECT session_id, label FROM sessions ORDER BY started_at"
        ).fetchall()
        # Calls can exist for a session row that was never written (a crash
        # between the first call and end_session). Verifying only what sessions
        # claims would silently skip them, so take the union.
        orphans = con.execute(
            "SELECT DISTINCT session_id FROM calls WHERE session_id NOT IN "
            "(SELECT session_id FROM sessions)"
        ).fetchall()

        out = []
        for row in list(sessions) + [{"session_id": o[0], "label": None} for o in orphans]:
            sid = row["session_id"]
            rows = [dict(r) for r in con.execute(
                "SELECT * FROM calls WHERE session_id=? ORDER BY seq, id", (sid,)
            ).fetchall()]
            if not rows:
                continue
            out.append((sid, row["label"], verify_rows(rows)))
        return out
    finally:
        con.close()
