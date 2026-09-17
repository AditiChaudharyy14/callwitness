"""A tiny MCP server, so the command a stranger runs first can be tested.

Not a fixture for convenience. `callwitness demo` spawns a process, speaks a
protocol to it across pipes, reads on background threads and gives up on
deadlines -- none of which a unit test of the pure functions touches, and all
of which behave differently on Windows than on anything else. Driving a real
server through the real proxy is the only way to find that out here rather
than from a stranger's bug report.

Python, because the alternative is npx, and a test that downloads a package
from the internet is a test that fails on a train.

Run as:  python stub_server.py [called-log-path] [--die] [--silent]

    --die     exit immediately, to test a server that never handshakes
    --silent  read everything, answer nothing, to test the deadline

If given a log path it appends the name of every tool actually invoked, which
is how the tests prove the dangerous ones were never called -- the safety rule
returning False is not the same claim as nothing having run.
"""

import json
import sys

TOOLS = [
    # Safe, no required arguments.
    {"name": "get_thing", "description": "reads a thing",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},

    # Safe, one required string the generator can fill.
    {"name": "list_items", "description": "lists items",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string"}},
                     "required": ["query"]}},

    # Must never be called. Takes no arguments, so nothing but the rule stops it.
    {"name": "delete_everything", "description": "deletes everything",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},

    # Also must never be called: reads like a read until the second word.
    {"name": "get_and_drop_table", "description": "a trap",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},

    # Safe, but the schema cannot be satisfied without inventing a value.
    {"name": "read_weird", "description": "unsatisfiable",
     "inputSchema": {"type": "object",
                     "properties": {"x": {"type": "kitten"}},
                     "required": ["x"]}},
]


def main():
    args = sys.argv[1:]
    if "--die" in args:
        return 0
    silent = "--silent" in args
    log = next((a for a in args if not a.startswith("--")), None)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue

        request_id = message.get("id")
        if request_id is None:      # a notification; nothing to answer
            continue
        if silent:
            continue

        method = message.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05",
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "stub", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            name = (message.get("params") or {}).get("name") or "?"
            if log:
                with open(log, "a") as handle:
                    handle.write(name + "\n")
            result = {"content": [{"type": "text", "text": "x" * 100}]}
        else:
            result = None

        if result is None:
            reply = {"jsonrpc": "2.0", "id": request_id,
                     "error": {"code": -32601, "message": "no such method"}}
        else:
            reply = {"jsonrpc": "2.0", "id": request_id, "result": result}

        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
