import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import task_manager as tm


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        quota_patch = patch.object(tm, "_candidate_has_quota", return_value=True)
        quota_patch.start()
        self.addCleanup(quota_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_root = tm._root
        tm._root = lambda: self.root / "home"

    def tearDown(self):
        tm._root = self.old_root
        self.temp.cleanup()

    def _owned_task(self, state="running"):
        task_id = "a" * 32
        token = "owner-token"
        tm._write(task_id, {
            "task_id": task_id,
            "state": state,
            "owner_hash": hashlib.sha256(token.encode()).hexdigest(),
        })
        return task_id, token

    def _repo(self):
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "seed.txt").write_text("seed\n")
        return repo

    def test_wait_checks_ownership_and_returns_when_transition_arrives(self):
        task_id, token = self._owned_task()
        self.assertIn("not owned", tm.dispatch({"action": "wait", "task_id": task_id})["error"])
        self.assertIn("timeout_seconds", tm.dispatch({
            "action": "wait", "task_id": task_id, "owner_token": token, "timeout_seconds": -1,
        })["error"])

        class Transition:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def wait(self, _remaining):
                data = tm._read(task_id)
                data.update(state="completed", phase="completed")
                tm._write(task_id, data)
                return True

        with patch.object(tm, "_STATE_CHANGED", Transition()):
            result = tm.dispatch({
                "action": "wait", "task_id": task_id, "owner_token": token, "timeout_seconds": 10,
            })
        self.assertEqual(result["state"], "completed")

    def _commit(self, repo, message):
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", message], check=True)
        return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    def test_committed_work_passes_file_changed_even_with_clean_status(self):
        repo = self._repo()
        before = self._commit(repo, "baseline")
        (repo / "seed.txt").write_text("worker change\n")
        self._commit(repo, "worker")
        self.assertEqual(tm._git_changed_paths(repo), [])
        checks = tm._independent_checks(repo, before, "change", {})
        self.assertEqual(checks["changed_files"], ["seed.txt"])
        self.assertFalse(checks["head_unchanged"])
        self.assertTrue(tm._acceptance(repo, [{"type": "file_changed", "path": "seed.txt"}],
                                       checks["changed_files"], checks)["passed"])

    def test_committed_and_uncommitted_files_are_checked_together(self):
        repo = self._repo()
        before = self._commit(repo, "baseline")
        (repo / "broken.py").write_text("def broken(:\n")
        self._commit(repo, "worker")
        (repo / "seed.txt").write_text("pending change\n")
        (repo / "new.txt").write_text("untracked\n")
        checks = tm._independent_checks(repo, before, "change", {})
        self.assertEqual(checks["changed_files"], ["broken.py", "new.txt", "seed.txt"])
        self.assertFalse(checks["checks"]["python_compile"]["ok"])

    def test_committed_whitespace_errors_fail_diff_check(self):
        repo = self._repo()
        before = self._commit(repo, "baseline")
        (repo / "seed.txt").write_text("trailing space \n")
        self._commit(repo, "worker")
        checks = tm._independent_checks(repo, before, "change", {})
        self.assertFalse(checks["checks"]["git_diff_clean"]["ok"])

    def test_committing_unchanged_user_edits_does_not_attribute_them_to_worker(self):
        repo = self._repo()
        before = self._commit(repo, "baseline")
        (repo / "user.txt").write_text("user edit\n")
        baseline = tm._path_digests(repo, tm._git_changed_paths(repo))
        (repo / "seed.txt").write_text("worker edit\n")
        self._commit(repo, "worker")
        checks = tm._independent_checks(repo, before, "change", baseline)
        self.assertEqual(checks["changed_files"], ["seed.txt"])

    def test_compact_active_status_directs_wait_and_shows_acceptance_failures(self):
        task_id, token = self._owned_task()
        data = tm._read(task_id)
        failure = {"criterion": "file_changed", "path": "missing.txt", "ok": False,
                   "reason": "file did not change"}
        data["verification"] = {"acceptance": {"passed": False, "criteria": [failure]}}
        tm._write(task_id, data)
        result = tm.dispatch({"action": "status", "task_id": task_id, "owner_token": token})
        self.assertEqual(result["next_action"]["action"], "wait")
        self.assertEqual(result["verification"]["acceptance_failures"], [failure])
        self.assertNotIn("owner_token", result["next_action"])

    def test_compact_status_hides_attempts_and_memory_by_default(self):
        task_id, token = self._owned_task("completed")
        data = tm._read(task_id)
        data.update(
            attempts=[{"outcome": "verified"}],
            ai_memory={"workstream_id": str(uuid.UUID(task_id))},
            events=[{"event": "task_completed", "phase": "completed"}],
            verification={
                "independent_checks_passed": True,
                "changed_files": ["done.py"],
                "acceptance": {"passed": True},
                "external": {"passed": True, "summary": "tests passed"},
            },
        )
        tm._write(task_id, data)
        compact = tm.dispatch({"action": "status", "task_id": task_id, "owner_token": token})
        self.assertNotIn("attempts", compact)
        self.assertNotIn("ai_memory", compact)
        self.assertTrue(compact["verification"]["acceptance_passed"])
        self.assertEqual(compact["latest_event"]["event"], "task_completed")
        detailed = tm.dispatch({
            "action": "status", "task_id": task_id, "owner_token": token, "include_history": True,
        })
        self.assertEqual(detailed["attempts"], [{"outcome": "verified"}])
        self.assertIn("ai_memory", detailed)

    def test_review_runs_cheapest_then_strongest_even_when_first_passes(self):
        repo = self._repo()
        implementer = {"cli": "codex", "model": "implementation", "effort": "low"}
        cheap = {"cli": "kimi", "model": "cheap", "effort": None}
        strong = {"cli": "codex", "model": "strong", "effort": "high"}
        with patch.object(tm, "_jev_choose", return_value=[cheap, strong]) as choose, \
             patch.object(tm, "_run_candidate", return_value={
                 "outcome": "needs_verification", "summary": "VERDICT: PASS",
             }) as run:
            result = tm._external_verification(
                "a" * 32, repo, "review", [], [implementer, cheap, strong], implementer, None, 2)
        self.assertEqual([call.args[0] for call in run.call_args_list], [cheap, strong])
        self.assertEqual([call.kwargs["attempt_index"] for call in run.call_args_list], [2, 3])
        self.assertEqual(choose.call_args.kwargs["purpose"], "review")
        self.assertTrue(result["passed"])
        self.assertEqual(len(result["reviews"]), 2)

    def test_two_review_verdicts_are_both_required_and_runtime_failures_stop(self):
        repo = self._repo()
        routes = [{"cli": "codex", "model": "cheap"}, {"cli": "kimi", "model": "strong"}]
        cases = [
            ([{"outcome": "needs_verification", "summary": "VERDICT: FAIL"},
              {"outcome": "needs_verification", "summary": "VERDICT: PASS"}], 2),
            ([{"outcome": "memory_blocked", "reason": "ledger unavailable"}], 1),
            ([{"outcome": "resource_blocked", "reason": "worker resource queue expired"}], 1),
            ([{"outcome": "account_stop", "reason": "quota exhausted"}], 1),
        ]
        for outcomes, count in cases:
            with self.subTest(outcomes=outcomes), \
                 patch.object(tm, "_jev_choose", return_value=routes), \
                 patch.object(tm, "_run_candidate", side_effect=outcomes) as run:
                result = tm._external_verification("b" * 32, repo, "review", [], routes, routes[0], None, 2)
                self.assertFalse(result["passed"])
                self.assertEqual(run.call_count, count)

    def test_one_model_at_two_efforts_cannot_complete_two_reviews(self):
        repo = self._repo()
        routes = [{"cli": "codex", "model": "same", "effort": effort} for effort in ["low", "high"]]
        with patch.object(tm, "_jev_choose", return_value=routes), \
             patch.object(tm, "_run_candidate", return_value={
                 "outcome": "needs_verification", "summary": "VERDICT: PASS",
             }) as run:
            result = tm._external_verification("c" * 32, repo, "review", [], routes, routes[0], None, 2)
        self.assertFalse(result["passed"])
        self.assertEqual(run.call_count, 1)
        self.assertIn("review incomplete", result["summary"])

    def test_external_verifier_uses_snapshot_fresh_context_and_original_memory_repo(self):
        repo = self._repo()
        implementer = {"cli": "codex", "model": "implementation", "effort": "low"}
        verifier = {"cli": "kimi", "model": "verification", "effort": None}
        captured = {}

        def run(candidate, _task, snapshot, _mode, _task_id, _criteria, _key, **kwargs):
            captured.update(kwargs)
            captured["snapshot"] = snapshot
            self.assertNotEqual(snapshot, repo)
            (snapshot / "seed.txt").write_text("changed only in snapshot\n")
            return {
                "outcome": "needs_verification",
                "summary": "relevant tests passed\nVERDICT: PASS",
                "ai_memory": {"workstream_id": "b" * 36},
            }

        with patch.object(tm, "_jev_choose", return_value=[verifier, implementer]) as choose, \
             patch.object(tm, "_run_candidate", side_effect=run):
            result = tm._external_verification(
                "c" * 32, repo, "original task", [{"type": "file_exists", "path": "seed.txt"}],
                [implementer, verifier], implementer, None, 2)
        self.assertTrue(result["passed"])
        self.assertEqual(choose.call_args.args[1], [implementer, verifier])
        self.assertEqual(captured["memory_repo"], repo)
        self.assertEqual(captured["role"], "verify")
        self.assertFalse(captured["new_workstream"])
        self.assertNotIn("memory_context", captured)
        self.assertFalse(captured["snapshot"].exists())
        self.assertEqual((repo / "seed.txt").read_text(), "seed\n")

    def test_external_verification_requires_an_explicit_pass_verdict(self):
        repo = self._repo()
        route = {"cli": "codex", "model": "implementation", "effort": "low"}
        with patch.object(tm, "_jev_choose", return_value=[route]), \
             patch.object(tm, "_run_candidate", return_value={
                 "outcome": "needs_verification", "summary": "tests passed but no verdict",
             }):
            result = tm._external_verification(
                "d" * 32, repo, "task", [], [route], route, None, 2)
        self.assertFalse(result["passed"])
        self.assertIn("Missing required", result["summary"])

    def test_verifier_wrapper_stays_in_original_repository_scope(self):
        # Codex state is bound only when present, so create it in a hermetic home instead of relying on the host.
        self.fake_home = self.root / "fakehome"
        (self.fake_home / ".codex").mkdir(parents=True)
        repo = self._repo()
        snapshot = self.root / "snapshot"
        snapshot.mkdir()
        captured = {}

        class Process:
            returncode = 0

            def communicate(self, **_kwargs):
                return "VERDICT: PASS", ""

        def start(command, **kwargs):
            launcher = command[command.index("--executable") + 1]
            captured.update(kwargs, command=command, script=Path(launcher).read_text())
            return Process()

        route = {"cli": "codex", "model": "verification", "effort": "low"}
        with patch.dict("os.environ", {"AI_MEMORY_AUTH_TOKEN": "mock-token"}), \
             patch.object(tm.ai_memory_bridge, "_parent_env", return_value={"AI_MEMORY_AUTH_TOKEN": "mock-token"}), \
             patch.object(tm.Path, "home", return_value=self.fake_home), \
             patch.object(tm.subprocess, "Popen", side_effect=start), \
             patch.object(tm.shutil, "which", side_effect=lambda name, **_: f"/usr/bin/{name}"), \
             patch.object(tm, "_installed_cli", return_value="/usr/bin/codex"), \
             patch.object(tm.ai_memory_bridge, "verify_runtime"), \
             patch.object(tm.ai_memory_bridge, "find_workstream", return_value="a" * 36), \
             patch.object(tm.ai_memory_bridge, "verify_attempt", return_value=2):
            result = tm._run_candidate(
                route, "verify", snapshot, "change", "f" * 32, [],
                role="verify", memory_repo=repo, new_workstream=False)
        self.assertEqual(result["outcome"], "needs_verification")
        self.assertEqual(captured["cwd"], repo)
        command = captured["command"]
        # The snapshot is aliased at the repository path, so Codex records ai-memory's cwd.
        self.assertEqual(command[command.index("--cd") + 1], str(repo))
        self.assertEqual(command[command.index("--sandbox") + 1], "danger-full-access")
        self.assertIn("--fresh", command)
        self.assertIn(f"--bind {snapshot} {repo}", captured["script"])
        self.assertIn(f"--chdir {repo}", captured["script"])
        self.assertIn("--ro-bind / /", captured["script"])
        self.assertIn(str(self.fake_home / ".codex"), captured["script"])

    def test_codex_work_attempt_keeps_its_own_sandbox(self):
        repo = self._repo()
        captured = {}

        class Process:
            returncode = 0

            def communicate(self, **_kwargs):
                return "done", ""

        def start(command, **kwargs):
            captured.update(kwargs, command=command)
            return Process()

        route = {"cli": "codex", "model": "implementation", "effort": "low"}
        with patch.dict("os.environ", {"AI_MEMORY_AUTH_TOKEN": "mock-token"}), \
             patch.object(tm.ai_memory_bridge, "_parent_env", return_value={"AI_MEMORY_AUTH_TOKEN": "mock-token"}), \
             patch.object(tm.subprocess, "Popen", side_effect=start), \
             patch.object(tm, "_installed_cli", return_value="/usr/bin/codex"), \
             patch.object(tm.ai_memory_bridge, "verify_runtime"), \
             patch.object(tm.ai_memory_bridge, "find_workstream", return_value="a" * 36), \
             patch.object(tm.ai_memory_bridge, "verify_attempt", return_value=1):
            tm._run_candidate(route, "work", repo, "change", "f" * 32, [])
        command = captured["command"]
        self.assertEqual(command[command.index("--executable") + 1], "/usr/bin/codex")
        self.assertEqual(command[command.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(command[command.index("--cd") + 1], str(repo))
        self.assertNotIn("--fresh", command)

    def test_kimi_verifier_aliases_snapshot_and_starts_fresh(self):
        repo = self._repo()
        snapshot = self.root / "snapshot"
        snapshot.mkdir()
        captured = {}

        class Process:
            returncode = 0

            def communicate(self, **_kwargs):
                return "VERDICT: PASS", ""

        def start(command, **kwargs):
            launcher = command[command.index("--executable") + 1]
            captured.update(kwargs, command=command, script=Path(launcher).read_text())
            return Process()

        route = {"cli": "kimi", "model": "kimi-code/k3.1-pro", "effort": None}
        with patch.dict("os.environ", {"AI_MEMORY_AUTH_TOKEN": "mock-token"}), \
             patch.object(tm.ai_memory_bridge, "_parent_env", return_value={"AI_MEMORY_AUTH_TOKEN": "mock-token"}), \
             patch.object(tm.subprocess, "Popen", side_effect=start), \
             patch.object(tm.ai_memory_bridge, "verify_runtime"), \
             patch.object(tm.ai_memory_bridge, "find_workstream", return_value="a" * 36), \
             patch.object(tm.ai_memory_bridge, "verify_attempt", return_value=2):
            result = tm._run_candidate(
                route, "verify", snapshot, "change", "f" * 32, [],
                role="verify", memory_repo=repo, new_workstream=False)
        self.assertEqual(result["outcome"], "needs_verification")
        self.assertEqual(captured["cwd"], repo)
        self.assertIn("--fresh", captured["command"])
        self.assertIn(f"--bind {snapshot} {repo}", captured["script"])
        self.assertIn(f"--chdir {repo}", captured["script"])
        self.assertNotIn(f"--chdir {snapshot}", captured["script"])

    def test_external_failure_allows_exactly_one_repair(self):
        repo = self._repo()
        task_id = "e" * 32
        tm._write(task_id, {"task_id": task_id, "state": "queued"})
        routes = [{"cli": "codex", "model": f"model-{index}", "effort": "low"}
                  for index in range(3)]
        outcome = {
            "outcome": "needs_verification",
            "summary": "implemented",
            "ai_memory": {
                "workstream_id": str(uuid.UUID(task_id)),
                "workstream_name": tm.ai_memory_bridge.workstream_name(task_id),
                "latest_sequence": 1,
            },
        }
        checks = {"checks": {"git_diff_clean": {"ok": True}},
                  "changed_files": ["done.py"], "head_unchanged": False}
        with patch.object(tm.ai_memory_bridge, "verify_runtime"), \
             patch.object(tm.ai_memory_bridge, "read_context",
                          return_value=(f"Code task {task_id} attempt 1", 1)), \
             patch.object(tm, "_run_candidate", return_value=outcome) as run, \
             patch.object(tm, "_independent_checks", return_value=checks), \
             patch.object(tm, "_external_verification", return_value={
                 "passed": False, "summary": "VERDICT: FAIL",
             }) as verify, \
             patch.object(tm, "_jev_handoff", return_value=True) as handoff:
            tm._worker(task_id, repo, "task", "change",
                       [{"type": "file_changed", "path": "done.py"}], routes, None)
        data = tm._read(task_id)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(handoff.call_count, 1)
        self.assertEqual(data["state"], "failed_verification")
        self.assertIn("repair limit reached", data["message"])
        self.assertNotIn("private task prompt", json.dumps(data))


    def test_repair_on_a_reviewer_harness_starts_a_fresh_native_session(self):
        repo = self._repo()
        task_id = "d" * 32
        tm._write(task_id, {"task_id": task_id, "state": "queued"})
        routes = [{"cli": "codex", "model": f"model-{index}", "effort": "low"}
                  for index in range(3)]
        outcome = {
            "outcome": "needs_verification",
            "summary": "implemented",
            "ai_memory": {
                "workstream_id": str(uuid.UUID(task_id)),
                "workstream_name": tm.ai_memory_bridge.workstream_name(task_id),
                "latest_sequence": 1,
            },
        }
        checks = {"checks": {"git_diff_clean": {"ok": True}},
                  "changed_files": ["done.py"], "head_unchanged": False}
        review = {"passed": False, "summary": "VERDICT: FAIL", "reviews": [
            {"route": {"cli": "codex", "model": "reviewer-model"},
             "worker_outcome": "needs_verification", "attempt_index": 2}]}
        def verify(*args, **kwargs):
            tm._record_route_warning(task_id, "review", RuntimeError("private task prompt"))
            return review
        with patch.object(tm.ai_memory_bridge, "verify_runtime"), \
             patch.object(tm.ai_memory_bridge, "read_context",
                          return_value=(f"Code task {task_id} attempt 1", 1)), \
             patch.object(tm, "_run_candidate", return_value=outcome) as run, \
             patch.object(tm, "_independent_checks", return_value=checks), \
             patch.object(tm, "_external_verification", side_effect=verify), \
             patch.object(tm, "_jev_handoff", return_value=True):
            tm._worker(task_id, repo, "task", "change",
                       [{"type": "file_changed", "path": "done.py"}], routes, None)
        self.assertEqual(run.call_count, 2)
        self.assertFalse(run.call_args_list[0].kwargs["fresh_session"])
        self.assertTrue(run.call_args_list[1].kwargs["fresh_session"])
        data = tm._read(task_id)
        self.assertEqual(len([e for e in data["events"] if e["event"] == "route_warning"]), 2)
        self.assertEqual(len(data["route_warnings"]), 1)
        self.assertNotIn("private task prompt", json.dumps(data))

if __name__ == "__main__":
    unittest.main()
