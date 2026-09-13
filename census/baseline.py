"""Turn the census into a machine-readable baseline other tools can depend on.

Why this exists rather than just the report: a benchmark that wants realistic
pressure levels should not copy a constant out of a blog post. A constant does
not know its own sample size, cannot say when it was measured, and does not
improve when the measurement does. So the distribution is published as a
document with its provenance attached, and consumers fetch it.

Every distribution carries `n`. That is the point of the format, not a detail:
these numbers come from a small census, and a consumer who cannot see how small
will present them with more confidence than they have earned.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

SCHEMA = "callwitness.baseline.v1"
REPO = "https://github.com/AditiChaudharyy14/callwitness"


def percentile(values: List[int], q: float) -> int:
    """Nearest-rank, so every number published actually occurred.

    Interpolation would invent a byte count nobody measured, which is a strange
    thing for a measurement to publish.
    """
    if not values:
        return 0
    ordered = sorted(values)
    i = max(0, min(len(ordered) - 1, int(round(q * (len(ordered) - 1)))))
    return int(ordered[i])


def spread(values: List[int]) -> Dict[str, int]:
    return {
        "n": len(values),
        "min": int(min(values)) if values else 0,
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "max": int(max(values)) if values else 0,
    }


def records(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def build(source: Path) -> Dict[str, Any]:
    rows = [r for r in records(source) if r.get("started")]

    servers = []
    every_returned: List[int] = []
    every_argument: List[int] = []

    for row in sorted(rows, key=lambda r: r.get("package") or r.get("server") or ""):
        calls = row.get("calls") or []
        returned = [c["returned_bytes"] for c in calls
                    if not c.get("is_error") and c.get("returned_bytes") is not None]
        arguments = [c["arguments_bytes"] for c in calls
                     if not c.get("is_error") and c.get("arguments_bytes") is not None]
        every_returned.extend(returned)
        every_argument.extend(arguments)

        declared = int(row.get("declared_bytes") or 0)
        entry: Dict[str, Any] = {
            "server": row.get("server"),
            "package": row.get("package"),
            "declared_bytes": declared,
            "tool_count": int(row.get("tool_count") or 0),
            "returned_bytes": spread(returned),
            "argument_bytes": spread(arguments),
            # The raw calls, not only the aggregate. A consumer computing its
            # own percentile, or weighting by tool, cannot do it from min/p50/
            # p95/max -- and a distribution that only ships its own summary
            # forces every consumer to accept the summariser's choices.
            "calls": [
                {"tool": c.get("tool"),
                 "arguments_bytes": c.get("arguments_bytes"),
                 "returned_bytes": c.get("returned_bytes"),
                 "ms": c.get("ms")}
                for c in calls if not c.get("is_error")
            ],
        }
        # The ratio is the finding, so it is computed here rather than left for
        # each consumer to derive differently. Only meaningful when the server
        # both declared something and was actually called.
        if declared and returned:
            entry["returned_over_declared"] = {
                "p50": round(percentile(returned, 0.50) / declared, 2),
                "max": round(max(returned) / declared, 2),
            }
        servers.append(entry)

    called = [s for s in servers if s["returned_bytes"]["n"]]

    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit": commit(),
        "source": REPO,
        "raw_data": REPO + "/tree/main/census/data",
        "method": "https://callwitness.tech/research",
        "licence": "MIT",
        # The stability promise, stated in the document because that is where a
        # consumer will look for it. Fields get added; they do not change
        # meaning or disappear inside a version.
        "stability": (
            "Within callwitness.baseline.v1, fields may be added but existing "
            "field names, units (bytes, milliseconds) and meanings will not "
            "change. A breaking change becomes v2 at a new path; v1 keeps "
            "resolving. The raw census JSONL carries no such promise -- depend "
            "on this document, not on that file."
        ),
        "sample": {
            "servers_started": len(servers),
            "servers_called": len(called),
            "tools_declared": sum(s["tool_count"] for s in servers),
            "calls": len(every_returned),
        },
        # Stated in the document itself so it travels with the numbers. A
        # consumer who copies the p95 into their code copies this line too if
        # they are reading the file at all.
        "caveat": (
            "Delivered sizes come from {} successful calls across {} servers, "
            "measured on one machine. This is the only published measurement of "
            "what MCP servers actually return rather than what they declare, "
            "which makes it the best number available and still a small one. "
            "Report n alongside any value taken from here."
        ).format(len(every_returned), len(called)),
        # Every delivered size in one sorted list. Any percentile a consumer
        # wants is one line from this, at whatever n they can see for themselves.
        "returned_bytes_all": sorted(every_returned),
        "overall": {
            "returned_bytes": spread(every_returned),
            "argument_bytes": spread(every_argument),
            "declared_bytes": spread([s["declared_bytes"] for s in servers
                                      if s["declared_bytes"]]),
        },
        "servers": servers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="census/data/census.jsonl")
    parser.add_argument("--out", default="docs/baseline/v1.json")
    args = parser.parse_args()

    source = Path(args.source)
    if not source.is_file():
        print("no census at {}".format(source))
        return 1

    document = build(source)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    print("wrote {} ({:,} bytes)".format(out, out.stat().st_size))
    print("  servers  {} started, {} called".format(
        document["sample"]["servers_started"], document["sample"]["servers_called"]))
    print("  calls    {}".format(document["sample"]["calls"]))
    print("  returned p50 {:,}  p95 {:,}  max {:,}".format(
        document["overall"]["returned_bytes"]["p50"],
        document["overall"]["returned_bytes"]["p95"],
        document["overall"]["returned_bytes"]["max"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())