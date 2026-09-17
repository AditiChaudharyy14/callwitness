"""`callwitness demo` against a real server, through the real proxy.

This is the command a stranger types first, and until now the only part of it
under test was the arithmetic. Everything that can actually fail on someone
else's machine -- spawning a process, the handshake, reading on background
threads, giving up on a deadline -- lives in the part no unit test reached,
and behaves differently on Windows than on anything else.

So these drive the whole thing: python spawns callwitness, callwitness spawns
a stub server, and the bytes go all the way round. Slower than the rest of the
suite and worth it, because the alternative is finding out from a bug report
about the first thing anyone runs.
"""

import sys
from pathlib import Path

import pytest

from callwitness import demo

STUB = Path(__file__).parent / "stub_server.py"


def _server(log=None, *flags):
    command = [sys.executable, str(STUB)]
    if log is not None:
        command.append(str(log))
    return command + list(flags)


def _text(lines):
    return "\n".join(lines)


# -- the happy path --------------------------------------------------------

def test_it_handshakes_lists_and_calls(tmp_path):
    made, lines = demo.run(server=_server(), home=str(tmp_path))
    assert made == 2                    # get_thing and list_items
    assert "declared 5 tools" in _text(lines)


def test_the_dangerous_tools_are_never_invoked(tmp_path):
    """The rule returning False is a different claim from nothing having run."""
    log = tmp_path / "called.txt"
    demo.run(server=_server(log), home=str(tmp_path))
    called = log.read_text().split() if log.exists() else []
    assert "delete_everything" not in called
    assert "get_and_drop_table" not in called
    assert sorted(called) == ["get_thing", "list_items"]


def test_a_tool_whose_schema_cannot_be_satisfied_is_skipped(tmp_path):
    log = tmp_path / "called.txt"
    demo.run(server=_server(log), home=str(tmp_path))
    assert "read_weird" not in (log.read_text() if log.exists() else "")


def test_the_refusals_are_reported_not_silent(tmp_path):
    _made, lines = demo.run(server=_server(), home=str(tmp_path))
    assert "2 refused" in _text(lines)


def test_the_sizes_of_what_came_back_are_shown(tmp_path):
    _made, lines = demo.run(server=_server(), home=str(tmp_path))
    assert "get_thing" in _text(lines)
    assert " B" in _text(lines)


def test_it_records_into_the_home_it_was_given(tmp_path):
    """Whatever it says it recorded has to be somewhere `baseline` can read."""
    demo.run(server=_server(), home=str(tmp_path))
    assert list(tmp_path.glob("*.db")) or list(tmp_path.glob("*.jsonl"))


# -- the ways it can go wrong ----------------------------------------------

def test_a_server_that_dies_is_reported_rather_than_hung(tmp_path):
    made, lines = demo.run(server=_server(None, "--die"), home=str(tmp_path))
    assert made == 0
    assert "handshake" in _text(lines)


def test_a_server_that_never_answers_gives_up(tmp_path, monkeypatch):
    """Without a deadline this hangs forever on a server that accepts and stalls."""
    monkeypatch.setattr(demo, "START_TIMEOUT", 2.0)
    monkeypatch.setattr(demo, "REPLY_TIMEOUT", 2.0)
    made, lines = demo.run(server=_server(None, "--silent"), home=str(tmp_path))
    assert made == 0
    assert "handshake" in _text(lines)


def test_a_command_that_does_not_exist_is_reported(tmp_path):
    made, lines = demo.run(server=["definitely-not-a-real-binary-xyz"],
                           home=str(tmp_path))
    assert made == 0
    assert _text(lines)                 # says something rather than crashing


def test_nothing_it_prints_breaks_a_windows_console(tmp_path):
    """cp1252 is the default console encoding for most of the people running this."""
    _made, lines = demo.run(server=_server(), home=str(tmp_path))
    text = _text(lines)
    assert text == text.encode("cp1252", "strict").decode("cp1252")


def test_the_call_budget_is_respected(tmp_path):
    log = tmp_path / "called.txt"
    demo.run(server=_server(log), home=str(tmp_path), max_calls=1)
    assert len(log.read_text().split()) == 1
