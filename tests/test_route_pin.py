import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import task_manager as tm


class RoutePinTests(unittest.TestCase):
    """Submit-time cli/model pins restrict the worker route and skip Jev ranking."""

    def _submit(self, tmp: str, extra: dict, *, routes=None, spawn_record=None):
        root = Path(tmp)
        repo = root / "repo"
        repo.mkdir(exist_ok=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        old_root = tm._root
        tm._root = lambda: root / "home"

        class FakeThread:
            def __init__(self, target=None, args=(), kwargs=None, name=None, daemon=None):
                # args = (_worker, task_id, repo, task, mode, criteria, candidates, key, lock)
                if spawn_record is not None and args and args[0] is tm._worker:
                    spawn_record.append([c["cli"] + "/" + c["model"] for c in args[6]])

            def start(self):
                pass

        try:
            with patch.object(tm.ai_memory_bridge, "verify_runtime"), \
                 patch.object(tm, "_quota", return_value={"fresh": True, "cache": {}}), \
                 patch.object(tm, "_subscriptions", return_value=routes or [
                     {"cli": "codex", "model": "gpt-6-luna", "effort": "low"},
                     {"cli": "claude", "model": "claude-opus-5-5", "effort": "high"},
                     {"cli": "claude", "model": "claude-sonnet-5-5", "effort": "medium"},
                 ]), \
                 patch.object(tm, "_installed_models", return_value={}), \
                 patch.object(tm, "_worker_capable", side_effect=lambda candidates: candidates), \
                 patch.object(tm, "_jev_choose",
                              side_effect=AssertionError("Jev must not run when a pin is set")), \
                 patch.object(tm, "_free_models", return_value=[]), \
                 patch.object(tm.threading, "Thread", FakeThread):
                result = tm.dispatch({"action": "submit", "repository": str(repo),
                                      "task": "inspect the repo", "mode": "analysis",
                                      "acceptance_criteria": [{"type": "git_diff_clean"}], **extra})
            return result
        finally:
            tm._root = old_root

    def test_cli_pin_keeps_only_that_cli_and_skips_jev(self):
        with tempfile.TemporaryDirectory() as tmp:
            spawn = []
            result = self._submit(tmp, {"cli": "claude"}, spawn_record=spawn)
            self.assertEqual(result["state"], "queued")
            self.assertEqual(spawn[0], ["claude/claude-opus-5-5", "claude/claude-sonnet-5-5"])

    def test_model_pin_keeps_only_that_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            spawn = []
            result = self._submit(tmp, {"model": "gpt-6-luna"}, spawn_record=spawn)
            self.assertEqual(result["state"], "queued")
            self.assertEqual(spawn[0], ["codex/gpt-6-luna"])

    def test_cli_and_model_pin_narrow_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            spawn = []
            result = self._submit(tmp, {"cli": "claude", "model": "claude-sonnet-5-5"},
                                  spawn_record=spawn)
            self.assertEqual(result["state"], "queued")
            self.assertEqual(spawn[0], ["claude/claude-sonnet-5-5"])

    def test_unmatched_pin_blocks_with_installed_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._submit(tmp, {"cli": "kimi", "model": "kimi-k5-thinking"})
            self.assertEqual(result["state"], "blocked")
            self.assertIn("route pin matched no eligible candidate", result["message"])
            self.assertIn("codex/gpt-6-luna", result["message"])
            self.assertIn("claude/claude-opus-5-5", result["message"])

    def test_invalid_pin_args_are_rejected_before_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn("cli must be", self._submit(tmp, {"cli": ""})["error"])
            self.assertIn("model must be", self._submit(tmp, {"model": 7})["error"])

    def test_unpinned_submit_still_ranks_with_jev(self):
        with tempfile.TemporaryDirectory() as tmp:
            spawn = []
            result = self._submit(tmp, {}, spawn_record=spawn)
            # Jev raises in this harness, so the unpinned path must surface that failure,
            # proving the pin branch, not the default path, skipped it above.
            self.assertEqual(result["state"], "blocked")

    def test_pinned_record_keeps_task_text_out_of_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            spawn = []
            result = self._submit(tmp, {"cli": "claude"}, spawn_record=spawn)
            raw = (Path(tmp) / "home" / "runtime" / "cli-tasks" / f"{result['task_id']}.json").read_text()
            self.assertNotIn("inspect the repo", raw)


if __name__ == "__main__":
    unittest.main()
