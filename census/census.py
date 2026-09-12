"""Measure what MCP servers declare, and what they actually hand back.

    python census/census.py --out census/data

Every published measurement of MCP's "context tax" counts `tools/list` -- the
menu. None of them counts what a tool returns at runtime -- the meal -- because
none of them run the servers. This does, through callwitness, so the numbers
come with a tamper-evident recording of the traffic they were derived from.

That distinction is the whole finding. A server's declared schema is a few
thousand tokens and it is the same on every machine. A single call can return
two orders of magnitude more than that, it varies by the arguments, and nobody
has written the distribution down.

Design notes
------------
Requests go one at a time and the reply is read before the next is sent. Firing
them all at stdin would be faster and would make per-call latency meaningless,
and latency is one of the columns.

Every server runs behind `callwitness run`, not directly. That is deliberate:
the census should be produced by the instrument it is advertising, and a run
that cannot be reproduced through the published tool is not evidence of
anything. It also means every census run leaves a hash-chained log.

Nothing here needs credentials, a model, or a paid API. That is the point --
it is a study that money cannot buy an advantage in.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import queue
import re
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

PROTOCOL = "2024-11-05"

# Language in a tool description that addresses the model rather than describing
# the tool. The model reads descriptions as part of its instructions, so a
# description that says "always call this first" is not documentation -- it is a
# prompt fragment written by whoever published the package, and nobody has
# counted how many there are. Deliberately conservative: these are flags for a
# human to read, never an accusation on their own.
DIRECTIVE = [
    (re.compile(r"\byou (?:must|should|need to|have to)\b", re.I), "you-must"),
    (re.compile(r"\b(?:always|never) (?:call|use|invoke|run)\b", re.I), "always-call"),
    (re.compile(r"\bdo not (?:use|call|mention|reveal|tell)\b", re.I), "do-not"),
    (re.compile(r"\b(?:ignore|disregard|override)\b", re.I), "ignore"),
    (re.compile(r"\b(?:before|after) (?:calling|using|any other)\b", re.I), "ordering"),
    (re.compile(r"\bprefer this (?:tool|over)\b", re.I), "prefer-this"),
    (re.compile(r"\binstead of\b", re.I), "instead-of"),
    (re.compile(r"\bIMPORTANT\b"), "shouting"),
    (re.compile(r"\b(?:system|assistant) (?:prompt|message)\b", re.I), "names-the-prompt"),
]


def flags_for(text: str) -> List[str]:
    return sorted({name for pattern, name in DIRECTIVE if pattern.search(text or "")})


class Server:
    """One MCP server under a callwitness proxy, spoken to one message at a time."""

    def __init__(self, name: str, argv: List[str], label: Optional[str] = None,
                 env: Optional[Dict[str, str]] = None) -> None:
        self.name = name
        self.argv = argv
        self.label = label or name
        self.env = env or {}
        self.proc: Optional[subprocess.Popen] = None
        self.lines: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self.stderr_tail: List[str] = []

    # -- process -----------------------------------------------------------

    def start(self) -> None:
        command = ["callwitness", "run", "--label", self.label, "--"] + self.argv
        environ = dict(os.environ)
        environ.update(self.env)
        # Servers that write UTF-8 to a Windows console otherwise die on the
        # first non-ASCII byte in a filename, which is not a finding about the
        # server.
        environ.setdefault("PYTHONIOENCODING", "utf-8")
        self.proc = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0, env=environ,
        )
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self) -> None:
        try:
            for line in iter(self.proc.stdout.readline, b""):
                self.lines.put(line)
        except Exception:
            pass
        finally:
            self.lines.put(None)

    def _pump_stderr(self) -> None:
        # Kept, not printed. A server's startup chatter is noise during the run
        # and the only thing that explains a failure afterwards.
        try:
            for line in iter(self.proc.stderr.readline, b""):
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    self.stderr_tail.append(text)
                    del self.stderr_tail[:-12]
        except Exception:
            pass

    def stop(self) -> None:
        if not self.proc:
            return
        for step in (self.proc.terminate, self.proc.kill):
            try:
                step()
                self.proc.wait(timeout=5)
                return
            except Exception:
                continue

    # -- protocol ----------------------------------------------------------

    def send(self, message: Dict[str, Any]) -> None:
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        self.proc.stdin.flush()

    def await_id(self, want: int, timeout: float) -> Tuple[Optional[Dict[str, Any]], int]:
        """Read until the reply with this id arrives. Returns (message, bytes).

        The byte count is of the raw line as it crossed the wire, because that
        is what the model's context is charged for -- not the pretty-printed
        re-serialisation.
        """
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None, 0
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                return None, 0
            if line is None:            # stdout closed: the server is gone
                return None, 0
            text = line.decode("utf-8", "replace").strip()
            if not text.startswith("{"):
                continue
            try:
                message = json.loads(text)
            except Exception:
                continue
            if message.get("id") == want:
                return message, len(line)


def plausible_arguments(schema: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Arguments for a tool we know nothing about, or None to skip it.

    Only tools whose required fields we can honestly fill get called. Inventing
    a path or an API key produces an error response, and an error response has
    a byte count, which would land in the dataset looking like a measurement.
    """
    if not isinstance(schema, dict):
        return None
    required = schema.get("required") or []
    if not required:
        return {}
    properties = schema.get("properties") or {}
    arguments: Dict[str, Any] = {}
    for field in required:
        spec = properties.get(field) or {}
        kind = spec.get("type")
        if "enum" in spec and spec["enum"]:
            arguments[field] = spec["enum"][0]
        elif kind == "boolean":
            arguments[field] = False
        elif kind in ("number", "integer"):
            arguments[field] = 1
        elif kind == "array":
            arguments[field] = []
        else:
            return None   # a string we would have to invent. Skip it.
    return arguments


