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

More than one server map per file
---------------------------------
A config is not one list of servers. Claude Code keeps a global map at the top
level and a separate map per project under `projects.<absolute path>`, so a
server registered inside a project used to be invisible here: the file parsed,
the servers sat right there, and install reported "nothing to wrap" while that
server ran unrecorded. A monitor that silently fails to monitor is worse than
no monitor at all, because it produces confident silence.

Two things follow from that, and both are load-bearing. Every known shape is
walked rather than just the first one found, and when the answer is "nothing to
wrap" the tool prints every location it looked at -- so a gap shows up as a
missing line someone can report, instead of as silence.
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
        ("Cursor (workspace)", Path.cwd() / ".cursor" / "mcp.json"),
        ("Windsurf", home / ".codeium" / "windsurf" / "mcp_config.json"),
        ("Claude Code", home / ".claude.json"),
        ("VS Code (workspace)", Path.cwd() / ".vscode" / "mcp.json"),
        ("Project", Path.cwd() / ".mcp.json"),
    ])
    return out


def discover() -> List[Tuple[str, Path]]:
    """Every known config that actually exists on this machine."""
    return [(name, path) for name, path in _client_paths() if path.is_file()]


def audit() -> List[Tuple[str, Path, bool]]:
    """Every location we look at, and whether it is there.

    Reported on the empty result, not just on failure. "Nothing to wrap" is
    three completely different situations -- no config exists, a config exists
    with no servers in it, or every server is already wrapped -- and only one
    of them means the user is finished.
    """
    return [(name, path, path.is_file()) for name, path in _client_paths()]


def _sections(doc: Dict[str, Any]) -> List[Tuple[str, List[Any], Dict[str, Any]]]:
    """Every server map in this document: (label, key path, the map itself).

    The key path is how `apply` finds its way back to the right dict. Writing
    a nested server into the top level would corrupt the config in a way the
    user could not see, which is the failure this whole module exists to avoid.
    """
    out: List[Tuple[str, List[Any], Dict[str, Any]]] = []

    for key in ("mcpServers", "servers"):
        value = doc.get(key)
        if isinstance(value, dict):
            out.append((key, [key], value))

    # Claude Code: one map per project, keyed by absolute path.
    projects = doc.get("projects")
    if isinstance(projects, dict):
        for project, config in projects.items():
            if not isinstance(config, dict):
                continue
            value = config.get("mcpServers")
            if isinstance(value, dict):
                out.append(("projects/{}".format(project),
                            ["projects", project, "mcpServers"], value))
    return out


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
        "doc": None, "key": None, "bom": False, "sections": [], "writes": [],
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

    sections = _sections(doc)
    if not sections:
        report["error"] = ("no server map found "
                           "(looked at mcpServers, servers, projects.*.mcpServers)")
        return report

    report["doc"] = doc
    report["key"] = sections[0][0]          # kept for callers that still read it
    report["sections"] = [label for label, _keypath, _servers in sections]

    # Only qualify names when there is something to disambiguate. A single-map
    # config is the common case and "filesystem" reads better than
    # "mcpServers :: filesystem".
    qualify = len(sections) > 1

    for label, keypath, servers in sections:
        for name, entry in servers.items():
            shown = "{} :: {}".format(label, name) if qualify else name

            if undo:
                restored = unwrap(entry) if isinstance(entry, dict) else None
                if restored is not None:
                    report["change"].append((shown, entry, restored))
                    report["writes"].append((keypath, name, restored))
                elif is_wrapped(entry):
                    report["skipped"].append(
                        (shown, "wrapped, but not in a shape we can undo"))
                else:
                    report["already"].append(shown)
                continue

            if not isinstance(entry, dict):
                report["skipped"].append((shown, "not an object"))
            elif is_wrapped(entry):
                report["already"].append(shown)
            elif "url" in entry and not entry.get("command"):
                report["skipped"].append(
                    (shown, "remote server -- needs `callwitness proxy --upstream {} "
                            "--port <port>` and a port you choose".format(entry.get("url"))))
            else:
                wrapped = wrap(name, entry, executable)
                if wrapped is None:
                    report["skipped"].append((shown, "no command to wrap"))
                else:
                    report["change"].append((shown, entry, wrapped))
                    report["writes"].append((keypath, name, wrapped))
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

    # Walk the recorded key path rather than assuming a single top-level map.
    # A server that lives under projects.<path>.mcpServers has to be written
    # back there, not invented at the top level.
    for keypath, name, after in report["writes"]:
        node = doc
        for key in keypath[:-1]:
            node = node[key]
        node[keypath[-1]][name] = after

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


def format_plan(reports: List[Dict[str, Any]], undo: bool, applied: bool,
                checked: Optional[List[Tuple[str, Path, bool]]] = None) -> str:
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
        lines.append("")
        lines.extend(_checked_lines(checked))
        lines.append("If yours lives somewhere else, point at it:")
        lines.append("  callwitness install --config /path/to/mcp.json")
        return "\n".join(lines) + "\n"

    if total == 0:
        # The important case. Saying only "nothing to wrap" is how a server we
        # failed to find looks exactly like a machine that is already set up.
        lines.append("Nothing to {}.".format(verb))
        lines.append("")
        lines.extend(_checked_lines(checked))
        lines.append("If a server is registered somewhere not listed above, that is a gap")
        lines.append("in callwitness -- please open an issue with the config shape.")
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


def _checked_lines(checked: Optional[List[Tuple[str, Path, bool]]]) -> List[str]:
    """The audit trail. Defaults to a live audit so callers need no change."""
    if checked is None:
        checked = audit()
    width = max((len(name) for name, _p, _e in checked), default=0)
    lines = ["Checked these locations:"]
    for name, path, exists in checked:
        lines.append("  {}  {}{}".format(
            name.ljust(width), path, "" if exists else "   (not found)"))
    lines.append("")
    return lines
