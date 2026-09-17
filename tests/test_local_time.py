"""Save as tests/test_local_time.py

These have to pass on a laptop at UTC+05:45 and on a CI runner at UTC, so
nothing here asserts a clock face. They assert the two things that can
actually break: that the conversion preserves the instant rather than shifting
it, and that the printed field is still exactly as wide as the one it replaced
-- every column in `tail` and `stats` is aligned behind it.
"""

from datetime import datetime, timezone

from callwitness import last, report

SAMPLES = [
    "2026-09-09T17:08:31.442000",
    "2026-09-09T17:08:31",
    "2026-09-09T17:08:31Z",
    "2026-09-09T17:08:31+00:00",
    "2026-09-09T22:53:31+05:45",
]


def test_local_keeps_the_instant_it_was_given():
    for stamp in SAMPLES:
        shown = last.local(stamp)
        assert shown is not None, stamp
        assert shown.tzinfo is not None, stamp
        back = shown.astimezone(timezone.utc).replace(tzinfo=None)
        assert back == last._moment(stamp), stamp


def test_every_sample_is_the_same_instant():
    """All five spellings describe one moment, whatever zone they arrive in."""
    instants = {last.local(s).astimezone(timezone.utc).replace(microsecond=0)
                for s in SAMPLES}
    assert len(instants) == 1


def test_unparseable_values_do_not_crash_or_recurse():
    assert last.local(None) is None
    assert last.local("") is None
    assert last.local("not a timestamp") is None
    assert last._clock(None) == "?"
    assert report._when("not a timestamp") == "not a timestamp"


def test_the_printed_field_is_still_nineteen_characters():
    for stamp in SAMPLES:
        assert len(report._when(stamp)) == 19, stamp


def test_report_and_last_agree_on_the_same_row():
    """The three commands must not print two different times for one call."""
    for stamp in SAMPLES:
        from_report = report._when(stamp)          # 2026-09-09 22:53:31
        from_last = last._clock(stamp)             # 09 Sep 22:53
        assert from_report[11:16] == from_last[-5:], stamp


def test_naive_input_is_read_as_utc_not_as_local():
    """The database stores naive UTC. Reading it as local would be silent
    corruption on every machine that is not on UTC."""
    stamp = "2026-09-09T17:08:31"
    assert last.local(stamp).astimezone(timezone.utc).hour == 17
