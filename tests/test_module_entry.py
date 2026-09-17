"""Save as tests/test_module_entry.py

`python -m callwitness` is the only way to run this tool on a machine where
pip put the console script somewhere that is not on PATH -- which is the
default for at least one common Windows Python install. It is therefore not a
convenience, and it needs a test that fails loudly if __main__.py is ever
removed or renamed.

These run the real interpreter in a subprocess rather than importing, because
what is being tested is exactly the thing importing would bypass.
"""

import subprocess
import sys


def run(*args, timeout=60):
    return subprocess.run(
        [sys.executable, "-m", "callwitness"] + list(args),
        capture_output=True, text=True, timeout=timeout)


def test_the_module_can_be_executed_at_all():
    """The failure this guards against: 'callwitness' is a package and
    cannot be directly executed."""
    result = run("--version")
    assert "cannot be directly executed" not in result.stderr, result.stderr
    assert "No module named" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr


def test_it_reports_the_same_version_as_the_console_script():
    from callwitness import __version__
    result = run("--version")
    assert __version__ in (result.stdout + result.stderr), result.stdout


def test_help_works_through_the_module_path():
    result = run("--help")
    assert result.returncode == 0, result.stderr
    text = result.stdout + result.stderr
    for command in ("install", "last", "cost", "tail", "demo"):
        assert command in text, "{} missing from --help".format(command)


def test_an_unknown_command_still_exits_non_zero():
    """argparse's exit code has to survive the __main__ path, or scripts
    that check it silently pass on a typo."""
    result = run("definitely-not-a-command")
    assert result.returncode != 0
