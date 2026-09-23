"""Put your own numbers next to everyone else's.

Why this exists
---------------
A recording of your own agent tells you what happened. It does not tell you
whether what happened is normal, and on its own it never can -- a number is
only unusual relative to something. Until now the only reference a deployment
had was its own history, which is empty on the first day and thin for a while
after that, exactly when someone is deciding whether this tool is worth
keeping.

The census fixes that. It is the same document, `callwitness.baseline.v1`,
measured across public servers and published at a fixed URL, so a machine with
four calls on it can still be told where those four calls sit among thousands.

Which direction the data moves
------------------------------
Down. This fetches a public document and compares locally; nothing about your
traffic leaves the machine, and the output says so, because a tool that reads
your tool calls has to be unusually clear about that. Contributing is a
separate command, opt-in, and shape-only.

Matching, and being honest about it
-----------------------------------
Three levels, best first: the same tool in the same package, any tool in the
same package, then the whole public distribution. A comparison against the
global distribution is much weaker than one against the same tool, so the level
is printed on every row rather than hidden. A weak comparison should look weak.
"""

from __future__ import annotations

import bisect
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PUBLIC_URL = "https://callwitness.tech/baseline/v1.json"
CACHE_NAME = "baseline-public-v1.json"
CACHE_SECONDS = 24 * 60 * 60

# Best match first. The label is printed, so a comparison the reader should
# trust less announces itself.
EXACT, PACKAGE, GLOBAL = "same tool", "same package", "all servers"


def _name_of(server: Dict[str, Any]) -> str:
    """The package key, whatever this document generation called it."""
    for key in ("package", "server", "name"):
        value = server.get(key)
        if isinstance(value, str) and value:
            return value
    return "?"


# Ecosystem prefixes a local document carries and a census label does not.
_ECOSYSTEMS = ("npm:", "pypi:", "uvx:", "npx:")
# Affixes that say "this is an MCP server" rather than which server it is.
_TRAILING = ("-mcp-server", "_mcp_server", "-mcp", "_mcp", "-server", "_server")
_LEADING = ("mcp-server-", "mcp_server_", "mcp-", "mcp_", "server-")
# Words that identify a protocol rather than a package. `@playwright/mcp`
# reduces to `mcp`, which would match everything; its scope is its name.
_GENERIC = frozenset({"mcp", "server", "servers", "cli", "core", "tools", "api", ""})


def forms(package: str) -> List[str]:
    """Every spelling of one package, so two documents can be joined.

    The same server is written three ways in the wild: a local recording knows
    it as `npm:chrome-devtools-mcp` because that is what was installed, the
    census labels it `chrome-devtools` because that is what fits a chart, and
    the repository calls it `@org/chrome-devtools-mcp`. Matching on the literal
    string means none of them ever meet, and every comparison silently falls
    back to the global distribution -- which still prints a percentile, so the
    failure looks like an answer.

    Normalising is a trade: `calculator-mcp-server` and `@wrtnlabs/calculator-
    mcp` collapse to the same key, and they are different packages. For a
    reference distribution that is the better error -- comparing a calculator
    against other calculators beats comparing it against every server ever
    measured -- but it is a judgement, so it lives in one documented place
    rather than being spread through the matching code.
    """
    raw = (package or "").strip().lower()
    for prefix in _ECOSYSTEMS:
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break

    candidates = {raw}
    if raw.startswith("@") and "/" in raw:
        scope, _, rest = raw.partition("/")
        candidates.add(rest)                         # drop the npm scope
        candidates.add(scope.lstrip("@"))            # ...unless the scope is the name

    for candidate in list(candidates):
        trimmed = candidate
        for suffix in _TRAILING:
            if trimmed.endswith(suffix) and len(trimmed) > len(suffix):
                trimmed = trimmed[:-len(suffix)]
                break
        for prefix in _LEADING:
            if trimmed.startswith(prefix) and len(trimmed) > len(prefix):
                trimmed = trimmed[len(prefix):]
                break
        candidates.add(trimmed)

    # Longest first: a more specific spelling should win before a generic one.
    return [c for c in sorted(candidates, key=len, reverse=True)
            if c and c not in _GENERIC]


