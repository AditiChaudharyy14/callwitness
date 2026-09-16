"""Wrapping someone else's config file: the step where people used to give up.

These lean hard on the destructive cases. This command edits a file the user
did not write and cannot easily repair, and a broken MCP config means a broken
agent -- which is the one outcome this whole project promises never to cause.
So the tests care less about the happy path than about: does it refuse when it
should, does it back up, does it round-trip, and does it leave alone what it
does not understand.
"""

import json
import tempfile
from pathlib import Path

import pytest

from callwitness.install import (
    apply, discover, format_plan, is_wrapped, plan, unwrap, wrap,
)

FILESYSTEM = {"command": "npx", "args": ["-y", "@mcp/server-filesystem", "/data"]}
GIT = {"command": "uvx", "args": ["mcp-server-git"]}
REMOTE = {"url": "https://mcp.acme.com/mcp"}
WRAPPED = {"command": "callwitness", "args": ["run", "--label", "fs", "--", "npx", "-y", "x"]}


def _config(servers, key="mcpServers"):
    path = Path(tempfile.mkdtemp()) / "mcp.json"
    path.write_text(json.dumps({key: servers}, indent=2), encoding="utf-8")
    return path


def _servers(path):
    # utf-8-sig for the same reason the code under test uses it: some of these
    # fixtures carry a BOM, and a helper that cannot read them would fail tests
    # about behaviour that is actually correct.
    return json.loads(path.read_bytes().decode("utf-8-sig"))["mcpServers"]


# -- the wrap itself -------------------------------------------------------

def test_a_plain_server_is_wrapped_with_its_command_preserved():
    out = wrap("filesystem", FILESYSTEM)
    assert out["command"] == "callwitness"
    assert out["args"] == ["run", "--label", "filesystem", "--",
                           "npx", "-y", "@mcp/server-filesystem", "/data"]


def test_other_keys_in_the_entry_survive_the_wrap():
    """A config may carry env, cwd, disabled -- losing them silently is a bug."""
    entry = dict(FILESYSTEM, env={"TOKEN": "x"}, cwd="/srv")
    out = wrap("fs", entry)
    assert out["env"] == {"TOKEN": "x"}
    assert out["cwd"] == "/srv"


def test_wrapping_is_idempotent():
    assert wrap("fs", WRAPPED) is None
    assert is_wrapped(WRAPPED)


def test_a_dev_install_invocation_counts_as_wrapped():
    entry = {"command": "python", "args": ["-m", "callwitness.cli", "run", "--", "npx"]}
    assert is_wrapped(entry)


def test_unwrap_restores_the_original_exactly():
    assert unwrap(wrap("fs", FILESYSTEM)) == FILESYSTEM
    assert unwrap(wrap("git", GIT)) == GIT


def test_unwrap_drops_the_args_key_when_there_were_none():
    entry = {"command": "server"}
    assert unwrap(wrap("s", entry)) == entry


def test_unwrap_refuses_a_shape_it_did_not_produce():
    assert unwrap({"command": "callwitness", "args": ["stats"]}) is None


# -- planning --------------------------------------------------------------

def test_plan_changes_nothing_on_disk():
    path = _config({"filesystem": FILESYSTEM})
    before = path.read_text()
    report = plan(path)
    assert report["change"]
    assert path.read_text() == before


def test_a_remote_server_is_skipped_with_the_command_it_would_need():
    """Inventing a port and rewriting their config is worse than saying so."""
    report = plan(_config({"api": REMOTE}))
    assert report["change"] == []
    name, why = report["skipped"][0]
    assert name == "api"
    assert "callwitness proxy" in why
    assert "https://mcp.acme.com/mcp" in why


def test_entries_that_are_not_objects_are_skipped_not_crashed_on():
    report = plan(_config({"weird": "just a string", "fs": FILESYSTEM}))
    assert [n for n, _ in report["skipped"]] == ["weird"]
    assert [n for n, _, _ in report["change"]] == ["fs"]


def test_the_servers_key_is_found_under_either_name():
    """Some clients use `servers` rather than `mcpServers`."""
    report = plan(_config({"fs": FILESYSTEM}, key="servers"))
    assert report["key"] == "servers"
    assert report["change"]


