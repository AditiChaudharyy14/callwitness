"""Turn the census into the tables the paper is made of.

    python census/report.py --data census/data/census.jsonl

Three sections, in the order a reader needs them:

  1. What each server declares. This is the number everyone else has already
     published, and it is here so the comparison in section 2 lands.
  2. What each call actually returned. This is the number nobody has.
  3. Tool descriptions that address the model rather than the user.

Distributions, not averages, throughout. The whole point is that most calls are
small and a few are enormous, and a mean hides exactly that.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
from typing import Any, Dict, List

# Rough, and labelled rough everywhere it is used. Four bytes per token is the
# usual approximation for English JSON; the real figure depends on the
# tokeniser, and no claim in the paper should rest on it. Byte counts are the
# measurement. Tokens are the translation for readers who budget in tokens.
BYTES_PER_TOKEN = 4


def human(n: float) -> str:
    if n >= 1_000_000:
        return "{:.1f}MB".format(n / 1_000_000)
    if n >= 1000:
        return "{:.0f}KB".format(n / 1000)
    return "{:.0f}B".format(n)


def percentile(values: List[float], q: float) -> float:
    """Nearest-rank percentile. No interpolation, so every number printed is a
    value that actually occurred -- which matters when the tail is the story."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


def load(path: pathlib.Path) -> List[Dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def section_declared(rows: List[Dict[str, Any]]) -> None:
    print("=" * 74)
    print("1. WHAT EACH SERVER DECLARES  (the part already measured elsewhere)")
    print("=" * 74)
    print("   every tool definition is resent on every request, for the whole session\n")
    print("   {:<22}{:>7}{:>12}{:>10}{:>12}".format(
        "server", "tools", "declared", "~tokens", "desc bytes"))
    print("   " + "-" * 63)

    live = [r for r in rows if r.get("started") and r.get("tool_count")]
    for row in sorted(live, key=lambda r: -r["declared_bytes"]):
        print("   {:<22}{:>7}{:>12}{:>10}{:>12}".format(
            row["server"][:22], row["tool_count"],
            "{:,}".format(row["declared_bytes"]),
            "{:,}".format(row["declared_bytes"] // BYTES_PER_TOKEN),
            "{:,}".format(row["description_bytes"])))

    total = sum(r["declared_bytes"] for r in live)
    tools = sum(r["tool_count"] for r in live)
    print("   " + "-" * 63)
    print("   {:<22}{:>7}{:>12}{:>10}".format(
        "all of them installed", tools, "{:,}".format(total),
        "{:,}".format(total // BYTES_PER_TOKEN)))
    print("\n   ~{:,} tokens before the agent reads a word of the actual task.".format(
        total // BYTES_PER_TOKEN))

    failed = [r for r in rows if not r.get("started") or not r.get("tool_count")]
    if failed:
        print("\n   did not start or declared nothing ({}):".format(len(failed)))
        for row in failed:
            print("     {:<22}{}".format(row["server"][:22], (row.get("error") or "?")[:44]))
        print("   That number is a finding too. Report it: a census of what people")
        print("   can install is not the same as a census of what starts.")


def section_returned(rows: List[Dict[str, Any]]) -> None:
    print("\n" + "=" * 74)
    print("2. WHAT ONE CALL ACTUALLY RETURNED  (the part nobody has measured)")
    print("=" * 74)
    print("   declared size is fixed and public. This is neither.\n")

    every: List[float] = []
    print("   {:<22}{:>7}{:>11}{:>11}{:>11}{:>9}".format(
        "server", "calls", "median", "p95", "largest", "vs decl"))
    print("   " + "-" * 71)

    for row in rows:
        good = [c for c in row.get("calls", [])
                if not c.get("timeout") and not c.get("is_error")]
        if not good:
            continue
        sizes = [c["returned_bytes"] for c in good]
        every.extend(sizes)
        declared = row.get("declared_bytes") or 1
        print("   {:<22}{:>7}{:>11}{:>11}{:>11}{:>8.1f}x".format(
            row["server"][:22], len(sizes),
            human(statistics.median(sizes)), human(percentile(sizes, 0.95)),
            human(max(sizes)), max(sizes) / declared))

    if not every:
        print("   no successful calls recorded.")
        return

    print("   " + "-" * 71)
    print("\n   across all {} calls:".format(len(every)))
    print("     median   {:>10}   ~{:,} tokens".format(
        human(statistics.median(every)), int(statistics.median(every)) // BYTES_PER_TOKEN))
    print("     p95      {:>10}   ~{:,} tokens".format(
        human(percentile(every, 0.95)), int(percentile(every, 0.95)) // BYTES_PER_TOKEN))
    print("     largest  {:>10}   ~{:,} tokens".format(
        human(max(every)), int(max(every)) // BYTES_PER_TOKEN))

    spread = max(every) / max(1.0, statistics.median(every))
    print("\n   The largest call returned {:,.0f}x the median call.".format(spread))
    print("   That spread is the finding. A budget built on the declared size, or")
    print("   on an average, is wrong by that factor exactly when it matters --")
    print("   and every one of those bytes is untrusted text entering the context.")

    errored = sum(1 for r in rows for c in r.get("calls", []) if c.get("is_error"))
    timed_out = sum(1 for r in rows for c in r.get("calls", []) if c.get("timeout"))
    if errored or timed_out:
        print("\n   excluded: {} error replies, {} timeouts. Error replies still".format(
            errored, timed_out))
        print("   consume context, but they measure the argument we guessed, not the tool.")


def section_directives(rows: List[Dict[str, Any]]) -> None:
    print("\n" + "=" * 74)
    print("3. TOOL DESCRIPTIONS THAT ADDRESS THE MODEL")
    print("=" * 74)
    print("   A description is not documentation -- the model reads it as part of")
    print("   its instructions. These are flagged for a human to read, not")
    print("   accusations. Quote them in the paper; do not characterise them.\n")

    found = [(r["server"], d) for r in rows for d in r.get("directive_tools", [])]
    if not found:
        print("   none flagged.")
        return
    for server, entry in found:
        print("   {:<22}{:<28}{}".format(
            server[:22], (entry["tool"] or "?")[:28], ", ".join(entry["flags"])))

    servers_with = len({s for s, _ in found})
    live = sum(1 for r in rows if r.get("started"))
    print("\n   {} tools across {} of {} servers that started.".format(
        len(found), servers_with, live))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", default="census/data/census.jsonl")
    args = parser.parse_args()

    path = pathlib.Path(args.data)
    if not path.is_file():
        print("no dataset at {} -- run census/census.py first".format(path))
        return 2
    rows = load(path)
    if not rows:
        print("dataset is empty")
        return 2

    when = rows[0].get("measured_at", "?")
    print("\ncallwitness census -- {} servers, measured {}".format(len(rows), when))
    print("byte counts are exact; token figures are ~bytes/{}, and no claim "
          "should rest on them\n".format(BYTES_PER_TOKEN))

    section_declared(rows)
    section_returned(rows)
    section_directives(rows)
    print("\n" + "=" * 74)
    print("Every number above came from traffic callwitness recorded. `callwitness")
    print("verify` will tell you whether that recording has been altered since.")
    print("=" * 74 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
