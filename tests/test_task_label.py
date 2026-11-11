import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import task_manager as tm


class TaskLabelTests(unittest.TestCase):
    def _submit(self, tmp: str, extra: dict):
        root = Path(tmp)
        repo = root / "repo"
        repo.mkdir(exist_ok=True)
        if not (repo / ".git").exists():
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
        old_root = tm._root
        tm._root = lambda: root / "home"

        class FakeThread:
            def __init__(self, target=None, args=(), **kwargs):
                self.args = args

            def start(self):
                # Match the worker's ownership of the repository lock in this synchronous harness.
                self.args[-1].close()

        try:
            with patch.object(tm.ai_memory_bridge, "verify_runtime"), \
                 patch.object(tm, "_repo", return_value=repo.resolve()), \
                 patch.object(tm, "_repo_key", return_value="testrepo"), \
                 patch.object(tm, "_quota", return_value={"fresh": True, "cache": {}}), \
                 patch.object(tm, "_subscriptions", return_value=[
                     {"cli": "codex", "model": "gpt-6-luna", "effort": "low"},
                 ]), \
                 patch.object(tm, "_installed_models", return_value={}), \
                 patch.object(tm, "_worker_capable", side_effect=lambda candidates: candidates), \
                 patch.object(tm, "_jev_choose", return_value=[]), \
                 patch.object(tm, "_free_models", return_value=[]), \
                 patch.object(tm.threading, "Thread", FakeThread):
                result = tm.dispatch({"action": "submit", "repository": str(repo),
                                      "task": "inspect the repo", "mode": "analysis",
                                      "cli": "codex",
                                      "acceptance_criteria": [{"type": "git_diff_clean"}], **extra})
            if result.get("task_id"):
                tm._THREADS.pop(result["task_id"], None)
                tm._LOCK_FDS.pop(result["task_id"], None)
            return result, root / "home" / "runtime" / "cli-tasks"
        finally:
            tm._root = old_root

    def test_valid_label_is_stored_and_shown_in_compact_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, task_dir = self._submit(tmp, {"label": "fix failing api test"})
            self.assertEqual(result["state"], "queued")
            data = json.loads((task_dir / f"{result['task_id']}.json").read_text())
            self.assertEqual(data["label"], "fix failing api test")
            compact = tm._status_view(data)
            self.assertEqual(compact["label"], "fix failing api test")

    def test_whitespace_and_newlines_are_collapsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, task_dir = self._submit(tmp, {"label": "  fix\t failing\napi   test  "})
            data = json.loads((task_dir / f"{result['task_id']}.json").read_text())
            self.assertEqual(data["label"], "fix failing api test")

    def test_none_or_absent_label_is_omitted(self):
        for extra in ({}, {"label": None}):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as tmp:
                result, task_dir = self._submit(tmp, extra)
                data = json.loads((task_dir / f"{result['task_id']}.json").read_text())
                self.assertNotIn("label", data)
                self.assertNotIn("label", tm._status_view(data))

    def test_invalid_labels_are_rejected_before_record_or_lock_creation(self):
        invalid = ["", " \t\n ", "x" * 61, 123, ["x"]]
        for label in invalid:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                result, task_dir = self._submit(tmp, {"label": label})
                self.assertEqual(result, {
                    "ok": False,
                    "error": "label must be a non-empty string of at most 60 characters",
                })
                self.assertEqual(list(task_dir.glob("[0-9a-f]" * 32 + ".json")), [])
                self.assertEqual(list(task_dir.glob("repo-*.lock")), [])

    def test_secret_looking_label_is_redacted_and_never_added_to_events_or_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, task_dir = self._submit(tmp, {"label": "api_key=fake-label-value"})
            self.assertEqual(result["state"], "queued")
            data = json.loads((task_dir / f"{result['task_id']}.json").read_text())
            self.assertEqual(data["label"], "api_key=[redacted]")
            self.assertNotIn("label", json.dumps(data["events"]))
            self.assertNotIn("fake-label-value", json.dumps(data["events"]))
            journal = (task_dir / "events.log.jsonl").read_text()
            self.assertNotIn("label", journal)
            self.assertNotIn("fake-label-value", journal)

    def test_sixty_character_label_is_allowed(self):
        label = "x" * 60
        with tempfile.TemporaryDirectory() as tmp:
            result, task_dir = self._submit(tmp, {"label": label})
            data = json.loads((task_dir / f"{result['task_id']}.json").read_text())
            self.assertEqual(data["label"], label)


if __name__ == "__main__":
    unittest.main()