def test_a_config_without_a_server_section_is_reported_not_edited():
    path = Path(tempfile.mkdtemp()) / "mcp.json"
    path.write_text('{"theme": "dark"}', encoding="utf-8")
    report = plan(path)
    assert "no server map found" in report["error"]
    assert report["change"] == []


def test_malformed_json_is_reported_rather_than_raised():
    path = Path(tempfile.mkdtemp()) / "mcp.json"
    path.write_text("{ not json", encoding="utf-8")
    report = plan(path)
    assert "could not read as JSON" in report["error"]


# -- applying --------------------------------------------------------------

def test_apply_writes_a_backup_before_touching_anything():
    path = _config({"fs": FILESYSTEM})
    original = path.read_text()
    backup = apply(plan(path))
    assert backup is not None and backup.exists()
    assert backup.read_text() == original


def test_apply_leaves_untouched_servers_exactly_as_they_were():
    path = _config({"fs": FILESYSTEM, "api": REMOTE, "done": WRAPPED})
    apply(plan(path))
    after = _servers(path)
    assert after["api"] == REMOTE
    assert after["done"] == WRAPPED
    assert after["fs"]["command"] == "callwitness"


def test_install_then_uninstall_returns_the_file_to_its_meaning():
    servers = {"fs": FILESYSTEM, "git": GIT, "api": REMOTE}
    path = _config(dict(servers))
    apply(plan(path))
    assert _servers(path)["fs"]["command"] == "callwitness"
    apply(plan(path, undo=True))
    assert _servers(path) == servers


def test_a_second_install_changes_nothing():
    path = _config({"fs": FILESYSTEM})
    apply(plan(path))
    after_first = path.read_text()
    report = plan(path)
    assert report["change"] == []
    assert apply(report) is None
    assert path.read_text() == after_first


def test_apply_on_an_empty_plan_makes_no_backup():
    """Running install twice should not litter the directory with copies."""
    path = _config({"done": WRAPPED})
    assert apply(plan(path)) is None
    assert list(path.parent.glob("*backup*")) == []


# -- what the user reads ---------------------------------------------------

def test_the_dry_run_says_plainly_that_nothing_happened():
    out = format_plan([plan(_config({"fs": FILESYSTEM}))], undo=False, applied=False)
    assert "Nothing has been changed" in out
    assert "--apply" in out
    assert "uninstall" in out


def test_the_dry_run_shows_the_before_and_after_command():
    out = format_plan([plan(_config({"fs": FILESYSTEM}))], undo=False, applied=False)
    assert "- npx -y @mcp/server-filesystem /data" in out
    assert "+ callwitness run --label fs -- npx" in out


def test_applying_tells_the_user_to_restart_the_client():
    """The change is invisible until the client reloads; not saying so wastes an hour."""
    report = plan(_config({"fs": FILESYSTEM}))
    report["backup"] = apply(report)
    out = format_plan([report], undo=False, applied=True)
    assert "Restart the client" in out
    assert "backed up" in out


def test_no_configs_found_points_at_the_escape_hatch():
    out = format_plan([], undo=False, applied=False)
    assert "No MCP client configs found" in out
    assert "--config" in out


def test_discover_returns_only_paths_that_exist():
    for _name, path in discover():
        assert path.is_file()


# --------------------------------------------------------------------------
# Byte-order marks
#
# Found by running the command on Windows. PowerShell's `Out-File -Encoding
# utf8` writes a BOM, so do Notepad and several editors, and plain utf-8
# decoding raises on the first character. The user sees "could not read as
# JSON" about a file that is perfectly good JSON and concludes the tool is
# broken -- on the one command whose entire job is not breaking their config.
# --------------------------------------------------------------------------

import codecs


def _config_with_bom(servers):
    path = Path(tempfile.mkdtemp()) / "mcp.json"
    body = json.dumps({"mcpServers": servers}, indent=2).encode("utf-8")
    path.write_bytes(codecs.BOM_UTF8 + body)
    return path


def test_a_config_with_a_bom_is_read_not_rejected():
    report = plan(_config_with_bom({"fs": FILESYSTEM}))
    assert report["error"] is None
    assert [n for n, _, _ in report["change"]] == ["fs"]


def test_a_bom_survives_the_rewrite():
    """Silently changing the encoding of someone's config is not our business."""
    path = _config_with_bom({"fs": FILESYSTEM})
    apply(plan(path))
    assert path.read_bytes().startswith(codecs.BOM_UTF8)
    assert _servers(path)["fs"]["command"] == "callwitness"