def fetch(home: Path, url: str = PUBLIC_URL,
          max_age: int = CACHE_SECONDS) -> Tuple[Optional[Dict[str, Any]], str]:
    """The public document, from cache when it is fresh. Never raises.

    Returns (document, where it came from). A failure here is not an error
    worth stopping for: the local numbers are still worth printing, and a
    comparison is a nicety on a machine with no network.
    """
    cache = home / CACHE_NAME
    if cache.is_file() and (time.time() - cache.stat().st_mtime) < max_age:
        try:
            return json.loads(cache.read_text(encoding="utf-8")), "cached"
        except Exception:
            pass                        # a corrupt cache is just a cache miss

    try:
        from urllib.request import urlopen
        with urlopen(url, timeout=10) as response:      # nosec - fixed URL
            raw = response.read().decode("utf-8")
        document = json.loads(raw)
    except Exception as exc:
        if cache.is_file():
            try:
                return json.loads(cache.read_text(encoding="utf-8")), "stale cache"
            except Exception:
                pass
        return None, "unavailable: {}".format(exc)

    try:
        home.mkdir(parents=True, exist_ok=True)
        cache.write_text(raw, encoding="utf-8")
    except Exception:
        pass                            # not being able to cache is not a failure
    return document, "fetched"


def index(public: Dict[str, Any]) -> Dict[str, Any]:
    """Sorted returned-byte lists, by tool, by package, and overall."""
    by_tool: Dict[Tuple[str, str], List[int]] = {}
    by_package: Dict[str, List[int]] = {}

    for server in public.get("servers", []) or []:
        # Indexed under every spelling, so a local document written in a
        # different naming convention can still find it.
        keys = forms(_name_of(server))
        for call in server.get("calls", []) or []:
            size = int(call.get("returned_bytes") or 0)
            tool = call.get("tool") or "?"
            for key in keys:
                by_package.setdefault(key, []).append(size)
                by_tool.setdefault((key, tool), []).append(size)

    every = public.get("returned_bytes_all")
    if not isinstance(every, list) or not every:
        every = sorted(s for sizes in by_package.values() for s in sizes)

    return {
        "by_tool": {k: sorted(v) for k, v in by_tool.items()},
        "by_package": {k: sorted(v) for k, v in by_package.items()},
        "all": sorted(int(x) for x in every),
    }


def _median(values: List[int]) -> int:
    """Nearest-rank, so the number printed is one that actually occurred."""
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(round(0.5 * len(ordered))) - 1))]


def where(value: int, ordered: List[int]) -> int:
    """Percentile of value within a sorted list, 0-100."""
    if not ordered:
        return 0
    return int(round(100.0 * bisect.bisect_right(ordered, value) / len(ordered)))


