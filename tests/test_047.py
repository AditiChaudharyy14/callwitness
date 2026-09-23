"""0.4.7: bounded preview redaction and the report's dropped banner."""
import json
import time

from callwitness.redact import redact_preview


def test_preview_redacts_known_keys_and_patterns():
    secret = "sk-proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
    raw = json.dumps({"content": [{"type": "text", "text": secret}],
                      "password": "hunter2", "pin": 1234})
    out, stats = redact_preview(raw)
    assert secret not in out and "hunter2" not in out and "1234" not in out
    assert stats.total >= 2


def test_preview_redacts_escaped_json_inside_a_string():
    inner = json.dumps({"password": "hunter2", "note": "ok"})
    raw = json.dumps({"content": [{"type": "text", "text": inner}]})
    out, _ = redact_preview(raw)
    assert "hunter2" not in out
    assert "ok" in out


def test_secret_straddling_the_preview_edge_is_caught_whole():
    secret = "sk-proj-" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4"
    raw = "a" * 500 + " " + secret + " " + "b" * 100
    out, _ = redact_preview(raw)
    assert "sk-proj-Z9y8" not in out


def test_preview_is_cheap_on_large_results():
    raw = json.dumps({"content": [{"type": "text", "text": "x" * 500000}]})
    start = time.perf_counter()
    for _ in range(20):
        redact_preview(raw)
    assert (time.perf_counter() - start) / 20 < 0.05


def test_preview_leaves_plain_text_alone():
    raw = json.dumps({"content": [{"type": "text", "text": "hello world"}]})
    out, stats = redact_preview(raw)
    assert out == raw[:512] and stats.total == 0