def test_a_file_without_a_bom_does_not_gain_one():
    path = _config({"fs": FILESYSTEM})
    apply(plan(path))
    assert not path.read_bytes().startswith(codecs.BOM_UTF8)


def test_the_backup_of_a_bom_file_is_byte_identical():
    path = _config_with_bom({"fs": FILESYSTEM})
    original = path.read_bytes()
    backup = apply(plan(path))
    assert backup.read_bytes() == original


def test_install_uninstall_round_trips_through_a_bom():
    servers = {"fs": FILESYSTEM, "git": GIT}
    path = _config_with_bom(dict(servers))
    apply(plan(path))
    apply(plan(path, undo=True))
    assert _servers(path) == servers
    assert path.read_bytes().startswith(codecs.BOM_UTF8)
CLAUDE_CODE_NESTED = {
    "numStartups": 7,
    "projects": {
        "C:\\Users\\dev\\proj": {
            "mcpServers": {
                "playwright": {"command": "npx", "args": ["-y", "@playwright/mcp"]}
            },
            "allowedTools": [],
        }
    },
}

CLAUDE_CODE_BOTH = {
    "mcpServers": {"memory": {"command": "npx", "args": ["-y", "@mcp/memory"]}},
    "projects": {
        "/home/dev/a": {"mcpServers": {"fs": {"command": "npx", "args": ["-y", "@mcp/fs"]}}},
        "/home/dev/b": {"mcpServers": {}},
        "/home/dev/c": {"allowedTools": []},
    },
}


def _write(doc, name=".claude.json"):
    path = Path(tempfile.mkdtemp()) / name
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path


def _read(path):
    return json.loads(path.read_bytes().decode("utf-8-sig"))


# -- the bug ---------------------------------------------------------------

def test_a_server_registered_only_under_a_project_is_found():
    """The live failure: install said "nothing to wrap" while this server ran."""
    report = plan(_write(CLAUDE_CODE_NESTED))
    assert report["error"] is None
    assert len(report["change"]) == 1


def test_applying_writes_into_the_project_not_the_top_level():
    path = _write(CLAUDE_CODE_NESTED)
    apply(plan(path))
    doc = _read(path)
    entry = doc["projects"]["C:\\Users\\dev\\proj"]["mcpServers"]["playwright"]
    assert entry["command"] == "callwitness"
    assert "mcpServers" not in doc        # nothing invented at the top level
    assert doc["numStartups"] == 7        # unrelated keys survive


def test_every_map_in_one_file_is_walked():
    report = plan(_write(CLAUDE_CODE_BOTH))
    assert len(report["change"]) == 2     # the global one and the project one
    assert len(report["sections"]) == 3   # the empty map counts; the keyless project does not


def test_names_are_qualified_only_when_there_is_more_than_one_map():
    many = plan(_write(CLAUDE_CODE_BOTH))
    assert any("::" in name for name, _b, _a in many["change"])
    one = plan(_config({"filesystem": FILESYSTEM}))
    assert all("::" not in name for name, _b, _a in one["change"])


# -- round trip ------------------------------------------------------------

def test_wrap_then_unwrap_restores_the_nested_document():
    """Uninstall has to reverse install exactly, or people cannot risk trying it."""
    path = _write(CLAUDE_CODE_NESTED)
    before = _read(path)
    apply(plan(path))
    apply(plan(path, undo=True))
    assert _read(path) == before


def test_wrap_then_unwrap_restores_a_file_with_several_maps():
    path = _write(CLAUDE_CODE_BOTH)
    before = _read(path)
    apply(plan(path))
    apply(plan(path, undo=True))
    assert _read(path) == before


# -- the empty answer ------------------------------------------------------

def test_nothing_to_wrap_says_what_was_checked():
    """Three different situations used to print the same sentence."""
    path = _config({"fs": WRAPPED})
    out = format_plan([plan(path)], undo=False, applied=False,
                      checked=[("Claude Code", Path("/home/x/.claude.json"), False)])
    assert "Checked these locations" in out
    assert "(not found)" in out


def test_a_file_with_no_server_map_at_all_says_where_it_looked():
    path = _write({"numStartups": 1}, name="empty.json")
    report = plan(path)
    assert report["error"] is not None
    assert "projects.*.mcpServers" in report["error"]
