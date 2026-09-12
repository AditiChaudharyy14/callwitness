"""What may leave the machine, and what may not.

Every other test in this suite protects the user's agent. These protect the
user. A bug here does not crash anything -- it quietly sends someone's
filenames to a stranger, and nobody notices because the feature worked.

So the important tests are the negative ones: given a database full of
realistic secrets, assert that none of them appear anywhere in the payload.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from callwitness import contribute as c


# -- naming ---------------------------------------------------------------

@pytest.mark.parametrize("command,expected", [
    ("npx -y @modelcontextprotocol/server-filesystem /home/aditi/Downloads",
     "npm:@modelcontextprotocol/server-filesystem"),
    ("npx -y chrome-devtools-mcp@latest", "npm:chrome-devtools-mcp"),
    ("npx -y @playwright/mcp@1.2.3", "npm:@playwright/mcp"),
    ("bunx -y some-server", "npm:some-server"),
    ("uvx mcp-server-git --repository /srv/acme", "pypi:mcp-server-git"),
    ("pipx run mcp-server-time", "pypi:mcp-server-time"),
])
def test_public_packages_are_named(command, expected):
    """A published package identifies software, and that is the useful part."""
    assert c.package_of(command) == expected


@pytest.mark.parametrize("command", [
    "/opt/acme/bin/internal-mcp --config /etc/acme/prod.yaml",
    "python -m acme.servers.billing",
    "./our-server.js",
    "node /home/priya/work/customer-db-server.js",
    "npx -y /opt/acme/local-server",     # a path handed to npx is still a path
    "",
])
def test_private_commands_are_unlisted(command):
    """A path identifies an organisation. Those never leave."""
    assert c.package_of(command) == "unlisted"


def test_versions_do_not_fragment_the_baseline():
    """@latest and @1.2.3 are the same package and must aggregate together."""
    assert (c.package_of("npx -y chrome-devtools-mcp@latest") ==
            c.package_of("npx -y chrome-devtools-mcp@0.9.1"))


def test_tool_pseudonyms_are_stable_and_meaningless():
    names = ["approve_wire_transfer", "fetch_patient_record", "list_accounts"]
    mapping = c.anonymise_tools(names)
    assert sorted(mapping.values()) == ["tool_1", "tool_2", "tool_3"]
    assert c.anonymise_tools(names) == c.anonymise_tools(list(reversed(names)))
    for original in names:
        assert original not in mapping.values()


# -- statistics -----------------------------------------------------------

def test_percentiles_report_values_that_occurred():
    """Interpolation would invent a byte count nobody measured."""
    values = [10, 20, 30, 40, 1000]
    for q in (0.0, 0.5, 0.95, 1.0):
        assert c.percentile(values, q) in values


def test_spread_of_nothing_is_zeroes_not_a_crash():
    assert c.spread([]) == {"min": 0, "p50": 0, "p95": 0, "max": 0}


# -- the payload ----------------------------------------------------------

SECRETS = {
    "path": "/home/aditi/Documents/investors/term-sheet-draft.pdf",
    "host": "billing-prod-3.internal.acme.example",
    "key": "sk-live-4eC39HqLyjWDarjtT1zdp7dc",
    "tool": "approve_wire_transfer",
    "email": "cfo@acme.example",
    "content": "Wire 40000 USD to account 8837221 on Friday",
}


def seed(home: Path) -> None:
    """A database that looks like a real, slightly alarming deployment."""
    home.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(home / "callwitness.db"))
    db.executescript("""
        CREATE TABLE sessions (session_id TEXT PRIMARY KEY, label TEXT,
                               command TEXT, started_at TEXT, ended_at TEXT,
                               exit_code INTEGER);
        CREATE TABLE calls (id INTEGER PRIMARY KEY AUTOINCREMENT,
                            session_id TEXT, label TEXT, ts TEXT, tool TEXT,
                            args_json TEXT, args_bytes INTEGER,
                            args_truncated INTEGER, signals_json TEXT,
                            redaction_json TEXT, seq INTEGER, prev_hash TEXT,
                            hash TEXT, duration_ms REAL, is_error INTEGER,
                            result_bytes INTEGER, result_preview TEXT);
    """)
    db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)",
               ("s1", "fs", "npx -y @modelcontextprotocol/server-filesystem "
                + SECRETS["path"], "2026-09-12T09:00:00Z", None, 0))
    db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)",
               ("s2", "billing", "/opt/acme/bin/billing-mcp --host "
                + SECRETS["host"], "2026-09-12T10:00:00Z", None, 0))

    for i, size in enumerate([180, 4210, 334987]):
        db.execute(
            "INSERT INTO calls (session_id, label, ts, tool, args_json, "
            "args_bytes, duration_ms, is_error, result_bytes, result_preview) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("s1", "fs", "2026-09-12T09:0{}:00Z".format(i), "list_directory",
             json.dumps({"path": SECRETS["path"]}), 120, 12.0, 0, size,
             SECRETS["content"]))
    db.execute(
        "INSERT INTO calls (session_id, label, ts, tool, args_json, "
        "args_bytes, duration_ms, is_error, result_bytes, result_preview) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("s2", "billing", "2026-09-12T10:00:00Z", SECRETS["tool"],
         json.dumps({"to": SECRETS["email"], "key": SECRETS["key"]}),
         240, 88.0, 0, 512, SECRETS["content"]))
    db.commit()
    db.close()


def test_no_secret_appears_anywhere_in_the_payload(tmp_path):
    """The test this whole module exists for.

    Not "the fields we expect are absent" -- that only catches the leaks
    somebody thought of. Serialise the entire payload and assert that no
    sensitive string from a realistic database appears anywhere in it.
    """
    seed(tmp_path)
    blob = json.dumps(c.build_payload(tmp_path, "test-install"))
    for label, secret in SECRETS.items():
        assert secret not in blob, "leaked {}".format(label)
    # and the pieces, not just the whole strings
    for fragment in ("acme", "aditi", "Documents", "investors", "8837221",
                     "sk-live", "wire", "patient"):
        assert fragment.lower() not in blob.lower(), "leaked {!r}".format(fragment)


def test_public_tool_names_survive_and_private_ones_do_not(tmp_path):
    seed(tmp_path)
    payload = c.build_payload(tmp_path, "test-install")
    by_package = {s["package"]: s for s in payload["servers"]}

    public = by_package["npm:@modelcontextprotocol/server-filesystem"]
    assert [t["tool"] for t in public["tools"]] == ["list_directory"]

    private = by_package["unlisted"]
    assert [t["tool"] for t in private["tools"]] == ["tool_1"]


def test_sizes_are_preserved_because_they_are_the_point(tmp_path):
    seed(tmp_path)
    payload = c.build_payload(tmp_path, "test-install")
    fs = next(s for s in payload["servers"] if s["package"].startswith("npm:"))
    returned = fs["tools"][0]["returned_bytes"]
    assert returned["min"] == 180
    assert returned["max"] == 334987
    assert fs["tools"][0]["calls"] == 3


def test_payload_carries_no_field_the_spec_does_not_define(tmp_path):
    seed(tmp_path)
    assert c.audit(c.build_payload(tmp_path, "test-install")) == []


def test_audit_catches_a_field_somebody_added_quietly():
    payload = {"schema": c.SCHEMA, "install": "x", "version": "0",
               "platform": "linux", "python": "3.11",
               "window": {}, "servers": [], "hostname": "prod-3"}
    assert "payload.hostname" in c.audit(payload)


def test_send_refuses_a_payload_that_fails_the_audit(monkeypatch):
    monkeypatch.setenv(c.ENV_URL, "https://example.invalid/v1")
    ok, message = c.send({"schema": c.SCHEMA, "servers": [], "secrets": "oops"})
    assert not ok
    assert "secrets" in message


def test_send_without_a_collector_sends_nothing(monkeypatch):
    """A build with no endpoint must say so, not fail at a dead host."""
    monkeypatch.delenv(c.ENV_URL, raising=False)
    monkeypatch.setattr(c, "CONTRIBUTE_URL", None)
    ok, message = c.send({"schema": c.SCHEMA, "servers": []})
    assert not ok
    assert "Nothing has left this machine" in message


# -- the switch -----------------------------------------------------------

def test_off_by_default(tmp_path):
    assert c.load_config(tmp_path).get("enabled") is False


def test_a_missing_database_is_an_empty_payload_not_a_crash(tmp_path):
    payload = c.build_payload(tmp_path, "test-install")
    assert payload["servers"] == []
    assert c.audit(payload) == []


def test_window_covers_only_what_is_being_sent(tmp_path):
    """So a weekly contributor and a monthly one are not double-counted."""
    seed(tmp_path)
    full = c.build_payload(tmp_path, "i")
    later = c.build_payload(tmp_path, "i", since="2026-09-12T09:01:00Z")
    assert full["window"]["from"] < later["window"]["from"]
    assert (sum(t["calls"] for s in later["servers"] for t in s["tools"]) <
            sum(t["calls"] for s in full["servers"] for t in s["tools"]))


# -- the version stamp ----------------------------------------------------

def test_the_reported_version_is_the_one_in_pyproject():
    """It drifted once and nobody noticed for a whole release.

    pyproject.toml said 0.1.1 while __init__.py said 0.1.0, so `--version` and
    every contributed record named a version that was not running. The number
    now comes from installed package metadata; this test fails if a second copy
    of it ever reappears and disagrees.
    """
    import re
    from pathlib import Path

    import callwitness

    root = Path(__file__).resolve().parents[1]
    declared = re.search(r'^version\s*=\s*"([^"]+)"',
                         (root / "pyproject.toml").read_text(encoding="utf-8"),
                         re.M).group(1)
    assert callwitness.__version__ == declared


def test_the_payload_stamps_the_running_version(tmp_path):
    """A baseline keyed on the wrong version is worse than one with no version."""
    import callwitness

    seed(tmp_path)
    assert c.build_payload(tmp_path, "i")["version"] == callwitness.__version__