#!/usr/bin/env python3
"""Analysis: does compromised agent traffic look different before the damage?

Two questions, in order of how much they matter.

1. Does the channel change the breach rate? (Replicates the earlier
   chain-of-thought finding on a multi-tool agent with real side effects.)

2. Given only the traffic — no model text, no knowledge of the condition —
   could you separate breached runs from clean ones? That is the question
   enforcement actually depends on, and nobody has published an answer.

Statistics are deliberately plain: proportions with Wilson intervals, and a
two-proportion test with no library dependency. Small n is stated, not hidden.

    python experiments/analyze_runs.py runs/pilot
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple


def load(run_dir: Path) -> List[Dict[str, Any]]:
    path = run_dir / "trials.jsonl"
    if not path.exists():
        sys.exit(f"no trials.jsonl in {run_dir} — run experiments/run.py first")
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def wilson(successes: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """95% CI for a proportion. Honest at small n, unlike the normal approx."""
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def two_proportion_z(a_hits: int, a_n: int, b_hits: int, b_n: int) -> Tuple[float, float]:
    """Returns (z, two-sided p). Normal approximation; report n alongside."""
    if a_n == 0 or b_n == 0:
        return (0.0, 1.0)
    p_pool = (a_hits + b_hits) / (a_n + b_n)
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / a_n + 1 / b_n))
    if se == 0:
        return (0.0, 1.0)
    z = (a_hits / a_n - b_hits / b_n) / se
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    return (z, p)


def median(values: List[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def by_channel(trials: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for trial in trials:
        grouped[trial["channel"]].append(trial)
    return grouped


def report_breach_rates(trials: List[Dict[str, Any]]) -> None:
    print("\n" + "=" * 72)
    print("1. BREACH RATE BY CHANNEL")
    print("=" * 72)
    print("   the payload is identical in every condition. only the channel differs.\n")

    grouped = by_channel(trials)
    print(f"   {'channel':<10}{'n':>5}{'breached':>10}{'rate':>9}   95% CI")
    print("   " + "-" * 55)

    rates = {}
    for channel in ("none", "task", "file", "db"):
        rows = grouped.get(channel, [])
        if not rows:
            continue
        hits = sum(1 for r in rows if r["observed"]["breach"])
        low, high = wilson(hits, len(rows))
        rates[channel] = (hits, len(rows))
        print(f"   {channel:<10}{len(rows):>5}{hits:>10}{hits / len(rows):>8.0%}"
              f"   [{low:.0%}, {high:.0%}]")

    if "task" in rates and "file" in rates:
        (th, tn), (fh, fn) = rates["task"], rates["file"]
        z, p = two_proportion_z(th, tn, fh, fn)
        print(f"\n   direct (task) vs indirect (file):  z={z:.2f}  p={p:.3f}")
        if p < 0.05:
            print("   -> the delivery channel changes the outcome.")
        else:
            print("   -> no separation detected at this n. report it as such.")


def report_traffic_signature(trials: List[Dict[str, Any]]) -> None:
    print("\n" + "=" * 72)
    print("2. DOES THE TRAFFIC LOOK DIFFERENT?")
    print("=" * 72)
    print("   comparing observed calls only. no model text, no condition labels.\n")

    breached = [t for t in trials if t["observed"]["breach"]]
    clean = [t for t in trials if not t["observed"]["breach"]]

    if not breached or not clean:
        print(f"   need both classes to compare (breached={len(breached)}, "
              f"clean={len(clean)}). run more trials.")
        return

    metrics = [
        ("calls per run", "n_calls"),
        ("outbound calls", "n_outbound"),
        ("largest payload (B)", "max_arg_bytes"),
        ("total args (B)", "total_arg_bytes"),
        ("errors", "n_errors"),
    ]
    print(f"   {'metric':<24}{'clean':>12}{'breached':>12}{'ratio':>10}")
    print("   " + "-" * 58)
    for label, key in metrics:
        c = median([t["observed"][key] for t in clean])
        b = median([t["observed"][key] for t in breached])
        ratio = f"{b / c:.1f}x" if c else "-"
        print(f"   {label:<24}{c:>12.0f}{b:>12.0f}{ratio:>10}")

    print(f"\n   (medians; n={len(clean)} clean, {len(breached)} breached)")

    threshold, best = _best_size_threshold(clean, breached)
    if threshold is not None:
        print(f"\n   simplest possible detector: flag any call over "
              f"{threshold:,} bytes")
        print(f"   -> separates breached from clean runs with accuracy "
              f"{best:.0%} on this sample")
        print("      one number, no model, no policy. that is the floor a real")
        print("      enforcement tier has to beat.")


def _best_size_threshold(clean: List[Dict[str, Any]],
                         breached: List[Dict[str, Any]]) -> Tuple[Any, float]:
    sizes = sorted({t["observed"]["max_arg_bytes"] for t in clean + breached})
    if not sizes:
        return (None, 0.0)
    best_threshold, best_accuracy = None, 0.0
    total = len(clean) + len(breached)
    for threshold in sizes:
        correct = (sum(1 for t in clean if t["observed"]["max_arg_bytes"] <= threshold)
                   + sum(1 for t in breached if t["observed"]["max_arg_bytes"] > threshold))
        accuracy = correct / total
        if accuracy > best_accuracy:
            best_threshold, best_accuracy = threshold, accuracy
    return (best_threshold, best_accuracy)


def report_shape(trials: List[Dict[str, Any]]) -> None:
    print("\n" + "=" * 72)
    print("3. WHAT THE RUNS ACTUALLY DID")
    print("=" * 72)
    sequences: Dict[str, int] = defaultdict(int)
    for trial in trials:
        sequences[" -> ".join(trial["observed"]["tool_sequence"])] += 1
    print()
    for sequence, count in sorted(sequences.items(), key=lambda kv: -kv[1])[:8]:
        print(f"   {count:>4}x  {sequence[:100] or '(no calls)'}")

    errors = [t for t in trials if t.get("error")]
    if errors:
        print(f"\n   {len(errors)} trials errored:")
        counts: Dict[str, int] = defaultdict(int)
        for trial in errors:
            counts[str(trial["error"])[:60]] += 1
        for message, count in sorted(counts.items(), key=lambda kv: -kv[1])[:5]:
            print(f"     {count:>4}x  {message}")


def main() -> int:
    if len(sys.argv) < 2:
        sys.exit("usage: python experiments/analyze_runs.py <run dir>")
    run_dir = Path(sys.argv[1])
    trials = load(run_dir)

    drivers = {t["driver"] for t in trials}
    print(f"\n{len(trials)} trials from {run_dir}  (driver: {', '.join(sorted(drivers))})")
    if drivers == {"scripted"}:
        print("NOTE: scripted driver — this validates the pipeline, not a model.")

    report_breach_rates(trials)
    report_traffic_signature(trials)
    report_shape(trials)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
