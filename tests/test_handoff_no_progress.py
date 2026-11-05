"""A quota failure that changed nothing must not stop rotation just because there is no checkpoint."""
import sys
import types
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import task_manager as tm

CANDIDATE = {"cli": "kimi", "model": "kimi-code/k3.1-pro", "effort": None}


def ask(previous_attempt, verification):
    captured = {}

    class Context:
        def get_config(self, name, default=None):
            return {"backend": "openrouter", "model": "typesafe/jev-1.14"}

    class Service:
        def __init__(self, ctx):
            pass

        def run(self, request):
            captured.update(request)
            return {"ok": True, "answers": {"handoff": {"choice": "continue"}}}

    fake = types.ModuleType("hermes_jev.service")
    fake.Service = Service
    with patch.object(tm.hermes_adapter, "_CONTEXT", Context()), patch.dict("sys.modules", {"hermes_jev.service": fake}):
        assert tm._jev_handoff("review", previous_attempt, verification, CANDIDATE, "")
    return captured


def test_attempt_without_changes_is_marked_as_no_progress():
    captured = ask({"outcome": "quota_limited", "changed_files": []}, {})
    assert captured["state"]["previous_attempt_made_progress"] is False


def test_attempt_with_changed_files_is_marked_as_progress():
    captured = ask({"outcome": "quota_limited", "changed_files": ["a.py"]}, {})
    assert captured["state"]["previous_attempt_made_progress"] is True


def test_verification_changes_count_as_progress():
    captured = ask({"outcome": "failed_verification"}, {"changed_files": ["b.py"]})
    assert captured["state"]["previous_attempt_made_progress"] is True


def test_instruction_explains_empty_context_and_keeps_the_stop_rule():
    text = ask({"outcome": "quota_limited", "changed_files": []}, {})["questions"]["handoff"]["instructions"]
    assert "previous_attempt_made_progress is false" in text
    assert "not a reason to stop" in text
    assert "unsafe" in text and "Choose stop" in text
