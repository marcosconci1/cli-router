import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import hermes_adapter
import task_manager as tm


class ProviderToggleTests(unittest.TestCase):
    def test_dotted_setting_is_read_through_adapter(self):
        ctx = Mock()
        ctx.get_config.return_value = {"claude": False, "kimi": True}
        with patch.object(hermes_adapter, "_CONTEXT", ctx):
            self.assertEqual(tm._provider_toggles(), {"claude": False, "kimi": True})
        ctx.get_config.assert_called_once_with("routing.providers", None)

    def test_absent_setting_leaves_everything_enabled(self):
        ctx = Mock()
        ctx.get_config.side_effect = lambda key, default: None if key == "routing.providers" else {}
        with patch.object(hermes_adapter, "_CONTEXT", ctx):
            candidates = [
                {"cli": "codex", "model": "gpt-6-luna"},
                {"cli": "claude", "model": "claude-opus-5-5"},
                {"cli": "agy", "model": "gemini-3.8-flash-low"},
            ]
            filtered = tm._filter_candidates_by_toggle(candidates)
            self.assertEqual(filtered, candidates)

    def test_missing_provider_in_dict_defaults_to_enabled(self):
        ctx = Mock()
        ctx.get_config.return_value = {"routing.providers": {"codex": False}}
        with patch.object(hermes_adapter, "_CONTEXT", ctx):
            candidates = [
                {"cli": "codex", "model": "gpt-6-luna"},
                {"cli": "claude", "model": "claude-opus-5-5"},
                {"cli": "agy", "model": "gemini-3.8-flash-low"},
            ]
            filtered = tm._filter_candidates_by_toggle(candidates)
            self.assertEqual([(c["cli"], c["model"]) for c in filtered], [
                ("claude", "claude-opus-5-5"),
                ("agy", "gemini-3.8-flash-low"),
            ])

    def test_toggle_off_removes_provider_candidates_and_toggle_on_restores(self):
        ctx = Mock()
        candidates = [
            {"cli": "codex", "model": "gpt-6-luna"},
            {"cli": "claude", "model": "claude-opus-5-5"},
        ]

        # Toggle claude off
        ctx.get_config.return_value = {"routing.providers": {"claude": False}}
        with patch.object(hermes_adapter, "_CONTEXT", ctx):
            filtered = tm._filter_candidates_by_toggle(candidates)
            self.assertEqual([(c["cli"], c["model"]) for c in filtered], [
                ("codex", "gpt-6-luna"),
            ])

        # Toggle claude on
        ctx.get_config.return_value = {"routing.providers": {"claude": True}}
        with patch.object(hermes_adapter, "_CONTEXT", ctx):
            filtered = tm._filter_candidates_by_toggle(candidates)
            self.assertEqual(filtered, candidates)

    def test_nested_settings_format_supported(self):
        ctx = Mock()
        candidates = [
            {"cli": "codex", "model": "gpt-6-luna"},
            {"cli": "claude", "model": "claude-opus-5-5"},
        ]
        # settings["routing"]["providers"]
        ctx.get_config.return_value = {"settings": {"routing": {"providers": {"codex": False}}}}
        with patch.object(hermes_adapter, "_CONTEXT", ctx):
            filtered = tm._filter_candidates_by_toggle(candidates)
            self.assertEqual([(c["cli"], c["model"]) for c in filtered], [
                ("claude", "claude-opus-5-5"),
            ])

        # cfg["routing"]["providers"]
        ctx.get_config.return_value = {"routing": {"providers": {"claude": False}}}
        with patch.object(hermes_adapter, "_CONTEXT", ctx):
            filtered = tm._filter_candidates_by_toggle(candidates)
            self.assertEqual([(c["cli"], c["model"]) for c in filtered], [
                ("codex", "gpt-6-luna"),
            ])

    def test_malformed_shape_fails_open(self):
        ctx = Mock()
        candidates = [
            {"cli": "codex", "model": "gpt-6-luna"},
            {"cli": "claude", "model": "claude-opus-5-5"},
        ]
        malformed_examples = [
            {"routing.providers": "not_a_dict"},
            {"routing.providers": ["codex", "claude"]},
            {"routing.providers": {"claude": "disabled"}},
            {"routing.providers": {"claude": None}},
            {"routing.providers": {123: False}},
            "not_a_dict_at_all",
        ]
        for bad_cfg in malformed_examples:
            with self.subTest(bad_cfg=bad_cfg):
                ctx.get_config.return_value = bad_cfg
                with patch.object(hermes_adapter, "_CONTEXT", ctx):
                    filtered = tm._filter_candidates_by_toggle(candidates)
                    self.assertEqual(filtered, candidates)

    def test_claude_candidates_appear_despite_no_provider_record(self):
        quota = {"fresh": True, "cache": {"providers": {
            "openai-codex": {"windows": [{"used_percent": 10}]},
        }}}
        installed = {
            "codex": {"gpt-6-luna": ["low"]},
            "claude": {
                "claude-opus-5-5": ["low", "high"],
                "claude-sonnet-5-5": ["medium"],
                "claude-haiku-5-5": ["low"],
            },
            "agy": {},
            "kimi": {},
        }
        routes = tm._subscriptions(quota, installed)
        claude_routes = [r for r in routes if r["cli"] == "claude"]
        self.assertEqual(len(claude_routes), 4)
        for r in claude_routes:
            self.assertEqual(r["quota_reason"], tm.CLAUDE_QUOTA_UNKNOWN)

    def test_codex_and_others_require_provider_record(self):
        quota = {"fresh": True, "cache": {"providers": {}}}
        installed = {
            "codex": {"gpt-6-luna": ["low"]},
            "agy": {"gemini-4-argon-low": ["low"]},
            "kimi": {"kimi-code/k3.1-pro": ["low"]},
            "claude": {"claude-opus-5-5": ["low"]},
        }
        routes = tm._subscriptions(quota, installed)
        # Codex, agy, kimi should NOT appear without a provider record
        self.assertFalse(any(r["cli"] == "codex" for r in routes))
        self.assertFalse(any(r["cli"] == "agy" for r in routes))
        self.assertFalse(any(r["cli"] == "kimi" for r in routes))
        # Claude should appear
        self.assertTrue(any(r["cli"] == "claude" for r in routes))

    def test_claude_excluded_when_toggle_disables_claude(self):
        ctx = Mock()
        ctx.get_config.return_value = {"routing.providers": {"claude": False}}
        with patch.object(hermes_adapter, "_CONTEXT", ctx):
            candidates = [
                {"cli": "codex", "model": "gpt-6-luna"},
                {"cli": "claude", "model": "claude-opus-5-5"},
            ]
            filtered = tm._filter_candidates_by_toggle(candidates)
            self.assertEqual([c["cli"] for c in filtered], ["codex"])

    def test_routing_event_recorded_when_toggle_hides_candidates(self):
        task_id = "f" * 32
        ctx = Mock()
        ctx.get_config.return_value = {"routing.providers": {"claude": False, "kimi": False}}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old_root = tm._root
            tm._root = lambda: root
            try:
                tm._write(task_id, {"task_id": task_id, "events": []})
                with patch.object(hermes_adapter, "_CONTEXT", ctx):
                    candidates = [
                        {"cli": "codex", "model": "gpt-6-luna"},
                        {"cli": "claude", "model": "claude-opus-5-5"},
                        {"cli": "kimi", "model": "kimi-code/k3.1-pro"},
                    ]
                    filtered = tm._filter_candidates_by_toggle(candidates, task_id=task_id)
                    self.assertEqual([c["cli"] for c in filtered], ["codex"])
                    task_data = tm._read(task_id)
                    events = task_data.get("events", [])
                    toggle_events = [e for e in events if e.get("event") == "provider_toggle"]
                    self.assertEqual(len(toggle_events), 1)
                    evt = toggle_events[0]
                    self.assertEqual(evt["question"], "provider_toggle")
                    self.assertEqual(evt["choice"], "claude,kimi")
                    self.assertEqual(evt["options"], ["claude", "kimi"])
            finally:
                tm._root = old_root

    def test_no_routing_event_recorded_when_no_candidates_hidden(self):
        task_id = "e" * 32
        ctx = Mock()
        ctx.get_config.return_value = {"routing.providers": {"claude": True}}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old_root = tm._root
            tm._root = lambda: root
            try:
                tm._write(task_id, {"task_id": task_id, "events": []})
                with patch.object(hermes_adapter, "_CONTEXT", ctx):
                    candidates = [
                        {"cli": "codex", "model": "gpt-6-luna"},
                        {"cli": "claude", "model": "claude-opus-5-5"},
                    ]
                    filtered = tm._filter_candidates_by_toggle(candidates, task_id=task_id)
                    self.assertEqual(len(filtered), 2)
                    task_data = tm._read(task_id)
                    events = task_data.get("events", [])
                    toggle_events = [e for e in events if e.get("event") == "provider_toggle"]
                    self.assertEqual(len(toggle_events), 0)
            finally:
                tm._root = old_root

    def test_review_routing_applies_toggle(self):
        task_id = "d" * 32
        ctx = Mock()
        ctx.get_config.return_value = {"routing.providers": {"claude": False}}
        candidates = [
            {"cli": "claude", "model": "claude-opus-5-5", "effort": "low"},
            {"cli": "kimi", "model": "kimi-code/k3.1-pro", "effort": None},
            {"cli": "agy", "model": "gemini-3.8-flash-low", "effort": "low"},
        ]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            old_root = tm._root
            tm._root = lambda: root
            try:
                tm._write(task_id, {"task_id": task_id, "events": []})
                with patch.object(hermes_adapter, "_CONTEXT", ctx), \
                     patch.object(tm, "_candidate_has_quota", return_value=True), \
                     patch.object(tm, "_jev_choose", side_effect=lambda task, cs, **kw: cs[:2]) as jev_choose, \
                     patch.object(tm, "_run_candidate", return_value={"outcome": "needs_verification", "summary": "VERDICT: PASS"}):
                    result = tm._external_verification(task_id, repo, "review", [], candidates, candidates[0], None, 1)
                    # Ensure jev_choose was called with only kimi and agy (claude filtered out)
                    first_call_candidates = jev_choose.call_args_list[0][0][1]
                    self.assertNotIn("claude", [c["cli"] for c in first_call_candidates])
                    self.assertIn("kimi", [c["cli"] for c in first_call_candidates])
                    self.assertIn("agy", [c["cli"] for c in first_call_candidates])
            finally:
                tm._root = old_root

    def test_submit_all_candidates_hidden_by_toggle_blocks(self):
        ctx = Mock()
        ctx.get_config.return_value = {"routing.providers": {"codex": False, "claude": False}}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            old_root = tm._root
            tm._root = lambda: root
            route = {"cli": "codex", "model": "gpt-6-luna", "effort": "low"}
            completed_git = subprocess.CompletedProcess([], 0, stdout=str(repo.resolve()) + "\n")
            try:
                with patch.object(hermes_adapter, "_CONTEXT", ctx), \
                     patch.object(tm.ai_memory_bridge, "verify_runtime"), \
                     patch.object(tm, "_quota", return_value={"fresh": True, "cache": {}}), \
                     patch.object(tm, "_subscriptions", return_value=[route]), \
                     patch.object(tm, "_installed_models", return_value={}), \
                     patch.object(tm, "_worker_capable", side_effect=lambda c: c), \
                     patch.object(tm, "_free_models", return_value=[]), \
                     patch.object(tm, "_git_readonly", return_value=completed_git):
                    result = tm.dispatch({"action": "submit", "repository": str(repo), "task": "some task",
                                          "mode": "analysis", "acceptance_criteria": [{"type": "git_diff_clean"}]})
                self.assertEqual(result["state"], "blocked")
                self.assertEqual(result["message"], "Jev found no suitable eligible route")
                task_data = tm._read(result["task_id"])
                events = task_data.get("events", [])
                toggle_events = [e for e in events if e.get("event") == "provider_toggle"]
                self.assertEqual(len(toggle_events), 1)
                self.assertEqual(toggle_events[0]["choice"], "codex")
            finally:
                tm._root = old_root


if __name__ == "__main__":
    unittest.main()
