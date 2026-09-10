"""Rules derived from observation, and the limits of what observation supports.

The interesting assertions here are the negative ones. It is easy to make a
tool that always produces a confident-looking number; the point of this one is
that it declines when the data cannot carry a number, and says why.
"""

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from callwitness.record import Recorder
from callwitness.suggest import (
    DEST_VARIANCE_LIMIT, MIN_SAMPLE, collect, format_suggestions, format_yaml, propose,
)


def _home(calls):
    """Write calls straight to a fresh store. `calls` are dicts of overrides."""
    home = Path(tempfile.mkdtemp())
    rec = Recorder(home, "s", "test")
    base = datetime.now(timezone.utc) - timedelta(hours=10)
    for i, spec in enumerate(calls):
        rec.call({
            "ts": (base + timedelta(minutes=i)).isoformat(),
            "tool": spec.get("tool", "send_email"),
            "args": {},
            "args_bytes": spec.get("bytes", 1000),
            "args_truncated": False,
            "signals": spec.get("signals", {}),
            "redaction": {},
            "duration_ms": 5.0,
            "is_error": spec.get("error", False),
            "result_bytes": 10,
            "result_preview": "",
        })
    rec.close()
    return home


def _rules(home, **kw):
    return {(p["tool"], p["rule"]): p for p in propose(collect(home, **kw))}


def _dest(hosts=(), emails=()):
    d = {}
    if hosts:
        d["hosts"] = list(hosts)
    if emails:
        d["emails"] = list(emails)
    return {"destinations": d} if d else {}


# -- it declines when it should --------------------------------------------

def test_a_thin_sample_produces_no_threshold():
    home = _home([{"bytes": 500}] * 5)
    rules = _rules(home)
    assert ("send_email", "max_payload") not in rules
    entry = rules[("send_email", "insufficient_data")]
    assert entry["confident"] is False
    assert "5 calls" in entry["basis"]


def test_the_boundary_of_sufficiency_is_where_it_says_it_is():
    assert ("send_email", "max_payload") not in _rules(_home([{}] * (MIN_SAMPLE - 1)))
    assert ("send_email", "max_payload") in _rules(_home([{}] * MIN_SAMPLE))


def test_a_general_fetcher_is_flagged_not_allowlisted():
    """Many destinations across few calls is not a policy, it's a shape."""
    calls = [{"tool": "fetch_url",
              "signals": _dest(hosts=["h{}.example".format(i)])}
             for i in range(MIN_SAMPLE + 20)]
    entry = _rules(_home(calls))[("fetch_url", "destinations_hosts")]
    assert entry["confident"] is False
    assert entry["value"] is None
    assert "review by hand" in entry["basis"]


def test_a_fixed_integration_does_get_an_allowlist():
    calls = [{"tool": "send_email",
              "signals": _dest(emails=["ops@acme.com", "billing@acme.com"])}
             for _ in range(MIN_SAMPLE + 20)]
    entry = _rules(_home(calls))[("send_email", "destinations_emails")]
    assert entry["confident"] is True
    assert sorted(entry["value"]) == ["billing@acme.com", "ops@acme.com"]


def test_variance_threshold_is_honoured_on_both_sides():
    n = 100
    few = int(n * (DEST_VARIANCE_LIMIT / 2))
    many = int(n * (DEST_VARIANCE_LIMIT * 2))

    def build(distinct):
        return [{"tool": "t", "signals": _dest(hosts=["h{}.io".format(i % distinct)])}
                for i in range(n)]

    assert _rules(_home(build(few)))[("t", "destinations_hosts")]["confident"]
    assert not _rules(_home(build(many)))[("t", "destinations_hosts")]["confident"]


# -- the numbers it does produce -------------------------------------------

def test_ceiling_sits_above_the_bulk_it_was_derived_from():
    """Headroom over ordinary traffic, so the limit does not fire tomorrow.

    This used to assert the ceiling exceeded *everything* observed. That was
    the poisoning bug written down as a requirement: it made a single enormous
    call raise the limit to permit itself. Headroom is owed to the bulk; the
    outlier gets a tail line instead.
    """
    bulk = [100] * (MIN_SAMPLE + 60)
    home = _home([{"bytes": b} for b in bulk + [50_000]])
    rules = _rules(home)
    ceiling = rules[("send_email", "max_payload")]["value"]
    assert ceiling > max(bulk)
    assert ceiling < 50_000
    assert ("send_email", "tail_review") in rules


def test_ceiling_is_not_dragged_up_by_a_single_outlier():
    """p99 with headroom, not max with headroom."""
    normal = [1000] * 500
    home = _home([{"bytes": b} for b in normal + [10_000_000]])
    ceiling = _rules(home)[("send_email", "max_payload")]["value"]
    assert ceiling < 10_000_000


def test_rate_limit_leaves_headroom_over_observed_rate():
    home = _home([{}] * 120)  # one per minute over two hours
    rule = _rules(home)[("send_email", "rate_limit_per_hour")]
    assert rule["value"] >= 120


def test_every_proposal_carries_its_evidence():
    home = _home([{"bytes": 900, "signals": _dest(emails=["a@b.com"])}]
                 * (MIN_SAMPLE + 5))
    for entry in propose(collect(home)).__iter__():
        assert entry["basis"], "a rule without its basis is a guess with a number on it"
        assert "n=" in entry["basis"] or "call" in entry["basis"] or "hour" in entry["basis"]


def test_since_window_excludes_older_calls():
    home = _home([{}] * 40)
    assert collect(home)["total"] == 40
    assert collect(home, since_days=0.01)["total"] == 0


# -- output ----------------------------------------------------------------

