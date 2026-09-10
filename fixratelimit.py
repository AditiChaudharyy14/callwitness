"""Survive rate limits, and stop reporting failed trials as clean ones.

Run from the repository root:  python fixratelimit.py
"""

import pathlib
import sys

ROOT = pathlib.Path("experiments")
if not (ROOT / "run.py").is_file():
    print("Run this from the repository root (the folder with pyproject.toml).")
    sys.exit(2)


def patch(name, edits, guard):
    p = ROOT / name
    s = p.read_text(encoding="utf-8")
    if guard in s:
        print("  {:<18} already patched".format(name))
        return 0
    n = 0
    for old, new in edits:
        if old not in s:
            print("  {:<18} ANCHOR NOT FOUND: {!r}".format(name, old[:70]))
            return -1
        s = s.replace(old, new, 1)
        n += 1
    p.write_text(s, encoding="utf-8")
    print("  {:<18} {} edits".format(name, n))
    return n


ok = True

RETRY_BODY = '''def _post_with_retry(request, timeout, attempts=6):
    """POST, backing off on 429 and 5xx.

    Free-tier keys are limited by tokens per minute, not just requests, so a
    burst of large tool outputs trips the limit even at a modest request rate.
    Providers send Retry-After; honour it rather than guessing, and fall back
    to exponential backoff when it is absent.

    After the last attempt the error is raised, not swallowed. A trial that
    could not talk to the model is not a trial, and the caller needs to know.
    """
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt == attempts - 1:
                raise
            after = exc.headers.get("Retry-After") if exc.headers else None
            try:
                wait = float(after) if after else 0.0
            except ValueError:
                wait = 0.0
            wait = max(wait, 2.0 * (2 ** attempt))
            print("      rate limited ({}), waiting {:.0f}s".format(exc.code, wait),
                  flush=True)
            time.sleep(min(wait, 90.0))
        except urllib.error.URLError:
            if attempt == attempts - 1:
                raise
            time.sleep(2.0 * (2 ** attempt))
    raise RuntimeError("unreachable")


'''

ok &= patch("agent.py", [
    (
        "import urllib.error\nimport urllib.request",
        "import time\nimport urllib.error\nimport urllib.request",
    ),
    (
        "def chat_completion(",
        RETRY_BODY + "def chat_completion(",
    ),
    (
        '    with urllib.request.urlopen(request, timeout=timeout) as response:\n'
        '        return json.loads(response.read().decode())',
        '    return _post_with_retry(request, timeout)',
    ),
], guard="_post_with_retry") >= 0

ok &= patch("run.py", [
    (
        '        flag = "BREACH" if obs["breach"] else "  ok  "\n'
        '        note = f" [{record[\'error\']}]" if record["error"] else ""',
        '        # An errored trial is not a clean trial. It gets its own flag, and\n'
        '        # the reason leads rather than trailing off the end of the line.\n'
        '        if record["error"]:\n'
        '            flag = " ERROR"\n'
        '        elif obs["breach"]:\n'
        '            flag = "BREACH"\n'
        '        else:\n'
        '            flag = "  ok  "\n'
        '        note = f" [{record[\'error\']}]" if record["error"] else ""',
    ),
    (
        '    print(f"\\nwrote {len(records)} trials to {summary}")',
        '    failed = [r for r in records if r["error"]]\n'
        '    if failed:\n'
        '        share = len(failed) / len(records)\n'
        '        print(f"\\n  {len(failed)} of {len(records)} trials FAILED "\n'
        '              f"({share:.0%}). Most common: {failed[0][\'error\'][:80]}")\n'
        '        if share > 0.1:\n'
        '            print("  This run is not usable. A failed trial is recorded with\\n"\n'
        '                  "  breach=False, which the analysis cannot tell apart from a\\n"\n'
        '                  "  model that saw the payload and refused. Fix the cause and\\n"\n'
        '                  "  re-run before analysing.")\n'
        '\n'
        '    print(f"\\nwrote {len(records)} trials to {summary}")',
    ),
], guard="is not a clean trial") >= 0

ok &= patch("run.py", [
    (
        '    parser.add_argument("--fresh", action="store_true", help="wipe the output dir first")',
        '    parser.add_argument("--fresh", action="store_true", help="wipe the output dir first")\n'
        '    parser.add_argument("--pause", type=float, default=0.0,\n'
        '                        help="seconds to wait between trials; use on a rate-limited key")',
    ),
    (
        "        records.append(record)",
        "        records.append(record)\n"
        "        if args.pause and i < len(plan):\n"
        "            time.sleep(args.pause)",
    ),
], guard="--pause") >= 0

rp = ROOT / "run.py"
src = rp.read_text(encoding="utf-8")
if "\nimport time" not in src and "import time\n" not in src:
    src = src.replace("import tempfile", "import tempfile\nimport time", 1)
    rp.write_text(src, encoding="utf-8")
    print("  {:<18} added 'import time'".format("run.py"))

print()
if ok:
    print("Done. Now run:  python -m pytest tests -q")
else:
    print("Something did not apply. Do not commit; tell Claude what printed above.")
    sys.exit(1)
