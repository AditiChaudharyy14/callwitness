"""Wrap the MCP servers a client already has, without hand-editing JSON.

Why this exists
---------------
Installing this used to mean: find a config file buried somewhere under
%APPDATA% or ~/Library, understand the wrapping syntax, and rewrite every
server entry by hand without breaking the JSON. That is the step where people
give up, and no amount of README fixes it -- the work is real and it is fiddly.

    "filesystem": {"command": "npx",     "args": ["-y", "@mcp/fs", "/data"]}
    "filesystem": {"command": "callwitness", "args": ["run", "--", "npx", "-y", "@mcp/fs", "/data"]}

So: find the configs, show exactly what would change, and only write when asked.

Dry-run by default
------------------
This edits a file the user did not write and cannot easily repair. Showing
before doing is the same principle the rest of the tool runs on -- observe
first, act only when someone has seen what the action is -- and it matters more
here than anywhere else, because a broken config means a broken agent.

Nothing is touched without `--apply`, `--apply` writes a timestamped backup
first, already-wrapped servers are left alone, and anything with a shape we do
not recognise is skipped and reported rather than guessed at.
"""

from __future__ import annotations

import codecs
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Known clients and where they keep their MCP config. Ordered so the most
# common ones are reported first. Unknown clients are handled by --config.
def _client_paths() -> List[Tuple[str, Path]]:
    home = Path.home()
    out: List[Tuple[str, Path]] = []

    if sys.platform == "darwin":
        out.append(("Claude Desktop",
                    home / "Library/Application Support/Claude/claude_desktop_config.json"))
    elif os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            out.append(("Claude Desktop",
                        Path(appdata) / "Claude" / "claude_desktop_config.json"))
    else:
        out.append(("Claude Desktop", home / ".config/Claude/claude_desktop_config.json"))

    out.extend([
        ("Cursor", home / ".cursor" / "mcp.json"),
        ("Windsurf", home / ".codeium" / "windsurf" / "mcp_config.json"),
        ("Claude Code", home / ".claude.json"),
        ("VS Code (workspace)", Path.cwd() / ".vscode" / "mcp.json"),
        ("Project", Path.cwd() / ".mcp.json"),
    ])
    return out


def discover() -> List[Tuple[str, Path]]:
    """Every known config that actually exists on this machine."""
    return [(name, path) for name, path in _client_paths() if path.is_file()]


