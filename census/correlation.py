"""Does declared size predict delivered size? Computed from the raw census.

Run from the repo root:  python census/correlation.py

Why this exists separately
--------------------------
The figure quoted so far -- Spearman rho 0.23 -- was derived from the numbers
the published endpoint serves, not from census.jsonl. That is a reasonable
basis and a bad provenance: a coefficient in a public post should come from
the raw record, computed by something anyone can run.

It also reports the same correlation three ways. A single number computed one
way is a choice presented as a fact; if rho moves a lot depending on whether a
server is summarised by its median, its largest or its mean call, the honest
claim is weaker than one number makes it sound. If it barely moves, the claim
is exactly as strong as it looks.

Only successful calls count. A failed call returned nothing, and a zero would
drag a server's summary down for a reason that has nothing to do with size.
"""

from __future__ import annotations

import io
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

DEFAULT = Path("census/data/census.jsonl")


def load(path: Path) -> List[Dict[str, Any]]:
    rows = []
    for line in io.open(str(path), encoding="utf-8"):
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


def sizes(record: Dict[str, Any]) -> List[int]:
    out = []
    for call in record.get("calls") or []:
        if call.get("is_error"):
            continue
        value = call.get("returned_bytes")
        if isinstance(value, (int, float)):
            out.append(int(value))
    return out


def median(values: List[int]) -> float:
    """Nearest-rank, so the number is one that actually occurred."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return float(ordered[min(len(ordered) - 1,
                             max(0, int(math.ceil(0.5 * len(ordered))) - 1))])


def ranks(values: List[float]) -> List[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            out[order[k]] = average
        i = j + 1
    return out


def pearson(a: List[float], b: List[float]) -> float:
    if len(a) < 2:
        return 0.0
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    return num / (da * db) if da and db else 0.0


def report(pairs: List[Tuple[str, float, float]], label: str) -> Dict[str, float]:
    declared = [p[1] for p in pairs]
    delivered = [p[2] for p in pairs]
    rho = pearson(ranks(declared), ranks(delivered))
    logs = pearson([math.log(max(1.0, x)) for x in declared],
                   [math.log(max(1.0, y)) for y in delivered])

    by_declared = [p[0] for p in sorted(pairs, key=lambda p: -p[1])]
    by_delivered = [p[0] for p in sorted(pairs, key=lambda p: -p[2])]
    overlap = len(set(by_declared[:10]) & set(by_delivered[:10]))

    rank_d = {name: i for i, name in enumerate(by_declared)}
    rank_r = {name: i for i, name in enumerate(by_delivered)}
    moves = sorted(abs(rank_d[n] - rank_r[n]) for n, _, _ in pairs)
    median_move = moves[len(moves) // 2] if moves else 0

    print("  {:<26} rho {:+.3f}   log r2 {:.3f}   top-10 overlap {}/10   "
          "median move {} of {}".format(
              label, rho, logs ** 2, overlap, median_move, len(pairs)))
    return {"rho": rho, "r2": logs ** 2, "overlap": overlap}


def main(argv):
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT
    if not path.is_file():
        print("Cannot find {}.".format(path))
        return 1

    records = load(path)
    usable = [r for r in records
              if isinstance(r.get("declared_bytes"), (int, float))
              and r.get("declared_bytes") and sizes(r)]

    print("")
    print("  {} records, {} with a declared size and at least one good call"
          .format(len(records), len(usable)))
    print("  {} successful calls in total".format(
        sum(len(sizes(r)) for r in usable)))
    print("")

    def name(record):
        return (record.get("package") or record.get("server") or "?")

    results = {}
    for label, pick in (("by median call", lambda v: median(v)),
                        ("by largest call", lambda v: float(max(v))),
                        ("by mean call", lambda v: sum(v) / float(len(v))),
                        ("by total returned", lambda v: float(sum(v)))):
        pairs = [(name(r), float(r["declared_bytes"]), pick(sizes(r)))
                 for r in usable]
        results[label] = report(pairs, label)

    print("")
    spread = max(r["rho"] for r in results.values()) - \
        min(r["rho"] for r in results.values())
    print("  rho ranges {:.3f} across those four summaries.".format(spread))
    if spread < 0.15:
        print("  The claim does not depend on which one you pick.")
    else:
        print("  The claim DOES depend on which one you pick. Say which, or")
        print("  make the claim about rank order rather than a coefficient.")
    print("")

    biggest = sorted(
        [(name(r), float(r["declared_bytes"]), median(sizes(r))) for r in usable],
        key=lambda p: -(p[2] / p[1]))
    print("  furthest from their declared size")
    for label, declared, delivered in biggest[:5]:
        print("    {:<34} declares {:>9,}   returns {:>9,}   {:.0f}x".format(
            label[:34], int(declared), int(delivered), delivered / declared))
    print("")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
