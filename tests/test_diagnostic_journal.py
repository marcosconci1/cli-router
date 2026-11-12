"""Local diagnostic journal and diagnose bundle; isolated home, no network."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import diagnostic_journal as dj
import task_manager as tm

FAKE_KEY = "sk-" + "a1b2c3d4e5f6g7h8i9j0"


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setattr(tm, "_root", lambda: path)
    monkeypatch.setattr(tm, "_JOURNAL_STARTED", False)
    return path


def _lines(home):
    return [json.loads(line) for line in (home / "runtime/cli-tasks/events.log.jsonl").read_text().splitlines()]


def test_append_event_writes_valid_jsonl(home):
    dj.append_event(home, "task_started", {"task_id": "t1", "repository": "/r", "mode": "change"})
    dj.append_event(home, "task_finished", {"task_id": "t1", "state": "blocked",
                                            "error_class": "RuntimeError", "error": f"api_key={FAKE_KEY}"})
    first, second = _lines(home)
    assert first["kind"] == "task_started" and first["mode"] == "change"
    datetime.fromisoformat(first["at"])
    assert FAKE_KEY not in json.dumps(second) and second["error_class"] == "RuntimeError"


def test_trim_lines_drops_entries_older_than_thirty_days():
    now = datetime(2026, 11, 6, tzinfo=timezone.utc)
    def line(days):
        return json.dumps({"at": (now - timedelta(days=days)).isoformat(), "kind": "x"})
    kept = dj.trim_lines([line(45), line(31), line(29), line(1), "not json"], now)
    assert kept == [line(29), line(1)]


def test_trim_lines_trims_oversized_input_from_the_oldest_end():
    now = datetime(2026, 11, 6, tzinfo=timezone.utc)
    lines = [json.dumps({"at": (now - timedelta(seconds=600 - i)).isoformat(), "kind": "x", "pad": "p" * 1000, "n": i})
             for i in range(2000)]
    kept = dj.trim_lines(lines, now)
    assert sum(len(l.encode()) + 1 for l in kept) <= dj.MAX_JOURNAL_BYTES
    assert 0 < len(kept) < len(lines) and kept[-1] == lines[-1]
    assert kept == lines[len(lines) - len(kept):]


def test_append_trims_the_file_on_disk(home):
    path = dj.journal_path(home)
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    path.write_text(json.dumps({"at": old, "kind": "old"}) + "\n")
    dj.append_event(home, "task_started", {"task_id": "new"})
    assert [e["kind"] for e in _lines(home)] == ["task_started"]


def test_diagnose_returns_redacted_bundle(home, tmp_path):
    repo = tmp_path / "secret-project-repo"
    repo.mkdir()
    token = "f" * 64
    tid = "b" * 32
    tm._write(tid, {"task_id": tid, "state": "blocked", "phase": "blocked", "updated_at": "2026-11-06T00:00:00+00:00",
                    "repository": str(repo), "owner_token": token, "owner_hash": "h", "error_class": "RuntimeError",
                    "error_detail": f"password=hunter2 {FAKE_KEY}", "message": "private prompt text",
                    "command": ["claude", "--secret-flag"]})
    dj.append_event(home, "task_started", {"task_id": tid, "repository": str(repo), "mode": "change"})
    dj.append_event(home, "task_finished", {"task_id": tid, "state": "blocked", "error": f"failed in {repo} api_key={FAKE_KEY}"})
    checks = [{"name": "bubblewrap", "status": "ok", "hint": "/usr/bin/bwrap"}]
    with patch.object(tm.doctor, "run_checks", return_value=checks):
        result = tm.dispatch({"action": "diagnose"})
    assert result["ok"] is True
    path = Path(result["path"])
    assert path.parent == home / "runtime/cli-tasks" and path.name.startswith("diagnose-")
    assert path.read_text() == result["report"]
    report = result["report"]
    assert "router version: 0.4.1" in report or re.search(r"router version: \d", report)
    assert "python:" in report and "platform:" in report
    assert "bubblewrap: ok" in report and "/usr/bin/bwrap" not in report
    assert f"task_id={tid}" in report and "RuntimeError" in report
    for forbidden in ("owner_token", token, str(repo), str(tmp_path), FAKE_KEY, "hunter2",
                      "private prompt text", "--secret-flag", "sk-"):
        assert forbidden not in report


def test_legacy_structured_credentials_are_absent_from_status_journal_and_diagnose(home):
    tid = "d" * 32
    fixtures = ("fixture-json-token", "fixture spaced password", "nested-unquoted-key")
    raw = ('{"token":"fixture-json-token","nested":{"password":"fixture spaced password",'
           '"api_key":nested-unquoted-key}}')
    tm._write(tid, {"task_id": tid, "state": "blocked", "phase": "blocked",
                    "updated_at": tm._stamp(), "error_detail": raw,
                    "degraded": [tm.problems.make("AIM_CHECK_FAILED", raw)]})
    view = tm._status_view(tm._read(tid))
    history = tm._status_view(tm._read(tid), include_history=True)
    dj.append_event(home, "task_finished", {"task_id": tid, "error": raw})
    checks = []
    with patch.object(tm.doctor, "run_checks", return_value=checks):
        report = tm.dispatch({"action": "diagnose"})["report"]
    journal = (home / "runtime/cli-tasks/events.log.jsonl").read_text()
    problem = json.dumps(view.get("degraded", []))
    for secret in fixtures:
        assert secret not in json.dumps(view)
        assert secret not in json.dumps(history)
        assert secret not in journal
        assert secret not in report
        assert secret not in problem


def test_doctor_action_appends_snapshot(home):
    checks = [{"name": "git", "status": "ok", "hint": "x"}, {"name": "ai-memory", "status": "warn", "hint": "y"}]
    with patch.object(tm.doctor, "run_checks", return_value=checks):
        assert tm.dispatch({"action": "doctor"})["checks"] == checks
    (snapshot,) = [e for e in _lines(home) if e["kind"] == "doctor_snapshot"]
    assert snapshot["checks"] == [{"name": "git", "status": "ok"}, {"name": "ai-memory", "status": "warn"}]


def test_journal_failure_does_not_break_submit(home, tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    with patch.object(dj, "append_event", side_effect=OSError("read-only")), \
         patch.object(tm, "_repo", return_value=repo), \
         patch.object(tm.ai_memory_bridge, "verify_runtime"), \
         patch.object(tm, "_quota", return_value={"fresh": True, "cache": {}}), \
         patch.object(tm, "_subscriptions", return_value=[{"cli": "claude", "model": "claude-opus-5-5", "effort": "high"}]), \
         patch.object(tm, "_installed_models", return_value={}), \
         patch.object(tm, "_worker_capable", side_effect=lambda c: c), \
         patch.object(tm, "_free_models", return_value=[]), \
         patch.object(tm.threading, "Thread", FakeThread):
        result = tm.dispatch({"action": "submit", "repository": str(repo), "task": "inspect", "mode": "analysis",
                              "cli": "claude", "acceptance_criteria": [{"type": "git_diff_clean"}]})
    assert result["state"] == "queued"
    assert "diagnostic journal write failed (OSError)" in capsys.readouterr().err


def test_task_finish_is_journaled_and_failure_is_recorded_as_warning(home):
    data = {"task_id": "c" * 32, "state": "queued", "attempts": [{}]}
    tm._transition(data, "blocked", "task_blocked", "x", state="blocked", finished_at=tm._stamp(), error_class="ValueError")
    (entry,) = _lines(home)
    assert (entry["kind"], entry["state"], entry["error_class"], entry["attempts"]) == ("task_finished", "blocked", "ValueError", 1)
    with patch.object(dj, "append_event", side_effect=OSError("boom")):
        tm._transition(data, "blocked", "task_blocked", "x", state="blocked", finished_at=tm._stamp())
    assert "diagnostic journal write failed (OSError)" in data["route_warnings"]
    assert tm._read(data["task_id"])["state"] == "blocked"
