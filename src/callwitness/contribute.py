"""Opt-in contribution of traffic *shape* to a public baseline.

Specified in docs/CONTRIBUTE.md. Read that first; this file implements it and
the constraints there are not negotiable from here.

The four that shape every line below:

  Off by default.       An install that never reads the docs sends nothing, ever.
  Never on the hot path. No network call happens during a proxy run. This module
                        is only ever reached from `callwitness contribute`, which
                        a person types. There is no thread, no timer, no atexit
                        hook, and adding one would break the promise that the
                        proxy cannot delay the stream.
  Auditable.            --dry-run prints the exact bytes. Not a summary of them.
  Shape only.           Sizes, counts and durations. Never arguments, paths,
                        filenames, hosts or results -- including hashed, which
                        is not anonymisation when the input space is small
                        enough to enumerate.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import __version__

SCHEMA = "callwitness.contribution.v1"

# The collector, a Cloudflare Worker that validates the payload against the same
# key list this file enforces and stores it unchanged. Overridable by env var so
# anyone can run their own and point their installs at it.
CONTRIBUTE_URL: Optional[str] = "https://callwitness-contribute.aditichaudharyy14.workers.dev/v1/contributions"

ENV_URL = "CALLWITNESS_CONTRIBUTE_URL"

# Exactly the keys this schema version emits. The collector rejects anything
# else, and so does the test suite: a field that appears here without being
# added to the spec is how a shape-only payload quietly stops being shape-only.
PAYLOAD_KEYS = {"schema", "install", "version", "platform", "python",
                "window", "servers"}
SERVER_KEYS = {"package", "declared_bytes", "tool_count", "tools"}
TOOL_KEYS = {"tool", "calls", "errors", "returned_bytes", "argument_bytes",
             "latency_ms"}


def config_path(home: Path) -> Path:
    return Path(home) / "contribute.json"


def load_config(home: Path) -> Dict[str, Any]:
    try:
        return json.loads(config_path(home).read_text(encoding="utf-8"))
    except Exception:
        return {"enabled": False}


def save_config(home: Path, config: Dict[str, Any]) -> None:
    path = config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


# -- naming ---------------------------------------------------------------

# The runner, the registry it fetches from, and how many words of the command
# belong to the runner itself. `pipx run foo` spends two words before the
# package starts; `uvx foo` spends one. Getting that count wrong reports the
# subcommand as the package name, which is both wrong and permanent once it is
# in a published baseline.
_RUNNERS = [
    (("pnpm", "dlx"), "npm"),
    (("pipx", "run"), "pypi"),
    (("npx",), "npm"),
    (("bunx",), "npm"),
    (("uvx",), "pypi"),
    (("uv", "tool", "run"), "pypi"),
]
_PKG = re.compile(r"^(?:@[a-z0-9][\w.-]*/)?[a-z0-9][\w.-]*$", re.I)


def package_of(command: str) -> str:
    """Name the wrapped server, but only when the name is already public.

    A published package identifies software. A path identifies an organisation:
    /opt/acme/internal-server says who you are, and so does ./our-server.js. So
    public registry specifiers are reported and everything else becomes
    "unlisted", which keeps the size distribution -- the entire point -- and
    discards the identity.

    Version suffixes are stripped: @latest and @1.2.3 would fragment the
    baseline across what is one package.
    """
    parts = (command or "").split()
    if not parts:
        return "unlisted"
    # The runner may be written as a bare name or a full path to it.
    first = Path(parts[0]).name.lower()
    first = first[:-4] if first.endswith(".cmd") else first
    registry = None
    consumed = 0
    for words, which in _RUNNERS:
        if first == words[0] and [p.lower() for p in parts[1:len(words)]] == list(words[1:]):
            registry, consumed = which, len(words)
            break
    if registry is None:
        return "unlisted"

    for token in parts[consumed:]:
        if token.startswith("-"):          # -y, --quiet, and friends
            continue
        name = token
        # @scope/name@1.2.3 -> @scope/name ; name@latest -> name
        at = name.rfind("@")
        if at > 0:
            name = name[:at]
        if _PKG.match(name):
            return "{}:{}".format(registry, name)
        return "unlisted"                  # a path handed to npx is still a path
    return "unlisted"


def anonymise_tools(names: Sequence[str]) -> Dict[str, str]:
    """Stable pseudonyms for the tools of a server we could not name publicly.

    On a public package the tool names are already public and are sent as-is.
    On an unlisted server they are the organisation's vocabulary -- approve_wire,
    fetch_patient -- so they become tool_1, tool_2, ordered so the mapping is
    stable within one payload and meaningless outside it.
    """
    return {name: "tool_{}".format(i + 1)
            for i, name in enumerate(sorted(set(names)))}


# -- statistics -----------------------------------------------------------

def percentile(values: Sequence[float], q: float) -> int:
    """Nearest-rank, so every number reported is one that actually occurred.

    Interpolation would invent a byte count nobody measured, which is a strange
    thing for a measurement tool to send.
    """
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(q * (len(ordered) - 1)))))
    return int(ordered[index])


def spread(values: Sequence[float]) -> Dict[str, int]:
    if not values:
        return {"min": 0, "p50": 0, "p95": 0, "max": 0}
    return {"min": int(min(values)), "p50": percentile(values, 0.50),
            "p95": percentile(values, 0.95), "max": int(max(values))}


# -- the payload ----------------------------------------------------------

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
        params: Tuple[Any, ...] = ()
        if since:
            query += " WHERE c.ts > ?"
            params = (since,)
        return conn.execute(query + " ORDER BY c.ts", params).fetchall()
    except Exception:
        return []
    finally:
        conn.close()


def build_payload(home: Path, install: str,
                  since: Optional[str] = None) -> Dict[str, Any]:
    """Everything that would be sent, and nothing that would not."""
    rows = _rows(home, since)

    grouped: Dict[str, Dict[str, List[sqlite3.Row]]] = {}
    for row in rows:
        package = package_of(row["command"] or "")
        grouped.setdefault(package, {}).setdefault(row["tool"] or "?", []).append(row)

    servers = []
    for package in sorted(grouped):
        tools = grouped[package]
        public = package != "unlisted"
        pseudonym = {} if public else anonymise_tools(list(tools))
        entries = []
        for name in sorted(tools):
            calls = tools[name]
            entries.append({
                "tool": name if public else pseudonym[name],
                "calls": len(calls),
                "errors": sum(1 for c in calls if c["is_error"]),
                "returned_bytes": spread([c["result_bytes"] or 0 for c in calls]),
                "argument_bytes": {
                    "p50": percentile([c["args_bytes"] or 0 for c in calls], 0.50),
                    "max": int(max((c["args_bytes"] or 0) for c in calls)),
                },
                "latency_ms": {
                    "p50": percentile([c["duration_ms"] or 0 for c in calls], 0.50),
                    "p95": percentile([c["duration_ms"] or 0 for c in calls], 0.95),
                },
            })
        servers.append({
            # declared_bytes needs a tools/list observation the recorder does not
            # keep yet. Reported as 0 rather than guessed; a fabricated number in
            # a baseline is worse than a missing one.
            "package": package,
            "declared_bytes": 0,
            "tool_count": len(tools),
            "tools": entries,
        })

    stamps = [r["ts"] for r in rows if r["ts"]]
    now = datetime.now(timezone.utc).isoformat()
    return {
        "schema": SCHEMA,
        "install": install,
        "version": __version__,
        # sys.platform, not platform.platform(): the latter carries the kernel
        # build string, which on some systems includes the hostname.
        "platform": _platform(),
        "python": _python(),
        "window": {"from": min(stamps) if stamps else now,
                   "to": max(stamps) if stamps else now},
        "servers": servers,
    }


def _platform() -> str:
    import sys
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


def _python() -> str:
    import sys
    return "{}.{}".format(sys.version_info[0], sys.version_info[1])


def audit(payload: Dict[str, Any]) -> List[str]:
    """Every key in the payload that this schema version does not define.

    Called before every send, not only in tests. If someone adds a field and
    forgets the spec, the send fails loudly instead of quietly widening what
    leaves people's machines.
    """
    problems = []
    for key in sorted(set(payload) - PAYLOAD_KEYS):
        problems.append("payload.{}".format(key))
    for server in payload.get("servers", []):
        for key in sorted(set(server) - SERVER_KEYS):
            problems.append("servers[].{}".format(key))
        for tool in server.get("tools", []):
            for key in sorted(set(tool) - TOOL_KEYS):
                problems.append("servers[].tools[].{}".format(key))
    return problems


# -- summary and sending --------------------------------------------------

def summarise(payload: Dict[str, Any]) -> str:
    servers = payload.get("servers", [])
    public = sum(1 for s in servers if s["package"] != "unlisted")
    tools = sum(len(s["tools"]) for s in servers)
    calls = sum(t["calls"] for s in servers for t in s["tools"])
    blob = json.dumps(payload, indent=2, sort_keys=True)
    window = payload.get("window", {})
    return (
        "  window     {} .. {}\n"
        "  servers    {}  ({} public {}, {} unlisted)\n"
        "  tools      {}\n"
        "  calls      {}\n"
        "  payload    {:,} bytes\n"
    ).format(
        (window.get("from") or "")[:19], (window.get("to") or "")[:19],
        len(servers), public, "package" if public == 1 else "packages",
        len(servers) - public, tools, calls, len(blob.encode()),
    )


def endpoint() -> Optional[str]:
    return os.environ.get(ENV_URL) or CONTRIBUTE_URL


def send(payload: Dict[str, Any], timeout: float = 30.0) -> Tuple[bool, str]:
    url = endpoint()
    if not url:
        return False, (
            "No collector is configured in this build, so there is nowhere to\n"
            "send it. Nothing has left this machine. Set {} if you\n"
            "run your own.".format(ENV_URL))
    problems = audit(payload)
    if problems:
        return False, ("Refusing to send: unexpected fields " +
                       ", ".join(problems) + "\nThis is a bug in callwitness.")
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "callwitness/{}".format(__version__)})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return True, "Sent {:,} bytes. Thank you.".format(len(body))
    except urllib.error.HTTPError as exc:
        return False, "Collector refused it: HTTP {}.".format(exc.code)
    except Exception as exc:
        return False, "Could not reach the collector: {}.".format(exc)


NOTICE = """callwitness contribute

  Sends the SHAPE of your tool traffic to a public baseline: which public
  packages you wrap, how many calls, how many bytes came back, how long it
  took. Nothing else.

  Never sent:  arguments, paths, filenames, hostnames, results, or any part
               of them -- including hashed.

  Nothing is sent automatically. Nothing is sent while the proxy is running.
  You run `callwitness contribute --send` yourself, and it shows you the
  payload and asks before every send.

  See exactly what would leave:  callwitness contribute --dry-run
  The published baseline:        https://callwitness.tech/baseline
"""
