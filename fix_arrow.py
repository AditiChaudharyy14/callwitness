"""The right-hand side of the arrow is still on UTC. (third patch)

Run from the repo root:  python fix_arrow.py

`17 Sep 16:54 -> 11:13` -- the start converted, the end did not. Two places in
last.py format an already-parsed datetime directly instead of going through
_clock(), so they never saw the conversion:

    288:    until = (finished.strftime("%H:%M")
    295:    until = (latest.strftime("%H:%M") if latest and started

A time range where the two ends are in different zones is worse than a range
where both are wrong, because nothing about it looks wrong -- it just silently
claims a four-minute run took minus five hours.

The fix splits the conversion in two. local() takes a recorded string; _shift()
takes an already-parsed naive-UTC datetime. Both end at the same place, so
there is still exactly one piece of code that knows what zone the reader is in.
"""

import io
import re
import sys
from pathlib import Path

LAST = Path("src/callwitness/last.py")

OLD_LOCAL_BODY = '''    when = _moment(stamp)
    if when is None:
        return None
    try:
        return when.replace(tzinfo=timezone.utc).astimezone()
    except Exception:
        return when
'''

NEW_LOCAL_BODY = '''    return _shift(_moment(stamp))
'''

SHIFT = '''def _shift(when):
    """An already-parsed naive-UTC datetime on the reader's clock.

    local() is for timestamps as recorded; this is for values that have
    already been through _moment(). Both funnel through here, so there is one
    place that knows the reader's zone and no way for two rendered times on
    one line to disagree about it.
    """
    if when is None:
        return None
    try:
        return when.replace(tzinfo=timezone.utc).astimezone()
    except Exception:
        return when


'''

NAKED = re.compile(r"(?<![\w.])(finished|latest)\.strftime\(")


def main():
    if not LAST.is_file():
        print("Run this from the repo root (the folder holding src\\).")
        return 1

    text = io.open(str(LAST), encoding="utf-8").read()

    if "def _shift(" in text:
        print("Already patched. Nothing to do.")
        return 0

    if "def local(stamp)" not in text:
        print("last.py has no local() -- run fix_localtime.py first.")
        return 1
    if OLD_LOCAL_BODY not in text:
        print("local() is not in the shape this patch expects. Nothing changed.")
        return 1

    hits = NAKED.findall(text)
    if not hits:
        print("Found no bare finished.strftime / latest.strftime. Lines with")
        print("strftime in last.py:")
        for i, line in enumerate(text.splitlines()):
            if "strftime" in line:
                print("  {:>4}: {}".format(i + 1, line.rstrip()[:76]))
        print("Nothing was changed.")
        return 1

    new = text.replace(OLD_LOCAL_BODY, NEW_LOCAL_BODY, 1)
    new = new.replace("def local(stamp)", SHIFT + "def local(stamp)", 1)
    new, count = NAKED.subn(r"_shift(\1).strftime(", new)

    io.open(str(LAST), "w", encoding="utf-8").write(new)

    print("")
    print("  added _shift(), local() now routes through it")
    print("  converted {} bare strftime call{} ({})".format(
        count, "" if count == 1 else "s", ", ".join(sorted(set(hits)))))
    print("")
    print("Check nothing else prints a raw time:")
    print("  findstr /n \"strftime\" src\\callwitness\\*.py")
    print("Every hit should be inside _clock, local, _shift or _when.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
