"""Print times on the reader's clock, not on UTC's. (second attempt)

Run from the repo root:  python fix_localtime.py

The previous version matched _clock() character for character and found
nothing, so it correctly changed nothing. This one finds the function by its
def line and replaces the whole block, whatever the body looks like, and if it
still cannot it prints exactly what it saw instead of a generic failure.

The database stays UTC. Only rendering moves, and it moves in one function
that `last`, `tail` and `stats` all call, so the three cannot disagree.
"""

import io
import re
import sys
from pathlib import Path

LAST = Path("src/callwitness/last.py")
REPORT = Path("src/callwitness/report.py")

NEW_CLOCK = '''def local(stamp):
    """A recorded UTC timestamp on the reader's own clock.

    The database stores UTC and keeps storing it. This is the only place the
    two representations meet, so `last`, `tail` and `stats` cannot drift --
    report.py calls this rather than keeping its own copy.

    astimezone() with no argument uses the system zone, which is what a person
    reading their own log means by "the time". Anything unparseable comes back
    as None and the caller prints "?" exactly as before.
    """
    when = _moment(stamp)
    if when is None:
        return None
    try:
        return when.replace(tzinfo=timezone.utc).astimezone()
    except Exception:
        return when


def _clock(stamp):
    when = local(stamp)
    return when.strftime("%d %b %H:%M") if when else "?"
'''

WHEN = '''def _when(stamp) -> str:
    """One timestamp format, one zone, shared with `last`.

    Imported lazily so report.py keeps no import-time dependency on last.py.
    Nineteen characters, same as the slice it replaces, so every column that
    was aligned stays aligned.
    """
    try:
        from .last import local
        when = local(stamp)
    except Exception:
        when = None
    return when.strftime("%Y-%m-%d %H:%M:%S") if when else str(stamp)[:19]


'''

SLICE = re.compile(r"str\((\w+)\)\[:19\]")


def block(lines, start):
    """End of the def starting at `start`: the next line in column one."""
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.strip() and not line[0].isspace():
            return i
    return len(lines)


def main():
    if not LAST.is_file() or not REPORT.is_file():
        print("Run this from the repo root (the folder holding src\\).")
        return 1

    last_text = io.open(str(LAST), encoding="utf-8").read()
    report_text = io.open(str(REPORT), encoding="utf-8").read()

    did = []

    # ---- last.py -------------------------------------------------------
    if "def local(stamp)" in last_text:
        did.append("last.py already had local(), left alone")
        new_last = last_text
    else:
        lines = last_text.splitlines(True)
        at = next((i for i, l in enumerate(lines)
                   if re.match(r"def _clock\s*\(", l)), None)
        if at is None:
            print("Could not find a line starting `def _clock(` in last.py.")
            print("Lines that mention _clock:")
            for i, l in enumerate(lines):
                if "_clock" in l:
                    print("  {:>4}: {}".format(i + 1, l.rstrip()[:76]))
            print("Nothing was changed. Paste those lines and I'll adjust.")
            return 1

        end = block(lines, at)
        replaced = "".join(lines[at:end])
        new_last = "".join(lines[:at] + [NEW_CLOCK] + lines[end:])
        did.append("last.py replaced _clock() (lines {}-{})".format(at + 1, end))

        if not re.search(r"from datetime import[^\n]*timezone", new_last):
            m = re.search(r"from datetime import[^\n]*\n", new_last)
            if not m:
                print("last.py has no `from datetime import` line to extend.")
                print("Nothing was changed.")
                return 1
            line = m.group(0)
            new_last = new_last.replace(line, line.rstrip("\n") + ", timezone\n", 1)
            did.append("last.py added timezone to the datetime import")

    # ---- report.py -----------------------------------------------------
    if "def _when(" in report_text:
        did.append("report.py already had _when(), left alone")
        new_report = report_text
    else:
        hits = SLICE.findall(report_text)
        if not hits:
            print("Found no str(x)[:19] in report.py. Lines with [:19]:")
            for i, l in enumerate(report_text.splitlines()):
                if "[:19]" in l:
                    print("  {:>4}: {}".format(i + 1, l.rstrip()[:76]))
            print("Nothing was changed.")
            return 1

        # Rewrite first, insert second -- the other order feeds the helper its
        # own fallback to the regex and _when() then calls itself forever.
        rewritten, count = SLICE.subn(r"_when(\1)", report_text)
        rlines = rewritten.splitlines(True)
        at = next((i for i, l in enumerate(rlines) if l.startswith("def format_")), None)
        if at is None:
            print("Found no `def format_` in report.py to insert above.")
            print("Nothing was changed.")
            return 1
        new_report = "".join(rlines[:at] + [WHEN] + rlines[at:])
        did.append("report.py added _when(), rewrote {} timestamp{}".format(
            count, "" if count == 1 else "s"))

    if new_last == last_text and new_report == report_text:
        print("Both files already patched. Nothing to do.")
        return 0

    io.open(str(LAST), "w", encoding="utf-8").write(new_last)
    io.open(str(REPORT), "w", encoding="utf-8").write(new_report)

    print("")
    for line in did:
        print("  " + line)
    print("")
    print("No backups written -- `git diff` and `git checkout --` are better")
    print("than stray files, and the last commit is clean.")
    print("")
    print("Now:  python -m pytest -q")
    return 0


if __name__ == "__main__":
    sys.exit(main())