def compare(local: Dict[str, Any], public: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One row per tool this machine called, most unusual first."""
    reference = index(public)
    rows: List[Dict[str, Any]] = []

    for server in local.get("servers", []) or []:
        package = _name_of(server)
        sizes: Dict[str, List[int]] = {}
        for call in server.get("calls", []) or []:
            sizes.setdefault(call.get("tool") or "?", []).append(
                int(call.get("returned_bytes") or 0))

        keys = forms(package)
        for tool, mine in sizes.items():
            against, level = None, EXACT
            for key in keys:                       # most specific spelling first
                against = reference["by_tool"].get((key, tool))
                if against:
                    break
            if not against:
                level = PACKAGE
                for key in keys:
                    against = reference["by_package"].get(key)
                    if against:
                        break
            if not against:
                against = reference["all"]
                level = GLOBAL

            yours = _median(mine)
            theirs = _median(against)
            rows.append({
                "package": package, "tool": tool, "n": len(mine),
                "yours": yours, "theirs": theirs, "level": level,
                "percentile": where(yours, against),
                "ratio": (float(yours) / theirs) if theirs else None,
            })

    rows.sort(key=lambda r: (-r["percentile"], -r["yours"]))
    return rows


def human(size: int) -> str:
    if size >= 1024 * 1024:
        return "{:.1f} MB".format(size / (1024.0 * 1024.0))
    if size >= 1024:
        return "{:.1f} KB".format(size / 1024.0)
    return "{} B".format(size)


# How many rows before the interesting ones are buried. The first run printed
# sixty and the five that mattered were off the top of the screen.
SHOWN = 20

# Read as "12x the median for this tool". The level names read as labels in
# the column beside it and as prose here, so they are not the same strings.
_MEDIAN_PHRASE = {
    EXACT: "the median for this tool",
    PACKAGE: "the median for this package",
    GLOBAL: "the overall median",
}


def _scale(row: Dict[str, Any]) -> str:
    """Words for the last column, taken from the same fact as the percentile.

    The first version of this read the ratio against the median while the
    column beside it read the rank, and the two disagreed in print: a call can
    sit below every public value and still be within 2x of their median, so
    rows appeared as `p0  about typical`. Both numbers were right and the line
    was nonsense. Rank decides the wording now, and the ratio is only allowed
    to add magnitude where it is large enough to be worth a number.
    """
    percentile, ratio = row["percentile"], row["ratio"]
    if ratio and ratio >= 2:
        return "{:.0f}x {}".format(
            ratio, _MEDIAN_PHRASE.get(row["level"], "the median"))
    if ratio and 0 < ratio <= 0.5:
        return "{:.0f}x smaller".format(1.0 / ratio)
    if percentile >= 90:
        return "high end"
    if percentile <= 10:
        return "low end"
    return "about typical"


def _pct(percentile: int) -> str:
    """p0 read like a failed match. Below every public sample is a finding."""
    return "<p1" if percentile <= 0 else "p{}".format(percentile)


def render(rows: List[Dict[str, Any]], public: Dict[str, Any], source: str) -> str:
    sample = public.get("sample", {}) or {}
    lines = ["", "Your calls against the public baseline ({} servers, {} calls, {})".format(
        sample.get("servers_called", "?"), sample.get("calls", "?"), source), ""]

    if not rows:
        lines.append("  Nothing recorded yet, so there is nothing to compare.")
        lines.append("  Run a server through the proxy first:  callwitness run -- npx -y <server>")
        lines.append("")
        return "\n".join(lines)

    shown = rows[:SHOWN]
    width = min(38, max(len(r["package"]) + len(r["tool"]) + 1 for r in shown))
    for row in shown:
        label = "{}/{}".format(row["package"], row["tool"])
        if len(label) > width:
            # ASCII. A single-character ellipsis is cp1252 on a Windows console
            # and prints as a stray accented letter in the middle of a tool
            # name, which looks like the tool name is wrong.
            label = label[:width - 3] + "..."
        lines.append("  {}  {:>9}  {:<4} vs {:<13} {}".format(
            label.ljust(width), human(row["yours"]),
            _pct(row["percentile"]), row["level"], _scale(row)))

    if len(rows) > SHOWN:
        lines.append("")
        lines.append("  {} more, least unusual last. --format md prints them all.".format(
            len(rows) - SHOWN))

    lines.append("")
    lines.append("  Biggest first. p50 means half the public calls were smaller.")
    lines.append("  p95 of everything measured publicly: {}".format(
        human(_percentile_of(public, "p95"))))
    lines.append("")
    lines.append("  The comparison is a download. Nothing about your traffic was sent.")
    lines.append("")
    lines.append("  That comparison exists because people sent the shape of")
    lines.append("  their traffic -- never arguments, never paths. Yours can")
    lines.append("  join it. It is off until you turn it on, and --dry-run")
    lines.append("  prints the exact bytes while sending nothing:")
    lines.append("")
    lines.append("    callwitness contribute --dry-run")
    lines.append("")
    return "\n".join(lines)


def render_markdown(rows: List[Dict[str, Any]], public: Dict[str, Any]) -> str:
    """Paste-ready, because the interesting rows are worth showing someone."""
    sample = public.get("sample", {}) or {}
    lines = ["| tool | mine | percentile | compared against |",
             "| --- | ---: | ---: | --- |"]
    for row in rows:
        lines.append("| `{}/{}` | {} | {} | {} |".format(
            row["package"], row["tool"], human(row["yours"]),
            _pct(row["percentile"]), row["level"]))
    lines.append("")
    lines.append("Baseline: {} servers, {} public calls, callwitness.tech/baseline/".format(
        sample.get("servers_called", "?"), sample.get("calls", "?")))
    return "\n".join(lines)


def _percentile_of(public: Dict[str, Any], key: str) -> int:
    overall = (public.get("overall") or {}).get("returned_bytes") or {}
    return int(overall.get(key) or 0)