def census_one(spec: Dict[str, Any], timeout: float, call_timeout: float,
               max_calls: int) -> Dict[str, Any]:
    name = spec["name"]
    server = Server(name, spec["command"], spec.get("label"), spec.get("env"))
    row: Dict[str, Any] = {
        "server": name, "command": spec["command"], "package": spec.get("package"),
        "started": False, "error": None, "declared_bytes": 0, "tool_count": 0,
        "description_bytes": 0, "directive_tools": [], "calls": [],
    }

    try:
        server.start()
    except Exception as exc:
        row["error"] = "launch: {}".format(exc)
        return row

    try:
        server.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "callwitness-census", "version": "1"}}})
        hello, _ = server.await_id(1, timeout)
        if hello is None:
            row["error"] = "no response to initialize"
            row["stderr"] = server.stderr_tail[-4:]
            return row
        row["started"] = True
        row["server_info"] = (hello.get("result") or {}).get("serverInfo")

        server.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        server.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listing, declared_bytes = server.await_id(2, timeout)
        if listing is None or "result" not in listing:
            row["error"] = "no tools/list"
            row["stderr"] = server.stderr_tail[-4:]
            return row

        tools = (listing["result"] or {}).get("tools") or []
        row["declared_bytes"] = declared_bytes
        row["tool_count"] = len(tools)
        row["description_bytes"] = sum(
            len((t.get("description") or "").encode()) for t in tools)
        for tool in tools:
            found = flags_for(tool.get("description") or "")
            if found:
                row["directive_tools"].append({"tool": tool.get("name"), "flags": found})

        # Explicit calls from the catalogue first: those are the ones chosen to
        # be interesting. Then anything else that can be called honestly.
        planned: List[Tuple[str, Dict[str, Any], str]] = [
            (c["tool"], c.get("arguments") or {}, "catalogue")
            for c in spec.get("calls") or []
        ]
        named = {t for t, _, _ in planned}
        for tool in tools:
            if len(planned) >= max_calls:
                break
            tool_name = tool.get("name")
            if not tool_name or tool_name in named:
                continue
            arguments = plausible_arguments(tool.get("inputSchema") or {})
            if arguments is None:
                continue
            planned.append((tool_name, arguments, "auto"))

        for index, (tool_name, arguments, origin) in enumerate(planned):
            request_id = 100 + index
            started = time.time()
            server.send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                         "params": {"name": tool_name, "arguments": arguments}})
            reply, returned_bytes = server.await_id(request_id, call_timeout)
            elapsed_ms = int((time.time() - started) * 1000)
            if reply is None:
                row["calls"].append({"tool": tool_name, "origin": origin,
                                     "timeout": True, "ms": elapsed_ms})
                break   # the pipe is out of step; nothing after this is trustworthy
            result = reply.get("result") or {}
            row["calls"].append({
                "tool": tool_name,
                "origin": origin,
                "arguments_bytes": len(json.dumps(arguments).encode()),
                "returned_bytes": returned_bytes,
                "ms": elapsed_ms,
                # isError is the server saying "this went wrong" inside a normal
                # reply. Those bytes are real -- the model still reads them --
                # but they are not a measurement of the tool working, so they
                # are counted separately rather than dropped.
                "is_error": bool(result.get("isError")) or "error" in reply,
            })
    except Exception as exc:
        row["error"] = "{}: {}".format(type(exc).__name__, exc)
    finally:
        server.stop()
    return row


