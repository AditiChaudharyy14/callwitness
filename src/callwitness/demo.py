"""Record a real server in under a minute, with no agent and no config.

Why this exists
---------------
Everything else here assumes you already have an agent wired to MCP servers.
Someone who has just run `pip install callwitness` does not. They run
`callwitness run -- npx -y <server>`, the server starts, and then nothing
happens at all -- because an MCP server over stdio sits silent until a client
speaks to it. The tool looks broken at the exact moment a stranger is deciding
whether it works.

So this is the client. It starts a server through the proxy, performs the
handshake, asks what tools exist, calls a few of the safe ones, and prints what
came back. No API key, no agent, no config file, nothing to edit.

What it calls, and what it refuses
----------------------------------
A rule picks the tools, not a guess: the name is split into words, any write
verb anywhere disqualifies it, and a read verb has to appear in the first two.
The same rule the published census used, for the same reason -- an earlier
version would have called kubectl_delete, cleanup and run_process, none of
which need arguments. A demo that deletes something on first run is worse than
no demo.

Arguments come from each tool's own schema, filled with the blandest value of
the right type. Anything whose schema cannot be satisfied that way is skipped
and said so, rather than being called with a guess.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_SERVER = ["npx", "-y", "@modelcontextprotocol/server-everything"]
PROTOCOL = "2024-11-05"
MAX_CALLS = 6
REPLY_TIMEOUT = 25.0
START_TIMEOUT = 90.0        # npx may be downloading the package on first run

# Same rule as the census. A write verb anywhere disqualifies; a read verb has
# to be one of the first two words.
READ_VERBS = frozenset({
    "get", "list", "read", "fetch", "search", "find", "query", "show", "describe",
    "view", "inspect", "status", "info", "count", "check", "echo", "ping", "help",
    "summarize", "summarise", "resolve", "lookup", "explain", "print", "add",
    "calculate", "evaluate", "convert", "version",
})
WRITE_VERBS = frozenset({
    "create", "write", "update", "delete", "remove", "destroy", "drop", "put",
    "post", "patch", "send", "publish", "deploy", "execute", "exec", "run",
    "apply", "install", "uninstall", "kill", "stop", "start", "restart", "move",
    "rename", "copy", "upload", "download", "set", "reset", "clear", "purge",
    "clean", "cleanup", "modify", "edit", "insert", "append", "commit", "push",
    "merge", "revert", "rollback", "grant", "revoke", "invite", "pay", "charge",
})

_WORDS = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


def words(name: str) -> List[str]:
    return [w.lower() for w in _WORDS.split(name or "") if w]


# A read can still be the wrong thing to call on a stranger's machine. The demo
# records what comes back, so a tool that returns the environment or a secret
# store would put the user's credentials in their own log. Refused by name.
SENSITIVE_WORDS = frozenset({
    "env", "environ", "environment", "secret", "secrets", "credential",
    "credentials", "password", "passwords", "token", "tokens", "key", "keys",
    "cookie", "cookies",
})


def is_safe(name: str) -> bool:
    """True only for names that read like a read, and touch nothing sensitive."""
    parts = words(name)
    if not parts:
        return False
    if any(part in WRITE_VERBS for part in parts):
        return False
    if any(part in SENSITIVE_WORDS for part in parts):
        return False
    return any(part in READ_VERBS for part in parts[:2])


def arguments_for(schema: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The blandest arguments that satisfy a tool's required fields.

    None means the schema asks for something this cannot invent -- an enum with
    no values, a nested object, a type nobody declared. Skipping is the right
    answer there: a demo should not send a guess to a stranger's server.
    """
    if not isinstance(schema, dict):
        return {}
    required = schema.get("required") or []
    properties = schema.get("properties") or {}
    if not isinstance(required, list) or not isinstance(properties, dict):
        return {}

    out: Dict[str, Any] = {}
    for field in required:
        spec = properties.get(field)
        if not isinstance(spec, dict):
            return None
        choices = spec.get("enum")
        if isinstance(choices, list):
            if not choices:
                return None
            out[field] = choices[0]
            continue
        kind = spec.get("type")
        if kind == "string":
            out[field] = "callwitness demo"
        elif kind in ("number", "integer"):
            out[field] = 1
        elif kind == "boolean":
            out[field] = False
        elif kind == "array":
            out[field] = []
        elif kind == "object":
            out[field] = {}
        else:
            return None
    return out


