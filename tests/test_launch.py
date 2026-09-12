"""The proxy must be able to start the servers people actually run.

Popen does not perform a shell's PATH lookup on Windows. The npm launchers are
`npx.cmd` and `npm.cmd`, so Popen(["npx", ...]) fails with WinError 2 on the
same machine where `npx --version` answers instantly. Most published MCP
servers are npm packages started with npx, so that one missing lookup made
Callwitness unable to wrap most real servers on most desktops -- while every
test passed, because the suite only ever launched sys.executable by full path.

These tests exercise the lookup itself rather than a platform, so the Windows
behaviour stays covered on a Linux CI runner.
"""

import os

from callwitness.proxy import resolve_program


def test_a_bare_name_is_resolved_to_a_full_path(tmp_path, monkeypatch):
    """What the shell finds, the proxy must find too."""
    suffix = ".cmd" if os.name == "nt" else ""
    target = tmp_path / ("fakeserver" + suffix)
    target.write_text("")
    target.chmod(0o755)

    monkeypatch.setenv("PATH", str(tmp_path), prepend=os.pathsep)
    if os.name == "nt":
        # The whole point on Windows: the extension comes from PATHEXT, and
        # the caller never types it.
        monkeypatch.setenv("PATHEXT", ".CMD")

    resolved = resolve_program(["fakeserver", "--root", "C:\\tmp"])
    # normcase, because which() builds each candidate by concatenating the
    # name with PATHEXT's own spelling: with PATHEXT=".CMD" it returns
    # fakeserver.CMD for a file on disk named fakeserver.cmd. Windows treats
    # those as the same file, so a case-sensitive assertion here would be
    # testing how PATHEXT happens to be written, not whether the lookup works.
    assert os.path.normcase(resolved[0]) == os.path.normcase(str(target))
    assert resolved[1:] == ["--root", "C:\\tmp"]


def test_arguments_are_never_touched(tmp_path, monkeypatch):
    """Only argv[0] is rewritten. Everything after it belongs to the server."""
    target = tmp_path / "srv"
    target.write_text("")
    target.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path), prepend=os.pathsep)
    if os.name == "nt":
        monkeypatch.setenv("PATHEXT", "")

    args = ["-y", "@modelcontextprotocol/server-filesystem", "--", "-y"]
    assert resolve_program(["srv"] + args)[1:] == args


def test_an_absolute_path_survives_unchanged(tmp_path, monkeypatch):
    """A caller who already resolved the program keeps their exact choice."""
    target = tmp_path / "srv"
    target.write_text("")
    target.chmod(0o755)
    monkeypatch.setenv("PATHEXT", "")
    assert resolve_program([str(target)]) == [str(target)]


def test_an_unknown_command_is_passed_through_verbatim():
    """So the error names what the user typed, not something they never wrote.

    Popen still raises FileNotFoundError; resolve_program must not convert a
    missing program into a silently different one, and must not raise here --
    reporting the failure is Proxy.run's job, which prints the command.
    """
    unknown = ["definitely-not-a-real-program-a7f3c1"]
    assert resolve_program(unknown) == unknown


def test_an_empty_command_does_not_explode():
    assert resolve_program([]) == []