def substitute(value: Any, root: str) -> Any:
    """Replace CENSUS_ROOT everywhere it appears in the catalogue.

    Servers that touch the filesystem need somewhere real to look. Keeping that
    one path out of the catalogue means the file can be published as-is and
    re-run by anyone against their own machine, which is the difference between
    a dataset and a screenshot.
    """
    if isinstance(value, str):
        return value.replace("CENSUS_ROOT", root)
    if isinstance(value, list):
        return [substitute(v, root) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, root) for k, v in value.items()}
    return value


def load_catalogue(path: pathlib.Path, root: str) -> List[Dict[str, Any]]:
    servers = json.loads(path.read_text(encoding="utf-8"))["servers"]
    return [substitute(s, root) for s in servers if not s.get("skip")]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--catalogue", default="census/servers.json")
    parser.add_argument("--out", default="census/data")
    parser.add_argument("--root", default=str(pathlib.Path.home() / "Downloads"),
                        help="a real folder for filesystem-shaped servers to read. "
                             "Point it somewhere with actual content -- a folder of "
                             "twelve files measures your test fixture, not the "
                             "filesystems people have")
    parser.add_argument("--only", help="substring: run just the servers matching it")
    parser.add_argument("--timeout", type=float, default=180.0,
                        help="seconds for the handshake; the first run of an "
                             "npx server downloads it, which is slow and is not "
                             "a property of the server")
    parser.add_argument("--call-timeout", type=float, default=60.0)
    parser.add_argument("--max-calls", type=int, default=12,
                        help="per server, so one chatty server cannot dominate the run")
    args = parser.parse_args()

    catalogue = pathlib.Path(args.catalogue)
    if not catalogue.is_file():
        print("no catalogue at {} -- run from the repository root".format(catalogue))
        return 2
    root = str(pathlib.Path(args.root).expanduser().resolve())
    if not pathlib.Path(root).is_dir():
        print("--root {} is not a folder".format(root))
        return 2
    servers = load_catalogue(catalogue, root)
    if args.only:
        servers = [s for s in servers if args.only.lower() in s["name"].lower()]
    if not servers:
        print("nothing to run")
        return 2

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    dataset = out / "census.jsonl"

    print("{} servers, writing to {}\n".format(len(servers), dataset))
    print("{:<24}{:>7}{:>11}{:>9}{:>11}".format(
        "server", "tools", "declared", "calls", "returned"))
    print("-" * 62)

    with dataset.open("w", encoding="utf-8") as handle:
        for spec in servers:
            row = census_one(spec, args.timeout, args.call_timeout, args.max_calls)
            row["measured_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            handle.write(json.dumps(row) + "\n")
            handle.flush()   # a run that dies at server 28 still has 27 servers

            if row["error"]:
                print("{:<24}{:>7}   {}".format(row["server"], "--", row["error"][:32]))
                continue
            good = [c for c in row["calls"] if not c.get("timeout") and not c.get("is_error")]
            returned = sum(c["returned_bytes"] for c in good)
            print("{:<24}{:>7}{:>11}{:>9}{:>11}".format(
                row["server"], row["tool_count"], row["declared_bytes"],
                len(good), returned))

    print("\nwrote {}".format(dataset))
    print("now run:  python census/report.py --data {}".format(dataset))
    return 0


if __name__ == "__main__":
    sys.exit(main())
