"""Result previews are redacted before storage; the demo refuses env dumps."""
import json

from callwitness.demo import is_safe
from callwitness.tracker import CallTracker


class _Rec:
    def __init__(self):
        self.rows = []

    def call(self, rec):
        self.rows.append(rec)

    def event(self, *a, **k):
        pass


def _run(result):
    rec = _Rec()
    t = CallTracker(rec)
    t.on_client_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "get-env", "arguments": {}}})
    t.on_server_message({"jsonrpc": "2.0", "id": 1, "result": result})
    return rec.rows[0]


def test_secret_in_a_result_never_reaches_the_preview():
    secret = "sk-proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
    result = {"content": [{"type": "text",
                           "text": json.dumps({"OPENAI_API_KEY": secret})}]}
    row = _run(result)
    assert secret not in row["result_preview"]
    assert "redacted" in row["result_preview"]


def test_result_size_is_measured_on_the_original():
    secret = "sk-proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
    result = {"content": [{"type": "text", "text": secret}]}
    raw = json.dumps(result, ensure_ascii=False)
    assert _run(result)["result_bytes"] == len(raw.encode("utf-8"))


def test_demo_refuses_environment_and_secret_readers():
    for name in ("get-env", "getEnv", "read_environment", "list_secrets",
                 "get_api_keys", "show-credentials", "fetch_token"):
        assert not is_safe(name), name
    for name in ("echo", "get-structured-content", "get-resource-reference"):
        assert is_safe(name), name
