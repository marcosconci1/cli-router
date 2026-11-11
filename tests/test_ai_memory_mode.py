import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import doctor
import hermes_adapter
import task_manager as tm


class AiMemoryModeTests(unittest.TestCase):
    def test_mode_resolution_defaults_and_configured_values(self):
        with patch.object(hermes_adapter, "has_context", return_value=False):
            self.assertEqual(tm._ai_memory_mode(), "auto")

        for config in ({}, {"ai-memory": "bogus"}, {"ai-memory": []}):
            with self.subTest(config=config), \
                 patch.object(hermes_adapter, "has_context", return_value=True), \
                 patch.object(hermes_adapter, "get_config", return_value=config):
                self.assertEqual(tm._ai_memory_mode(), "auto")

        for expected in ("enabled", "disabled", "auto"):
            with self.subTest(expected=expected), \
                 patch.object(hermes_adapter, "has_context", return_value=True), \
                 patch.object(hermes_adapter, "get_config", return_value={"ai-memory": expected}):
                self.assertEqual(tm._ai_memory_mode(), expected)

    def _submit(self, configured_mode, verify_side_effect=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        repo = root / "repo"
        repo.mkdir()
        state = root / "state"
        candidate = {"cli": "codex", "provider": "openai-codex", "model": "gpt-6-luna",
                     "effort": "low"}
        quota = {"fresh": True, "cache": {"providers": {}}}
        verify = patch.object(tm.ai_memory_bridge, "verify_runtime", side_effect=verify_side_effect)
        self.addCleanup(verify.stop)
        verify_mock = verify.start()
        with patch.object(tm, "_root", return_value=state), \
             patch.object(tm, "_repo", return_value=repo), \
             patch.object(tm, "_repo_key", return_value="repo-key"), \
             patch.object(tm, "_lock_path", return_value=root / "repo.lock"), \
             patch.object(tm, "_recover_orphaned_tasks"), \
             patch.object(tm, "_ai_memory_mode", return_value=configured_mode), \
             patch.object(tm, "_quota", return_value=quota), \
             patch.object(tm, "_subscriptions", return_value=[candidate]), \
             patch.object(tm, "_installed_models", return_value={}), \
             patch.object(tm, "_candidate_has_quota", return_value=True), \
             patch.object(tm.threading.Thread, "start", return_value=None):
            result = tm.dispatch({"action": "submit", "repository": str(repo), "task": "inspect",
                                  "mode": "analysis", "cli": "codex",
                                  "acceptance_criteria": [{"type": "file_exists", "path": "README.md"}]})
            data = tm._read(result["task_id"]) if result.get("task_id") else None
        return result, data, verify_mock

    def test_auto_degrades_and_records_warning_and_mode(self):
        result, data, verify = self._submit(
            "auto", RuntimeError("ai-memory server unavailable"))
        self.assertEqual(result["state"], "queued")
        self.assertEqual(data["ai_memory"]["mode"], "standalone")
        self.assertIn("ai-memory unavailable; running standalone (single attempt, no handoff memory)",
                      data["route_warnings"])
        event = next(event for event in data["events"] if event["event"] == "ai_memory_mode")
        self.assertEqual(event["mode"], "standalone")
        verify.assert_called_once()

    def test_disabled_skips_runtime_check(self):
        result, data, verify = self._submit("disabled")
        self.assertEqual(result["state"], "queued")
        self.assertEqual(data["ai_memory"]["mode"], "standalone")
        verify.assert_not_called()

    def test_enabled_keeps_blocking_on_runtime_failure(self):
        result, data, verify = self._submit("enabled", RuntimeError("ai-memory server unavailable"))
        self.assertEqual(result["state"], "blocked")
        self.assertIn("ai-memory prerequisite failed", result["message"])
        verify.assert_called_once()

    def test_standalone_candidate_runs_raw_cli_without_memory_auth_or_wrapper(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.dict("os.environ", {}, clear=True), \
             patch.object(tm, "_installed_cli", return_value="/usr/bin/codex"), \
             patch.object(tm.worker_resources, "scope_command", side_effect=lambda command, *_: command), \
             patch.object(tm.worker_resources, "acquire", return_value=type(
                 "Lease", (), {"fd": 0, "close": lambda self: None})()), \
             patch.object(tm.subprocess, "Popen", return_value=type(
                 "Process", (), {"returncode": 0, "communicate": lambda self, **_: ("", "")})()) as popen, \
             patch.object(tm.ai_memory_bridge, "managed_command", side_effect=AssertionError("managed wrapper called")), \
             patch.object(tm.ai_memory_bridge, "verify_runtime", side_effect=AssertionError("runtime check called")), \
             patch.object(tm.ai_memory_bridge, "verify_attempt", side_effect=AssertionError("ledger check called")):
            outcome = tm._run_candidate(
                {"cli": "codex", "model": "gpt-6-luna", "effort": "low"}, "inspect",
                Path(temporary), "change", "a" * 32, [], memory_mode="standalone")
        command = popen.call_args.args[0]
        self.assertEqual(command[:2], ["/usr/bin/codex", "codex"])
        self.assertEqual(outcome["outcome"], "needs_verification")
        self.assertIsNone(outcome["ai_memory"])

    def test_standalone_worker_stops_after_one_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            state = root / "state"
            task_id = "b" * 32
            candidates = [
                {"cli": "codex", "provider": "openai", "model": "first", "effort": "low"},
                {"cli": "kimi", "provider": "moonshot", "model": "second", "effort": None},
            ]
            with patch.object(tm, "_root", return_value=state), \
                 patch.object(tm, "_lock_path", return_value=root / "repo.lock"), \
                 patch.object(tm, "_write", wraps=tm._write), \
                 patch.object(tm, "_git_readonly", return_value=type("Result", (), {"stdout": ""})()), \
                 patch.object(tm, "_git_changed_paths", return_value=[]), \
                 patch.object(tm, "_path_digests", return_value={}), \
                 patch.object(tm, "_candidate_has_quota", return_value=True), \
                 patch.object(tm, "_independent_checks", return_value={"changed_files": []}), \
                 patch.object(tm, "_run_candidate", return_value={"outcome": "model_unavailable"}) as run, \
                 patch.object(tm.ai_memory_bridge, "verify_runtime", side_effect=AssertionError("runtime check called")), \
                 patch.object(tm.ai_memory_bridge, "read_context", side_effect=AssertionError("context read called")), \
                 patch.object(tm, "_jev_handoff", side_effect=AssertionError("handoff called")):
                tm._write(task_id, {"task_id": task_id, "state": "queued",
                                    "ai_memory": {"mode": "standalone"}})
                tm._worker(task_id, repo, "inspect", "change", [], candidates, None)
                data = tm._read(task_id)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs["memory_mode"], "standalone")
        self.assertEqual(data["state"], "failed_verification")
        self.assertEqual(len(data["attempts"]), 1)
        self.assertIsNone(data["attempts"][0]["ai_memory"])
        self.assertNotIn("memory_check_started", [event["event"] for event in data["events"]])

    def test_doctor_status_follows_configured_mode(self):
        class Context:
            def __init__(self, mode):
                self.mode = mode

            def get_config(self, name, default=None):
                return {"settings": {"ai-memory": self.mode}}

        failure = doctor.ai_memory_bridge.AiMemoryError("memory unavailable")
        for mode, expected, side_effect in (
                ("disabled", "skip", None),
                # Intentional (structured degraded records): an explicitly chosen, failing ai-memory warns.
                ("auto", "warn", failure),
                ("enabled", "warn", failure),
                ("enabled", "ok", None)):
            with self.subTest(mode=mode, expected=expected), \
                 patch.object(doctor.hermes_adapter, "has_context", return_value=True), \
                 patch.object(doctor.hermes_adapter, "get_config", return_value={"settings": {"ai-memory": mode}}), \
                 patch.object(doctor.ai_memory_bridge, "verify_runtime", side_effect=side_effect) as verify:
                result = doctor._check_ai_memory()
                self.assertEqual(result["status"], expected)
                if mode == "disabled":
                    self.assertEqual(result["hint"], "ai-memory integration disabled by configuration")
                    verify.assert_not_called()
                elif expected == "warn":
                    self.assertTrue(result["hint"].startswith("chosen but"))
                    self.assertTrue(result["chosen"])
                else:
                    verify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
