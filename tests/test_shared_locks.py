import fcntl
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import task_manager as tm


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path.resolve()


class SharedLockTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        base = Path(self._temp.name)
        home = base / "home"
        home.mkdir()
        old_root = tm._root
        tm._root = lambda: home
        self.addCleanup(lambda: setattr(tm, "_root", old_root))
        self.repo = _git_repo(base / "repo")

    def _task(self, task_id, state):
        tm._write(task_id, {"task_id": task_id, "state": state,
                            "repository": str(self.repo),
                            "repo_lock_key": tm._repo_key(self.repo), "owner_hash": "x"})

    def _submit(self, mode):
        route = {"cli": "codex", "model": "gpt-6-luna", "effort": "low"}
        threads = []

        class ThreadStub:
            def __init__(self, **kwargs):
                self.args = kwargs["args"]
                threads.append(self)

            def start(self):
                pass

        patches = [
            patch.object(tm, "_repo", return_value=self.repo),
            patch.object(tm.ai_memory_bridge, "verify_runtime"),
            patch.object(tm, "_quota", return_value={"fresh": True, "cache": {}}),
            patch.object(tm, "_subscriptions", return_value=[route]),
            patch.object(tm, "_installed_models", return_value={}),
            patch.object(tm, "_worker_capable", side_effect=lambda candidates: candidates),
            patch.object(tm, "_filter_candidates_by_toggle", side_effect=lambda candidates, **kwargs: candidates),
            patch.object(tm.threading, "Thread", ThreadStub),
        ]
        with ExitStack() as stack:
            for mocked in patches:
                stack.enter_context(mocked)
            result = tm.dispatch({"action": "submit", "repository": str(self.repo),
                                  "task": "inspect repository", "mode": mode,
                                  "cli": "codex", "model": "gpt-6-luna",
                                  "acceptance_criteria": [{"type": "git_diff_clean"}]})
        for thread in threads:
            thread.args[-1].close()
        if result.get("task_id"):
            tm._THREADS.pop(result["task_id"], None)
        return result

    def _hold_lock(self, lock_type):
        holder = tm._lock_path(self.repo).open("a+")
        fcntl.flock(holder.fileno(), lock_type | fcntl.LOCK_NB)
        self.addCleanup(holder.close)
        self.addCleanup(fcntl.flock, holder.fileno(), fcntl.LOCK_UN)
        return holder

    def test_analysis_submit_is_accepted_while_shared_lock_is_held(self):
        self._hold_lock(fcntl.LOCK_SH)
        result = self._submit("analysis")
        self.assertTrue(result.get("task_id"), result)
        self.assertEqual(result["state"], "queued")

    def test_change_submit_is_refused_while_shared_lock_is_held(self):
        self._hold_lock(fcntl.LOCK_SH)
        result = self._submit("change")
        self.assertEqual(result, {"ok": False, "error": tm._busy_message(self.repo)})

    def test_analysis_submit_is_refused_while_exclusive_lock_is_held(self):
        self._hold_lock(fcntl.LOCK_EX)
        result = self._submit("analysis")
        self.assertEqual(result, {"ok": False, "error": tm._busy_message(self.repo)})

    def test_analysis_submit_does_not_reap_with_shared_lock_held(self):
        task_id = "a" * 32
        self._task(task_id, "running")
        self._hold_lock(fcntl.LOCK_SH)
        result = self._submit("analysis")
        self.assertTrue(result.get("task_id"), result)
        self.assertEqual(tm._read(task_id)["state"], "running")

    def test_change_submit_reaps_dead_record_without_another_lock(self):
        task_id = "b" * 32
        self._task(task_id, "running")
        result = self._submit("change")
        self.assertTrue(result.get("task_id"), result)
        self.assertEqual(tm._read(task_id)["state"], "interrupted")


if __name__ == "__main__":
    unittest.main()
