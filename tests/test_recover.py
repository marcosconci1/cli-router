import fcntl
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import task_manager as tm


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path.resolve()


class RecoverTests(unittest.TestCase):
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
        self.other = _git_repo(base / "other")

    def _task(self, tid, state, repo):
        tm._write(tid, {"task_id": tid, "state": state, "repository": str(repo),
                        "repo_lock_key": tm._repo_key(repo), "owner_hash": "x"})

    def test_running_task_is_recovered_with_checkpoint(self):
        tid = "a" * 32
        self._task(tid, "running", self.repo)
        result = tm.dispatch({"action": "recover", "repository": str(self.repo)})
        self.assertTrue(result["ok"])
        self.assertEqual(result["recovered"], [tid])
        self.assertEqual(result["already_interrupted"], [])
        data = tm._read(tid)
        self.assertEqual(data["state"], "interrupted")
        self.assertIn("checkpoint", data)

    def test_busy_repository_is_not_reaped(self):
        tid = "b" * 32
        self._task(tid, "running", self.repo)
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl,sys,time; f=open(sys.argv[1],'a+'); fcntl.flock(f.fileno(), fcntl.LOCK_EX);"
             " print('locked', flush=True); time.sleep(30)", str(tm._lock_path(self.repo))],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        result = tm.dispatch({"action": "recover", "repository": str(self.repo)})
        self.assertEqual(result, {"ok": False, "error": "repository is busy; a worker may be active"})
        self.assertEqual(tm._read(tid)["state"], "running")

    def test_other_repository_task_is_untouched(self):
        tid = "c" * 32
        self._task(tid, "running", self.other)
        result = tm.dispatch({"action": "recover", "repository": str(self.repo)})
        self.assertEqual(result, {"ok": True, "recovered": [], "already_interrupted": []})
        self.assertEqual(tm._read(tid)["state"], "running")

    def test_already_interrupted_task_is_reported(self):
        tid = "d" * 32
        self._task(tid, "interrupted", self.repo)
        before = tm._read(tid)
        result = tm.dispatch({"action": "recover", "repository": str(self.repo)})
        self.assertEqual(result, {"ok": True, "recovered": [], "already_interrupted": [tid]})
        self.assertEqual(tm._read(tid), before)


if __name__ == "__main__":
    unittest.main()
