#!/usr/bin/env python3
"""A minimal fake MCP server, for the demo and the end-to-end tests.

Speaks just enough of the protocol to be proxied: newline-delimited JSON-RPC
on stdin/stdout.
"""

import json
import sys


def respond(msg):
    method = msg.get("method")
    if method == "initialize":
        return {"result": {"protocolVersion": "2024-11-05",
                           "serverInfo": {"name": "mock", "version": "0"}}}
    if method == "tools/list":
        return {"result": {"tools": [
            {"name": "send_email"}, {"name": "read_db"}, {"name": "explode"},
        ]}}
    if method == "tools/call":
        name = (msg.get("params") or {}).get("name")
        if name == "explode":
            return {"error": {"code": -32000, "message": "boom"}}
        return {"result": {"content": [{"type": "text", "text": f"ok:{name}"}]}}
    return {"result": {}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" not in msg:
            continue
        out = {"jsonrpc": "2.0", "id": msg["id"], **respond(msg)}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
