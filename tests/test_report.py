"""callwitness report: one page, escaped, and without contents."""
from callwitness.cli import main
from callwitness.record import Recorder
from callwitness.tracker import CallTracker


def _record(home, tool="read_file", text="SECRET-CONTENT-123"):
    rec = Recorder(home, "sessabcdef01", "L")
    rec.start_session(["npx", "server"])
    t = CallTracker(rec)
    t.on_client_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": tool, "arguments": {"path": "x"}}})
    t.on_server_message({"jsonrpc": "2.0", "id": 1,
                         "result": {"content": [{"type": "text", "text": text}]}})
    rec.end_session(0)
    rec.close()


def test_report_is_written_and_says_intact(tmp_path):
    home = tmp_path / "h"
    _record(home)
    out = tmp_path / "r.html"
    assert main(["--home", str(home), "report", "-o", str(out)]) == 0
    page = out.read_text(encoding="utf-8")
    assert "Chain intact" in page
    assert "read_file" in page


def test_report_never_contains_response_contents(tmp_path):
    home = tmp_path / "h"
    _record(home, text="SECRET-CONTENT-123")
    out = tmp_path / "r.html"
    main(["--home", str(home), "report", "-o", str(out)])
    assert "SECRET-CONTENT-123" not in out.read_text(encoding="utf-8")


def test_report_escapes_tool_names(tmp_path):
    home = tmp_path / "h"
    _record(home, tool="<script>alert(1)</script>")
    out = tmp_path / "r.html"
    main(["--home", str(home), "report", "-o", str(out)])
    page = out.read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_report_by_session_prefix(tmp_path):
    home = tmp_path / "h"
    _record(home)
    out = tmp_path / "r.html"
    assert main(["--home", str(home), "report", "--session", "sessab",
                 "-o", str(out)]) == 0


def test_report_with_empty_store(tmp_path):
    assert main(["--home", str(tmp_path / "none"), "report"]) == 1
