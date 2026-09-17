"""Support `python -m callwitness`.

Save as src/callwitness/__main__.py

pip installs the callwitness executable into a Scripts directory that is not
always on PATH. On Windows it prints a warning saying exactly that and carries
on regardless:

    WARNING: The script callwitness.exe is installed in
    'C:\\Users\\...\\Python\\pythoncore-3.14-64\\Scripts' which is not on PATH.

Without this file, that machine ends up with the package installed and no way
to run it. `callwitness` is not found, and `python -m callwitness` answers
"'callwitness' is a package and cannot be directly executed". The install
succeeded and the tool is unreachable, which is a worse first impression than
a failed install -- the person has already committed by then.

Found on a clean Windows 11 / Python 3.14 laptop, 17 September 2026, by
watching someone who had never seen this tool try to install it.

SystemExit rather than a bare call so the process exit code is whatever
entrypoint() returns, matching the console script's behaviour exactly.
"""

from .cli import entrypoint

if __name__ == "__main__":
    raise SystemExit(entrypoint())