def _servers_of(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The server map, whichever key this client uses for it."""
    for key in ("mcpServers", "servers"):
        value = doc.get(key)
        if isinstance(value, dict):
            return value
    return None


KNOWN_EXECUTABLES = frozenset({"callwitness", "bollard"})


def is_wrapped(entry: Dict[str, Any]) -> bool:
    if not isinstance(entry, dict):
        return False
    command = entry.get("command")
    if isinstance(command, str) and Path(command).stem in KNOWN_EXECUTABLES:
        return True
    # Also catch `python -m callwitness.cli ...`, which is how a dev install runs.
    args = entry.get("args")
    if not isinstance(args, list):
        return False
    return any(str(a).split(".")[0] in KNOWN_EXECUTABLES
               for a in args if str(a).endswith(".cli"))


def wrap(name: str, entry: Dict[str, Any], executable: str = "callwitness") -> Optional[Dict[str, Any]]:
    """Return the wrapped form of one server entry, or None if we should not.

    Returning None rather than guessing is deliberate. A remote server has a
    `url` and no command, and needs `callwitness proxy` plus a port the operator
    chooses -- inventing one and rewriting their config would be a worse
    outcome than telling them it needs a decision.
    """
    if not isinstance(entry, dict) or is_wrapped(entry):
        return None
    command = entry.get("command")
    if not isinstance(command, str) or not command:
        return None

    wrapped = dict(entry)
    wrapped["command"] = executable
    wrapped["args"] = ["run", "--label", name, "--",
                       command] + [str(a) for a in entry.get("args", [])]
    return wrapped


def unwrap(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Undo a wrap, recovering the original command and args."""
    if not is_wrapped(entry):
        return None
    args = [str(a) for a in entry.get("args", [])]
    if "--" not in args:
        return None
    rest = args[args.index("--") + 1:]
    if not rest:
        return None
    restored = dict(entry)
    restored["command"] = rest[0]
    if len(rest) > 1:
        restored["args"] = rest[1:]
    else:
        restored.pop("args", None)
    return restored


def plan(path: Path, executable: str = "callwitness",
         undo: bool = False) -> Dict[str, Any]:
    """Work out what would change in one config, without changing anything."""
    report: Dict[str, Any] = {
        "path": path, "error": None, "change": [], "already": [], "skipped": [],
        "doc": None, "key": None, "bom": False,
    }
    try:
        # utf-8-sig, not utf-8. Windows writes JSON with a byte-order mark by
        # default -- PowerShell's Out-File does it, Notepad does it, several
        # editors do it -- and plain utf-8 raises on the first character. The
        # user then sees "could not read as JSON" about a file that is perfectly
        # valid JSON, and concludes the tool is broken. utf-8-sig reads both.
        raw = path.read_bytes()
        report["bom"] = raw.startswith(codecs.BOM_UTF8)
        doc = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:
        report["error"] = "could not read as JSON: {}".format(exc)
        return report

    servers = _servers_of(doc)
    if servers is None:
        report["error"] = "no mcpServers section"
        return report

    report["doc"] = doc
    report["key"] = "mcpServers" if "mcpServers" in doc else "servers"

    for name, entry in servers.items():
        if undo:
            restored = unwrap(entry) if isinstance(entry, dict) else None
            if restored is not None:
                report["change"].append((name, entry, restored))
            elif is_wrapped(entry):
                report["skipped"].append((name, "wrapped, but not in a shape we can undo"))
            else:
                report["already"].append(name)
            continue

        if not isinstance(entry, dict):
            report["skipped"].append((name, "not an object"))
        elif is_wrapped(entry):
            report["already"].append(name)
        elif "url" in entry and not entry.get("command"):
            report["skipped"].append(
                (name, "remote server -- needs `callwitness proxy --upstream {} --port <port>` "
                       "and a port you choose".format(entry.get("url"))))
        else:
            wrapped = wrap(name, entry, executable)
            if wrapped is None:
                report["skipped"].append((name, "no command to wrap"))
            else:
                report["change"].append((name, entry, wrapped))
    return report


def apply(report: Dict[str, Any]) -> Optional[Path]:
    """Write the planned change, after backing the original up. Returns backup path."""
    path: Path = report["path"]
    doc = report["doc"]
    if doc is None or not report["change"]:
        return None

    backup = path.with_suffix(path.suffix + ".callwitness-backup-{}".format(
        time.strftime("%Y%m%d-%H%M%S")))
    shutil.copy2(str(path), str(backup))

    servers = doc[report["key"]]
    for name, _before, after in report["change"]:
        servers[name] = after

    # Write the file back the way we found it. If the client wrote a BOM, it
    # gets a BOM: quietly changing the encoding of someone's config is not our
    # business, and this tool's one promise is that it does not break configs.
    body = (json.dumps(doc, indent=2) + "\n").encode("utf-8")
    path.write_bytes(codecs.BOM_UTF8 + body if report.get("bom") else body)
    return backup


def _render_one(entry: Dict[str, Any]) -> str:
    command = entry.get("command", "")
    args = " ".join(str(a) for a in entry.get("args", []))
    return "{} {}".format(command, args).strip()


def format_plan(reports: List[Dict[str, Any]], undo: bool, applied: bool) -> str:
    verb = "unwrap" if undo else "wrap"
    lines: List[str] = []
    total = 0

    for report in reports:
        lines.append(str(report["path"]))
        if report["error"]:
            lines.append("  -- {}".format(report["error"]))
            lines.append("")
            continue
        for name, before, after in report["change"]:
            total += 1
            lines.append("  {}".format(name))
            lines.append("    - {}".format(_render_one(before)))
            lines.append("    + {}".format(_render_one(after)))
        for name in report["already"]:
            lines.append("  {}  (already {}ped, left alone)".format(
                name, "unwrap" if undo else "wrap"))
        for name, why in report["skipped"]:
            lines.append("  {}  SKIPPED: {}".format(name, why))
        if report.get("backup"):
            lines.append("  backup: {}".format(report["backup"].name))
        lines.append("")

    if not reports:
        lines.append("No MCP client configs found on this machine.")
        lines.append("If yours lives somewhere else, point at it:")
        lines.append("  callwitness install --config /path/to/mcp.json")
        return "\n".join(lines) + "\n"

    if total == 0:
        lines.append("Nothing to {}.".format(verb))
    elif applied:
        lines.append("{} server{} {}ped. Restart the client to pick it up.".format(
            total, "" if total == 1 else "s", verb))
        lines.append("Originals were backed up next to each config.")
    else:
        lines.append("{} server{} would be {}ped. Nothing has been changed.".format(
            total, "" if total == 1 else "s", verb))
        lines.append("")
        lines.append("Re-run with --apply to write it. Your config is backed up first,")
        lines.append("and `callwitness uninstall` puts it back.")
    return "\n".join(lines) + "\n"