class Session(object):
    """A minimal MCP client over the proxy's stdio, and nothing more.

    Reading happens on its own thread because a pipe read cannot be given a
    deadline portably -- select does not work on Windows pipes, and a demo that
    hangs forever on a server that never answers is the failure this command
    exists to prevent.
    """

    def __init__(self, command: List[str]):
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=1, universal_newlines=True,
            encoding="utf-8", errors="replace")
        self.replies: Dict[int, Dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.stderr: List[str] = []
        self.closed = False
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("id") is not None:
                with self.lock:
                    self.replies[message["id"]] = message
        self.closed = True

    def _read_stderr(self) -> None:
        for line in self.process.stderr:
            self.stderr.append(line.rstrip())
            if len(self.stderr) > 200:
                del self.stderr[:100]

    def send(self, message: Dict[str, Any]) -> None:
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def call(self, request_id: int, method: str, params: Dict[str, Any],
             timeout: float = REPLY_TIMEOUT) -> Optional[Dict[str, Any]]:
        self.send({"jsonrpc": "2.0", "id": request_id,
                   "method": method, "params": params})
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                if request_id in self.replies:
                    return self.replies.pop(request_id)
            if self.closed and self.process.poll() is not None:
                return None
            time.sleep(0.05)
        return None

    def close(self) -> None:
        try:
            self.process.stdin.close()
        except Exception:
            pass
        try:
            self.process.wait(timeout=5)
        except Exception:
            self.process.kill()


def _child_command(home: Optional[str], server: List[str],
                   label: str) -> List[str]:
    base = [sys.executable, "-m", "callwitness.cli"]
    if home:
        base += ["--home", str(home)]
    return base + ["run", "--label", label, "--"] + list(server)


def human(size: int) -> str:
    if size >= 1024 * 1024:
        return "{:.1f} MB".format(size / (1024.0 * 1024.0))
    if size >= 1024:
        return "{:.1f} KB".format(size / 1024.0)
    return "{} B".format(size)


def run(server: Optional[List[str]] = None, home: Optional[str] = None,
        max_calls: int = MAX_CALLS) -> Tuple[int, List[str]]:
    """Drive one server through the proxy. Returns (calls made, output lines)."""
    server = list(server or DEFAULT_SERVER)
    label = re.sub(r"[^A-Za-z0-9_.-]+", "-", server[-1]).strip("-") or "demo"
    lines: List[str] = []

    lines.append("")
    lines.append("  starting: {}".format(" ".join(server)))
    lines.append("  through:  callwitness run")
    lines.append("")

    try:
        session = Session(_child_command(home, server, label))
    except Exception as exc:
        lines.append("  could not start it: {}".format(exc))
        return 0, lines

    try:
        hello = session.call(1, "initialize", {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "callwitness-demo", "version": "1"},
        }, timeout=START_TIMEOUT)

        if hello is None:
            lines.append("  the server never completed the handshake.")
            if session.stderr:
                lines.append("  it said:")
                for line in session.stderr[-6:]:
                    lines.append("    {}".format(line))
            return 0, lines

        name = (((hello.get("result") or {}).get("serverInfo") or {})
                .get("name") or "the server")
        session.send({"jsonrpc": "2.0", "method": "notifications/initialized",
                      "params": {}})

        listing = session.call(2, "tools/list", {})
        tools = ((listing or {}).get("result") or {}).get("tools") or []
        if not isinstance(tools, list) or not tools:
            lines.append("  {} declared no tools, so there is nothing to call."
                         .format(name))
            return 0, lines

        safe, refused = [], 0
        for tool in tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                continue
            if is_safe(tool["name"]):
                safe.append(tool)
            else:
                refused += 1

        lines.append("  {} declared {} tools. {} read like reads; {} refused by the"
                     .format(name, len(tools), len(safe), refused))
        lines.append("  safety rule and never called.")
        lines.append("")

        made = 0
        for tool in safe:
            if made >= max_calls:
                break
            arguments = arguments_for(tool.get("inputSchema"))
            if arguments is None:
                continue
            reply = session.call(100 + made, "tools/call",
                                 {"name": tool["name"], "arguments": arguments})
            made += 1
            if reply is None:
                lines.append("    {:<28} no answer".format(tool["name"][:28]))
                continue
            payload = reply.get("error") if reply.get("error") else reply.get("result")
            size = len(json.dumps(payload, ensure_ascii=False,
                                  default=str).encode("utf-8"))
            note = "error" if reply.get("error") else ""
            lines.append("    {:<28} {:>9}  {}".format(
                tool["name"][:28], human(size), note))

        lines.append("")
        if made:
            lines.append("  {} calls recorded. Nothing was blocked, nothing was".format(made))
            lines.append("  altered -- the proxy forwards every byte and writes down")
            lines.append("  what it saw.")
        else:
            lines.append("  Nothing was safely callable without inventing arguments.")
        return made, lines
    finally:
        session.close()
