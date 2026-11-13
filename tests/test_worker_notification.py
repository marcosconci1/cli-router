"""Terminal worker error notices are posted through the Hermes adapter."""
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import task_manager as tm


class Context:
    def __init__(self):
        self.posted_texts = []

    def get_config(self, name, default=None):
        return {"backend": "openrouter", "model": "typesafe/jev-1.14"}

    def post_message(self, text):
        self.posted_texts.append(text)
        return SimpleNamespace(message_id=f"message-{len(self.posted_texts)}")


def run_worker(tmp_path, outcome, *, mode="change", checks=None):
    task_id = uuid.uuid4().hex
    repo = tmp_path / f"repo-{task_id}"
    repo.mkdir()
    context = Context()
    candidate = {"cli": "codex", "model": "m1", "effort": "low"}
    checks = checks or {"checks": {"diff": {"ok": False}},
                        "changed_files": [], "head_unchanged": True}
    with patch.object(tm, "_root", return_value=tmp_path / "home"), \
         patch.object(tm, "_lock_path", return_value=tmp_path / f"{task_id}.lock"), \
         patch.object(tm, "_candidate_has_quota", return_value=True), \
         patch.object(tm, "_git_readonly", return_value=SimpleNamespace(stdout="head")), \
         patch.object(tm, "_git_changed_paths", return_value=[]), \
         patch.object(tm, "_path_digests", return_value={}), \
         patch.object(tm, "_independent_checks", return_value=checks), \
         patch.object(tm.ai_memory_bridge, "verify_runtime"), \
         patch.object(tm.ai_memory_bridge, "read_context", return_value=("", 0)), \
         patch.object(tm, "_run_candidate", return_value=outcome), \
         patch.object(tm, "_acceptance", return_value={"passed": True}), \
         patch.object(tm, "_independent_review_enabled", return_value=False), \
         patch.object(tm.hermes_adapter, "_CONTEXT", context):
        tm._write(task_id, {"task_id": task_id, "state": "queued"})
        tm._worker(task_id, repo, "task", mode, [], [candidate], None)
        data = tm._read(task_id)
    return data, context.posted_texts


def test_worker_posts_one_notice_for_blocked_state_and_none_for_completed(tmp_path):
    blocked, blocked_texts = run_worker(
        tmp_path, {"outcome": "account_stop", "reason": "account unavailable"})
    assert blocked["state"] == "blocked"
    assert len(blocked_texts) == 1
    assert blocked_texts[0]

    completed_checks = {"checks": {"analysis": {"ok": True}},
                        "changed_files": [], "head_unchanged": True}
    completed, completed_texts = run_worker(
        tmp_path,
        {"outcome": "needs_verification", "summary": "analysis complete",
         "ai_memory": {"workstream_id": "stream", "sequence": 1}},
        mode="analysis", checks=completed_checks)
    assert completed["state"] == "completed"
    assert completed_texts == []