def test_text_output_states_what_it_cannot_know():
    home = _home([{"signals": _dest(emails=["ops@acme.com"])}] * (MIN_SAMPLE + 5))
    out = format_suggestions(home)
    assert "not a destination that is forbidden" in out
    assert "Review before enforcing" in out


def test_text_output_marks_unconfident_lines():
    home = _home([{"tool": "rare"}] * 3 + [{"tool": "common"}] * (MIN_SAMPLE + 5))
    out = format_suggestions(home)
    marked = [l for l in out.splitlines() if l.startswith("?")]
    assert any("rare" in l for l in marked)
    assert not any("common" in l for l in marked)


def test_yaml_output_comments_every_rule_with_its_basis():
    home = _home([{"bytes": 800, "signals": _dest(hosts=["api.acme.com"])}]
                 * (MIN_SAMPLE + 5))
    out = format_yaml(home)
    assert "version: 1" in out
    assert "max_payload:" in out
    assert "api.acme.com" in out
    assert out.count("#") >= 4, "a generated policy must carry its reasoning"


def test_yaml_does_not_emit_rules_it_is_not_confident_in():
    home = _home([{"tool": "rare"}] * 4)
    out = format_yaml(home)
    assert "max_payload" not in out
    assert "rare" in out  # still mentioned, as a comment


def test_empty_store_says_so_rather_than_proposing_nothing_confidently():
    home = Path(tempfile.mkdtemp())
    Recorder(home, "s", "l").close()
    assert "No calls recorded" in format_suggestions(home)


def test_missing_store_raises_for_the_cli_to_handle():
    with pytest.raises(FileNotFoundError):
        format_suggestions(Path(tempfile.mkdtemp()) / "nope")


# --------------------------------------------------------------------------
# Baseline poisoning
#
# The demo showed this against real output: a 29KB exfiltration inside the
# observation window produced a 43KB proposed ceiling -- one that would have
# permitted the exact call the tool exists to catch. The attack raised the
# limit rather than being noticed. These pin the inversion.
# --------------------------------------------------------------------------

from callwitness.suggest import TAIL_MAX_FRACTION, TAIL_MULTIPLE, _split_tail


def _poisoned(normal=1000, n=60, attack=30_000):
    return _home([{"bytes": normal}] * n + [{"bytes": attack}])


def test_the_ceiling_does_not_permit_the_outlier_that_set_it():
    """The regression that matters. Previously ceiling > attack; now under it."""
    home = _poisoned()
    ceiling = _rules(home)[("send_email", "max_payload")]["value"]
    assert ceiling < 30_000, (
        "a single large call must not raise the limit to permit itself")


def test_the_outlier_is_reported_rather_than_absorbed():
    entry = _rules(_poisoned())[("send_email", "tail_review")]
    assert entry["value"] == 1
    assert entry["mark"] == "!"
    assert entry["confident"] is False


def test_the_tail_line_says_when_and_where():
    """A count is not reviewable. A person needs to go look at the call."""
    home = _home([{"bytes": 800, "signals": _dest(emails=["ops@acme.com"])}] * 60
                 + [{"bytes": 40_000, "signals": _dest(hosts=["exfil.example.net"])}])
    basis = _rules(home)[("send_email", "tail_review")]["basis"]
    assert "exfil.example.net" in basis
    assert "39" in basis or "40" in basis  # the size, humanised


def test_the_ceiling_basis_admits_what_it_excluded():
    basis = _rules(_poisoned())[("send_email", "max_payload")]["basis"]
    assert "EXCLUDES" in basis
    assert "tail line" in basis


def test_ordinary_traffic_produces_no_tail_line():
    """Nothing to review means nothing shouted about. Alarm fatigue is a bug."""
    import random
    rng = random.Random(3)
    home = _home([{"bytes": rng.randint(800, 1600)} for _ in range(80)])
    assert ("send_email", "tail_review") not in _rules(home)


def test_a_genuinely_wide_distribution_is_not_trimmed():
    """If a third of calls are large, that is the shape, not an intrusion.

    Excluding that much would misdescribe the traffic -- a different failure
    from the one we are defending against, and just as wrong.
    """
    n = 90
    big = int(n * 0.3)
    sizes = [500] * (n - big) + [80_000] * big
    _, tail, _ = _split_tail(sizes)
    assert tail == [], "excluding 30% of traffic is not tail trimming"


def test_the_tail_fraction_boundary_holds_on_both_sides():
    n = 200
    under = int(n * TAIL_MAX_FRACTION) - 1
    over = int(n * TAIL_MAX_FRACTION) + 5

    def split(count):
        return _split_tail([1000] * (n - count) + [500_000] * count)[1]

    assert len(split(under)) == under
    assert split(over) == []


def test_a_zero_median_defines_no_tail():
    """With no scale to measure against, nothing is far from anything."""
    bulk, tail, median = _split_tail([0] * 50 + [9_999_999])
    assert median == 0
    assert tail == []
    assert bulk == [0] * 50 + [9_999_999]


def test_the_threshold_is_a_multiple_of_the_median_not_the_mean():
    """A mean is moved by the outlier; that is how the ceiling got poisoned."""
    sizes = [1000] * 99 + [10_000_000]
    _, tail, median = _split_tail(sizes)
    assert median == 1000
    assert tail == [10_000_000]
    assert 10_000_000 > median * TAIL_MULTIPLE


def test_text_output_explains_why_ceilings_exclude_the_tail():
    out = format_suggestions(_poisoned())
    assert "already happened while we were watching" in out
    assert "!" in out


def test_yaml_marks_the_tail_for_review_not_as_unresolved():
    out = format_yaml(_poisoned())
    assert "REVIEW tail_review" in out
    assert "max_payload" in out
