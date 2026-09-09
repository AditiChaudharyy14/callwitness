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

from bollard.record import Recorder
from bollard.suggest import (
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

def test_ceiling_sits_above_everything_observed():
    sizes = [100] * (MIN_SAMPLE + 60) + [50_000]
    home = _home([{"bytes": b} for b in sizes])
    ceiling = _rules(home)[("send_email", "max_payload")]["value"]
    assert ceiling > max(sizes), "a ceiling at the largest thing ever seen fires tomorrow"


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
