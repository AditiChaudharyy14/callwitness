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

from bollard.install import (
    apply, discover, format_plan, is_wrapped, plan, unwrap, wrap,
)

FILESYSTEM = {"command": "npx", "args": ["-y", "@mcp/server-filesystem", "/data"]}
GIT = {"command": "uvx", "args": ["mcp-server-git"]}
REMOTE = {"url": "https://mcp.acme.com/mcp"}
WRAPPED = {"command": "bollard", "args": ["run", "--label", "fs", "--", "npx", "-y", "x"]}


def _config(servers, key="mcpServers"):
    path = Path(tempfile.mkdtemp()) / "mcp.json"
    path.write_text(json.dumps({key: servers}, indent=2), encoding="utf-8")
    return path


def _servers(path):
    return json.loads(path.read_text())["mcpServers"]


# -- the wrap itself -------------------------------------------------------

def test_a_plain_server_is_wrapped_with_its_command_preserved():
    out = wrap("filesystem", FILESYSTEM)
    assert out["command"] == "bollard"
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
    entry = {"command": "python", "args": ["-m", "bollard.cli", "run", "--", "npx"]}
    assert is_wrapped(entry)


def test_unwrap_restores_the_original_exactly():
    assert unwrap(wrap("fs", FILESYSTEM)) == FILESYSTEM
    assert unwrap(wrap("git", GIT)) == GIT


def test_unwrap_drops_the_args_key_when_there_were_none():
    entry = {"command": "server"}
    assert unwrap(wrap("s", entry)) == entry


def test_unwrap_refuses_a_shape_it_did_not_produce():
    assert unwrap({"command": "bollard", "args": ["stats"]}) is None


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
    assert "bollard proxy" in why
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
    assert report["error"] == "no mcpServers section"
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
    assert after["fs"]["command"] == "bollard"


def test_install_then_uninstall_returns_the_file_to_its_meaning():
    servers = {"fs": FILESYSTEM, "git": GIT, "api": REMOTE}
    path = _config(dict(servers))
    apply(plan(path))
    assert _servers(path)["fs"]["command"] == "bollard"
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
    assert "+ bollard run --label fs -- npx" in out


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
