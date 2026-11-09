import importlib.util
import json
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import task_manager as tm


class PolicyTests(unittest.TestCase):
    def setUp(self):
        quota_patch = patch.object(tm, "_candidate_has_quota", return_value=True)
        quota_patch.start()
        self.addCleanup(quota_patch.stop)
        # Route tests never reach OpenRouter: seed the benchmark cache with an empty, fresh index.
        import time
        tm.route_benchmark._CACHE.update(at=time.time(), index={"models": {}, "max_coding": 0,
                                                                 "max_intelligence": 0})

    def test_review_routing_uses_cost_then_quality_objectives(self):
        requests = []
        class Context:
            def get_config(self, name, default=None):
                return {"backend": "openrouter", "model": "typesafe/jev-1.14"}
        class Service:
            def __init__(self, ctx): pass
            def run(self, request):
                requests.append(request)
                return {"ok": True, "answers": {"route": {"choice": "route_0"}}}
        candidates = [{"cli": "kimi", "model": "cheap"},
                      {"cli": "codex", "model": "strong", "effort": "high"}]
        with patch.object(tm.hermes_adapter, "_CONTEXT", Context()), patch.object(tm.hermes_adapter, "_jev_service_cls", return_value=Service):
            self.assertEqual(tm._jev_choose("review this change", candidates, purpose="review"), candidates)
        self.assertIn("cheapest suitable", requests[0]["questions"]["route"]["instructions"])
        self.assertIn("strongest available", requests[1]["questions"]["route"]["instructions"])
        self.assertNotEqual(requests[0]["questions"]["route"]["instructions"],
                            requests[1]["questions"]["route"]["instructions"])

    def test_unknown_and_zero_quota_are_unavailable(self):
        self.assertEqual(tm._remaining({"windows": []}), (False, "quota remaining is unknown"))
        self.assertEqual(tm._remaining({"windows": [{"used_percent": None}]}), (False, "quota remaining is unknown"))
        self.assertEqual(tm._remaining({"windows": [{"used_percent": float("nan")}]}),
                         (False, "quota remaining is unknown"))
        self.assertEqual(tm._remaining({"windows": [{"used_percent": True}]}),
                         (False, "quota remaining is unknown"))
        self.assertEqual(tm._remaining({"windows": [{"remaining": float("inf")}]}),
                         (False, "quota remaining is unknown"))
        self.assertEqual(tm._remaining({"windows": [{"used_percent": 10}, {"used_percent": None}]}),
                         (False, "quota remaining is unknown"))
        self.assertEqual(tm._remaining({"windows": [{"used_percent": 10}], "details": ["shared pool"]}),
                         (False, "shared quota pool mapping is unknown"))
        self.assertEqual(tm._remaining({"windows": [{"used_percent": 100}]}), (False, "quota exhausted"))
        self.assertTrue(tm._remaining({"windows": [{"used_percent": 30}]})[0])

    def test_window_past_its_reset_time_no_longer_limits(self):
        past, future = "2026-01-01T00:00:00+00:00", "2999-01-01T00:00:00Z"
        self.assertEqual(tm._remaining({"windows": [{"used_percent": 100, "reset_at": past},
                                                    {"used_percent": 48, "reset_at": future}]}),
                         (True, "quota available"))
        self.assertEqual(tm._remaining({"windows": [{"used_percent": 100, "reset_at": future}]}),
                         (False, "quota exhausted"))
        self.assertEqual(tm._remaining({"windows": [{"used_percent": 85, "reset_at": future}]}),
                         (False, "quota near limit (20% reserve)"))
        # An unparseable reset time is not evidence of a reset.
        self.assertEqual(tm._remaining({"windows": [{"used_percent": 100, "reset_at": "later"}]}),
                         (False, "quota exhausted"))

    def test_claude_record_applies_reserve_and_fails_open_without_windows(self):
        installed = {"codex": {}, "agy": {}, "kimi": {}, "claude": {"claude-sonnet-5-5": ["medium"]}}
        def routes(record):
            return [r for r in tm._subscriptions({"fresh": True, "cache": {"providers": {"claude": record}}},
                                                 installed) if r["cli"] == "claude"]
        self.assertEqual(routes({"label": "claude", "windows": [{"used_percent": 85}]}), [])
        ok = routes({"label": "claude", "windows": [{"used_percent": 54}, {"used_percent": 48}]})
        self.assertEqual([(r["quota_reason"], r["account"]) for r in ok], [("quota available", "claude")])
        unknown = routes({"label": "claude", "unavailable_reason": "http-429", "windows": []})
        self.assertEqual([r["quota_reason"] for r in unknown], [f"{tm.CLAUDE_QUOTA_UNKNOWN} (http-429)"])

    def test_quota_reserve_checks_each_percentage_window(self):
        for used in (80, 90, 99, 99.9):
            self.assertFalse(tm._remaining({"windows": [{"used_percent": used}]})[0])
        self.assertTrue(tm._remaining({"windows": [{"used_percent": 79.9}]})[0])
        self.assertFalse(tm._remaining({"windows": [
            {"used_percent": 20}, {"used_percent": 95}]})[0])
        self.assertTrue(tm._remaining({"windows": [{"remaining": 1}]})[0])

    def test_model_failures_advance_but_account_failures_stop(self):
        self.assertEqual(tm._failure_category("Model rate limit reached", ""), "model_rate_limited")
        self.assertEqual(tm._failure_category("Unknown model", ""), "model_unavailable")
        self.assertEqual(tm._failure_category("", "HTTP 429 insufficient quota"), "quota_limited")
        self.assertEqual(tm._failure_category("permission denied", ""), "account_stop")

    def test_free_candidates_are_assessed_in_exact_rank_order(self):
        import types
        from unittest.mock import patch
        seen = []
        class Context:
            def get_config(self, name, default=None):
                return {"backend": "openrouter", "model": "typesafe/jev-1.14"}
        class Service:
            def __init__(self, ctx): pass
            def run(self, request):
                seen.append(request["state"]["candidate"]["model"])
                return {"ok": True, "answers": {"fit": {"choice": "eligible"}}}
        fake = types.ModuleType("test_jev.service")
        fake.Service = Service
        fake.MODELS = {}
        candidates = [{"cli": "opencode", "model": f"rank-{i}:free", "api_rank": i} for i in range(1, 16)]
        with patch.object(tm.hermes_adapter, "_CONTEXT", Context()), patch.dict("sys.modules", {"test_jev.service": fake}):
            selected = tm._jev_choose("task", candidates, one_at_a_time=True)
        self.assertEqual(seen, [f"rank-{i}:free" for i in range(1, 16)])
        self.assertEqual(selected, candidates)

    def test_jev_route_retries_a_failed_request_once(self):
        import types
        calls = []
        class Context:
            def get_config(self, name, default=None):
                return {"backend": "openrouter", "model": "typesafe/jev-1.14"}
        class Service:
            def __init__(self, ctx): pass
            def run(self, request):
                calls.append(request)
                if len(calls) == 1:
                    return {"ok": False}
                return {"ok": True, "answers": {"route": {"choice": "route_0"}}}
        fake = types.ModuleType("test_jev.service")
        fake.Service = Service
        fake.MODELS = {}
        candidates = [{"cli": "codex", "model": "gpt-6-luna", "effort": "low"}]
        with patch.object(tm.hermes_adapter, "_CONTEXT", Context()), patch.dict("sys.modules", {"test_jev.service": fake}):
            self.assertEqual(tm._jev_choose("task", candidates), candidates)
        self.assertEqual(len(calls), 2)

    def test_jev_route_stops_after_two_failed_requests(self):
        import types
        calls = []
        class Context:
            def get_config(self, name, default=None):
                return {"backend": "openrouter", "model": "typesafe/jev-1.14"}
        class Service:
            def __init__(self, ctx): pass
            def run(self, request):
                calls.append(request)
                return {"ok": False}
        fake = types.ModuleType("test_jev.service")
        fake.Service = Service
        fake.MODELS = {}
        candidates = [{"cli": "codex", "model": "gpt-6-luna", "effort": "low"}]
        with patch.object(tm.hermes_adapter, "_CONTEXT", Context()), patch.dict("sys.modules", {"test_jev.service": fake}):
            self.assertEqual(tm._jev_choose("task", candidates), candidates)
        self.assertEqual(len(calls), 2)

    def test_jev_route_requires_pinned_openrouter_model(self):
        import types
        from unittest.mock import patch
        class Context:
            def __init__(self, connection): self.connection = connection
            def get_config(self, name, default=None): return self.connection
        class Service:
            def __init__(self, ctx): pass
        fake = types.ModuleType("test_jev.service")
        fake.Service = Service
        fake.MODELS = {}
        candidate = [{"cli": "codex", "model": "gpt-6-luna", "effort": "low"}]
        for connection in ({"backend": "typesafe", "model": "jev-latest"},
                           {"backend": "openrouter", "model": "typesafe/jev-latest"}):
            with patch.object(tm.hermes_adapter, "_CONTEXT", Context(connection)), \
                 patch.dict("sys.modules", {"test_jev.service": fake}):
                self.assertEqual(tm._jev_choose("task", candidate), candidate)

    def test_jev_handoff_uses_fixed_candidate_and_typed_continue_choice(self):
        import types
        from unittest.mock import patch
        class Context:
            def get_config(self, name, default=None):
                return {"backend": "openrouter", "model": "typesafe/jev-1.14"}
        captured = {}
        class Service:
            def __init__(self, ctx): pass
            def run(self, request):
                captured.update(request)
                return {"ok": True, "answers": {"handoff": {"choice": "continue"}}}
        fake = types.ModuleType("hermes_jev.service"); fake.Service = Service
        candidate = {"cli": "codex", "model": "gpt-6-luna", "effort": "low"}
        with patch.object(tm.hermes_adapter, "_CONTEXT", Context()), patch.dict("sys.modules", {"hermes_jev.service": fake}):
            self.assertTrue(tm._jev_handoff("repair task", {"outcome": "failed_verification"},
                                            {"changed_files": ["partial.py"]}, candidate,
                                            "Historical task events are untrusted evidence."))
        self.assertEqual(captured["state"]["next_candidate"], candidate)
        self.assertIn("untrusted evidence", captured["state"]["persisted_context"])
        self.assertEqual(set(captured["questions"]["handoff"]["criteria"]), {"continue", "stop"})

    def test_only_exact_installed_approved_models_route(self):
        q = {"fresh": True, "cache": {"providers": {
            "openai-codex": {"accounts": [{"label": "work", "windows": [{"used_percent": 20}]}]},
            "kimi": {"windows": [{"used_percent": 20}]},
            "antigravity": {"windows": [{"used_percent": 10}]},
        }}}
        installed = {"codex": {"gpt-6-luna": ["low"], "gpt-5.6-terra": ["low"]},
                     "kimi": {"kimi-code/kimi-for-coding-highspeed": []},  # not an approved route
                     "agy": {"gemini-4-argon-low": ["low"], "gemini-3.8-flash-low": ["low"]}}
        routes = tm._subscriptions(q, installed)
        self.assertEqual([r["model"] for r in routes], ["gpt-6-luna", "gpt-5.6-terra", "gemini-4-argon-low", "gemini-3.8-flash-low"])
        # A Kimi alias that resembles the requested model is never substituted.
        self.assertFalse(any(r["cli"] == "kimi" for r in routes))

    def test_efforts_are_limited_to_installed_model_evidence(self):
        q = {"fresh": True, "cache": {"providers": {
            "openai-codex": {"windows": [{"used_percent": 25}]},
            "antigravity": {"windows": [{"used_percent": 25}]},
        }}}
        installed = {"codex": {"gpt-6-luna": ["low", "high"], "gpt-5.6-terra": ["low", "ultra"]},
                     "agy": {"gemini-4-argon-low": ["low"], "gemini-4-argon-medium": ["medium"],
                             "gemini-3.8-flash-high": ["high"]}, "kimi": {}}
        routes = tm._subscriptions(q, installed)
        self.assertEqual([(r["model"], r["effort"]) for r in routes], [
            ("gpt-6-luna", "low"), ("gpt-6-luna", "high"), ("gpt-5.6-terra", "low"),
            ("gpt-5.6-terra", "ultra"), ("gemini-4-argon-low", "low"),
            ("gemini-4-argon-medium", "medium"), ("gemini-3.8-flash-high", "high")])
        q["fresh"] = False
        self.assertEqual(tm._subscriptions(q, installed), [])

    def test_ambiguous_codex_accounts_are_not_routed(self):
        q = {"fresh": True, "cache": {"providers": {"openai-codex": {"accounts": [
            {"label": "first", "windows": [{"used_percent": 100}]},
            {"label": "second", "windows": [{"used_percent": 20}]},
        ]}}}}
        routes = tm._subscriptions(q, {"codex": {"gpt-6-luna": ["low"]}, "agy": {}, "kimi": {}})
        self.assertEqual(routes, [])

    def test_known_shared_subscription_pool_routes_once_and_exhaustion_excludes_accounts(self):
        q = {"fresh": True, "cache": {"providers": {"openai-codex": {"accounts": [
            {"label": "first", "shared_pool_id": "pool-1", "windows": [{"used_percent": 20}]},
            {"label": "second", "shared_pool_id": "pool-1", "windows": [{"used_percent": 20}]},
        ]}}}}
        models = {"codex": {"gpt-6-luna": ["low"]}, "agy": {}, "kimi": {}}
        routes = tm._subscriptions(q, models)
        self.assertEqual([route["model"] for route in routes], ["gpt-6-luna"])
        q["cache"]["providers"]["openai-codex"]["accounts"][0]["windows"][0]["used_percent"] = 100
        q["cache"]["providers"]["openai-codex"]["accounts"][1]["windows"][0]["used_percent"] = 100
        self.assertEqual(tm._subscriptions(q, models), [])

    def test_sandboxed_clis_route_only_when_bubblewrap_is_available(self):
        candidates = [{"cli": "codex", "model": "gpt-6-luna"}, {"cli": "agy", "model": "gemini-4-argon-high"},
                      {"cli": "claude", "model": "claude-opus-5-5"}, {"cli": "kimi", "model": "kimi-code/k3"}]
        with patch.object(tm.shutil, "which", return_value="/usr/bin/bwrap"):
            # AGY attempts are confirmed from the hook-captured session page, so AGY routes too.
            self.assertEqual(tm._worker_capable(candidates), candidates)
        with patch.object(tm.shutil, "which", return_value=None):
            self.assertEqual(tm._worker_capable(candidates), [candidates[0]])

    def test_kimi_subscriptions_route_with_exact_models(self):
        q = {"fresh": True, "cache": {"providers": {
            "anthropic": {"windows": [{"used_percent": 26}]},
            "kimi": {"unavailable_reason": "cli-token-expired"},
        }}}
        installed = {"codex": {}, "agy": {}, "claude": {},
                     "kimi": {"kimi-code/k3.1-pro": ["low", "high"],
                              "kimi-code/kimi-for-coding": ["low", "high"]}}
        routes = tm._subscriptions(q, installed)
        self.assertEqual([(r["cli"], r["model"], r["effort"]) for r in routes], [
            ("kimi", "kimi-code/k3.1-pro", None), ("kimi", "kimi-code/kimi-for-coding", None)])
        q["cache"]["providers"]["kimi"] = {"unavailable_reason": "no-credentials"}
        self.assertFalse(any(r["cli"] == "kimi" for r in tm._subscriptions(q, installed)))

    def test_sandbox_launcher_keeps_host_read_only_and_repo_writable_only_for_changes(self):
        repo = Path(self.tmp.name) if hasattr(self, "tmp") else Path(__file__).resolve().parent
        for mode, bind in (("analysis", "--ro-bind"), ("change", "--bind")):
            with patch.object(tm.shutil, "which", return_value="/usr/bin/bwrap"):
                isolated, launcher = tm._sandbox_launcher("kimi", "/usr/bin/kimi", repo, mode)
            try:
                script = Path(launcher).read_text()
                self.assertIn("--ro-bind / /", script)
                self.assertIn(f"{bind} {repo} {repo}", script)
                self.assertTrue(script.rstrip().endswith('/usr/bin/kimi "$@"'))
            finally:
                isolated.cleanup()

    @unittest.skipUnless(tm.shutil.which("bwrap") and tm.shutil.which("git"), "needs bubblewrap and git")
    def test_worker_git_metadata_binds_only_the_task_repository_own_git_dir(self):
        import subprocess
        import tempfile
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name).resolve()

        def git(*args):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                           check=True, capture_output=True)

        outer = root / "outer"          # stands in for the home dotfiles repository
        mono = outer / "mono"
        (mono / "modules" / "a").mkdir(parents=True)
        git("init", "-q", str(outer))
        git("init", "-q", str(mono))
        (mono / "modules" / "a" / "f.txt").write_text("1\n")
        git("-C", str(mono), "add", ".")
        git("-C", str(mono), "commit", "-qm", "init")
        git("-C", str(mono), "worktree", "add", "-q", str(root / "wt"))
        planted = outer / "planted"
        planted.mkdir()
        (planted / ".git").write_text(f"gitdir: {outer / '.git'}\n")
        snapshot = root / "snapshot"
        snapshot.mkdir()

        common = lambda path: (tm._git_metadata(path) or (None, None, None))[1]
        self.assertEqual(common(mono / "modules" / "a"), mono / ".git")
        self.assertEqual(common(mono), mono / ".git")  # never the enclosing repository
        self.assertEqual(common(root / "wt"), mono / ".git")
        self.assertIsNone(tm._git_metadata(planted))  # a planted .git file is refused
        self.assertIsNone(tm._git_metadata(snapshot))

        def script(path, mode, visible=None):
            isolated, launcher = tm._sandbox_launcher("kimi", "/usr/bin/kimi", path, mode, visible_repo=visible)
            try:
                return Path(launcher).read_text()
            finally:
                isolated.cleanup()

        git_dir = mono / ".git"
        protected = [f"--ro-bind {git_dir / name} {git_dir / name}" for name in ("HEAD", "config", "hooks")]
        change = script(mono / "modules" / "a", "change")
        self.assertIn(f"--bind {git_dir} {git_dir}", change)
        for bind in protected:
            self.assertIn(bind, change)
            self.assertGreater(change.index(bind), change.index(f"--bind {git_dir} {git_dir}"))
        root_task = script(mono, "change")
        self.assertNotIn(f"--bind {git_dir} {git_dir}", root_task)  # the repository bind covers it
        self.assertNotIn(str(outer / ".git"), root_task)
        for bind in protected:
            self.assertIn(bind, root_task)
        analysis = script(mono / "modules" / "a", "analysis")
        self.assertIn(f"--ro-bind {git_dir} {git_dir}", analysis)
        self.assertNotIn(f" --bind {git_dir}", analysis)
        worktree = script(root / "wt", "change")
        self.assertIn(f"--ro-bind {git_dir / 'worktrees' / 'wt' / 'HEAD'} {git_dir / 'worktrees' / 'wt' / 'HEAD'}",
                      worktree)
        # A review snapshot of a repository root protects the copy at the path the reviewer sees.
        import shutil
        copy = root / "copy"
        shutil.copytree(mono, copy, symlinks=True)
        review = script(copy, "change", visible=mono)
        self.assertIn(f"--ro-bind {copy / '.git' / 'hooks'} {git_dir / 'hooks'}", review)

    @unittest.skipUnless(tm.shutil.which("bwrap") and tm.shutil.which("git"), "needs bubblewrap and git")
    def test_subdirectory_task_checks_resolve_git_paths_from_the_work_tree_root(self):
        import subprocess
        import tempfile
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        mono = Path(scratch.name).resolve() / "mono"
        module = mono / "modules" / "a"
        module.mkdir(parents=True)

        def git(*args):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(mono), *args],
                           check=True, capture_output=True)

        git("init", "-q")
        (module / "ok.py").write_text("x = 1\n")
        (module / "dirty.txt").write_text("a\n")
        git("add", ".")
        git("commit", "-qm", "init")
        (module / "dirty.txt").write_text("user edit\n")  # dirty before the task starts
        before = subprocess.run(["git", "-C", str(mono), "rev-parse", "HEAD"],
                                capture_output=True, text=True, check=True).stdout.strip()
        baseline = tm._path_digests(module, tm._git_changed_paths(module))
        self.assertTrue(all(baseline.values()))  # digests read the real files

        (module / "ok.py").write_text("def broken(:\n")
        (module / "dirty.txt").write_text("worker edit\n")
        checks = tm._independent_checks(module, before, "change", baseline)
        self.assertEqual(checks["checks"]["python_compile"]["ok"], False)
        self.assertEqual(checks["changed_files"], ["modules/a/dirty.txt", "modules/a/ok.py"])
        accepted = tm._acceptance(module, [{"type": "file_changed", "path": "dirty.txt"},
                                           {"type": "file_changed", "path": "ok.py"},
                                           {"type": "file_changed", "path": "missing.txt"}],
                                  checks["changed_files"], checks)
        self.assertEqual([c["ok"] for c in accepted["criteria"]], [True, True, False])

        # An untracked directory below an enclosing repository (a stray review snapshot under
        # the home dotfiles repository) never gets that repository's metadata.
        stray = mono / "stray-snapshot"
        stray.mkdir()
        (stray / "copy.txt").write_text("copy\n")
        self.assertIsNone(tm._git_metadata(stray))
        self.assertEqual(tm._git_metadata(module)[1], mono / ".git")

    def test_git_hooks_path_inside_the_work_tree_stays_read_only(self):
        if not (tm.shutil.which("bwrap") and tm.shutil.which("git")):
            self.skipTest("needs bubblewrap and git")
        import subprocess
        import tempfile
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        repo = Path(scratch.name).resolve() / "husky"
        (repo / ".husky").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", ".husky"], check=True)
        isolated, launcher = tm._sandbox_launcher("kimi", "/usr/bin/kimi", repo, "change")
        try:
            self.assertIn(f"--ro-bind {repo / '.husky'} {repo / '.husky'}", Path(launcher).read_text())
        finally:
            isolated.cleanup()

    def test_review_sandbox_aliases_snapshot_to_memory_repository(self):
        import tempfile
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        snapshot = root / "snapshot"
        visible = root / "real-repository"
        snapshot.mkdir()
        visible.mkdir()
        with patch.object(tm.shutil, "which", return_value="/usr/bin/bwrap"):
            isolated, launcher = tm._sandbox_launcher(
                "kimi", "/usr/bin/kimi", snapshot, "change", visible_repo=visible)
        try:
            script = Path(launcher).read_text()
            self.assertIn("--ro-bind / /", script)
            self.assertIn(f"--bind {snapshot} {visible}", script)
            self.assertIn(f"--chdir {visible}", script)
            self.assertNotIn(f"--chdir {snapshot}", script)
        finally:
            isolated.cleanup()

    def test_jev_service_is_found_under_the_hyphenated_plugin_module_name(self):
        import types
        fake = types.ModuleType("hermes_plugins.hermes-jev.service")
        fake.Service, fake.MODELS = type("Service", (), {}), {}
        with patch.dict("sys.modules", {"hermes_plugins.hermes-jev.service": fake}):
            self.assertIs(tm.hermes_adapter._jev_service_cls(), fake.Service)

    def test_claude_summary_is_the_answer_text(self):
        raw = 'notice\n{"type":"result","result":"The secret word is lamplight.","usage":{"input_tokens":4}}'
        self.assertEqual(tm._claude_result_text(raw), "The secret word is lamplight.")
        self.assertEqual(tm._claude_result_text("plain failure text"), "plain failure text")

    def test_managed_command_maps_agy_to_antigravity_harness(self):
        import ai_memory_bridge as bridge
        with patch.object(bridge, "_cli", return_value="/usr/bin/ai-memory"):
            command = bridge.managed_command("0123456789abcdef0123456789abcdef", 1, "agy", "/tmp/launcher", ["--prompt=x"], {"HOME": "/h"}, True)
        self.assertEqual(command[command.index("--executable") + 2], "antigravity")

    def test_free_filter_preserves_catalog_order_and_ties(self):
        first = {"id": "x/one:free", "pricing": {"prompt": "0", "completion": "0"},
                 "supported_parameters": ["tools"], "context_length": 65536}
        second = {"id": "x/two:free", "pricing": {"prompt": "0", "completion": "0", "image": "0"},
                  "supported_parameters": ["tools", "tool_choice"], "context_length": 65536}
        bad_pricing = {"id": "x/paid:free", "pricing": {"prompt": "0", "completion": "0.0001"},
                       "supported_parameters": ["tools"], "context_length": 65536, "reasoning": {"efforts": ["low"]}}
        no_tools = {"id": "x/no-tools:free", "pricing": {"prompt": "0", "completion": "0"},
                    "supported_parameters": ["json"], "context_length": 65536}
        choice_only = {"id": "x/choice-only:free", "pricing": {"prompt": "0", "completion": "0"},
                       "supported_parameters": ["tool_choice"], "context_length": 65536}
        payload = json.dumps({"data": [first, bad_pricing, second, no_tools, choice_only]}).encode()
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size=-1): return payload
        opener = type("Opener", (), {"open": lambda self, *a, **k: Response()})()
        with patch("urllib.request.build_opener", return_value=opener):
            result = tm._free_models()
        self.assertEqual([r["model"] for r in result], ["x/one:free", "x/two:free"])
        self.assertEqual([r["api_rank"] for r in result], [1, 3])
        self.assertIsNone(result[0]["effort"])

    def test_free_filter_keeps_equal_and_unscored_catalog_order(self):
        rows = [{"id": f"x/{name}:free", "pricing": {"prompt": "0", "completion": "0"},
                 "supported_parameters": ["tools"], "context_length": 65536,
                 } for name in ("tie-a", "tie-b", "unscored")]
        rows[0]["intelligence_score"] = 90
        rows[1]["intelligence_score"] = 90
        payload = json.dumps({"data": rows}).encode()
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size=-1): return payload
        opener = type("Opener", (), {"open": lambda self, *a, **k: Response()})()
        with patch("urllib.request.build_opener", return_value=opener):
            result = tm._free_models()
        self.assertEqual([r["model"] for r in result], [row["id"] for row in rows])

    def test_catalog_pagination_and_failure(self):
        page2 = {"data": [{"id": "x/two:free", "pricing": {"prompt": "0", "completion": "0"},
                           "supported_parameters": ["tools"], "context_length": 65536}]}
        page1 = {"data": [{"id": "x/one:free", "pricing": {"prompt": "0", "completion": "0"},
                           "supported_parameters": ["tools"], "context_length": 65536}],
                 "links": {"next": "https://openrouter.ai/api/v1/models?sort=intelligence-high-to-low&offset=500&limit=500"}}
        class Response:
            def __init__(self, b): self.b = b
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size=-1): return json.dumps(self.b).encode()
        opener = type("Opener", (), {"__init__": lambda self: setattr(self, "n", 0),
                                     "open": lambda self, *a, **k: (setattr(self, "n", self.n + 1) or
                                          [Response(page1), Response(page2)][self.n - 1])})()
        with patch("urllib.request.build_opener", return_value=opener):
            result = tm._free_models()
        self.assertEqual([r["model"] for r in result], ["x/one:free", "x/two:free"])
        self.assertEqual(opener.n, 2)
        with patch("urllib.request.build_opener", return_value=type("Opener", (), {"open": lambda *a, **k: (_ for _ in ()).throw(OSError("offline"))})()):
            with self.assertRaises(OSError):
                tm._free_models()
        page1["links"]["next"] = "https://attacker.invalid/api/v1/models?sort=intelligence-high-to-low&offset=500"
        opener = type("Opener", (), {"open": lambda self, *a, **k: Response(page1)})()
        with patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(RuntimeError, "pagination target is not trusted"):
                tm._free_models()
        large = type("Response", (), {"__enter__": lambda self: self, "__exit__": lambda self, *a: None,
                                       "read": lambda self, size=-1: b"x" * size})()
        opener = type("Opener", (), {"open": lambda self, *a, **k: large})()
        with patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(RuntimeError, "response exceeded its size limit"):
                tm._free_models()

    def test_free_catalog_has_no_twelve_model_cap_and_each_task_starts_at_rank_one(self):
        rows = [{"id": f"x/rank-{i}:free", "pricing": {"prompt": "0", "completion": "0"},
                 "supported_parameters": ["tools"], "context_length": 65536} for i in range(1, 16)]
        payload = json.dumps({"data": rows}).encode()
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size=-1): return payload
        opener = type("Opener", (), {"open": lambda self, *a, **k: Response()})()
        with patch("urllib.request.build_opener", return_value=opener):
            first_task = tm._free_models()
            second_task = tm._free_models()
        self.assertEqual(len(first_task), 15)
        self.assertEqual(first_task[12]["model"], "x/rank-13:free")
        self.assertEqual([x["api_rank"] for x in first_task], list(range(1, 16)))
        self.assertEqual(first_task[0]["model"], second_task[0]["model"])
        self.assertEqual(second_task[0]["api_rank"], 1)
    def test_quota_tool_is_separate_and_no_native_dispatch_gate(self):
        import importlib.util
        captured = {}
        class Context:
            def register_tool(self, **kwargs): captured[kwargs["name"]] = kwargs
            def register_hook(self, name, fn): captured["hook"] = (name, fn)
            def register_skill(self, name, path): captured.setdefault("skills", []).append(name)
        from __init__ import register
        package_name = "cli_router_test_registration"
        spec = importlib.util.spec_from_file_location(package_name, ROOT / "__init__.py",
                                                       submodule_search_locations=[str(ROOT)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[package_name] = package
        spec.loader.exec_module(package)
        package.register(Context())
        self.assertIn("cli_router", captured)
        self.assertNotIn("hook", captured)
        self.assertFalse(hasattr(package, "_tool_gate"))
        quota_dir = ROOT.parent / "hermes-quota-viewer"
        if not quota_dir.is_dir():
            self.skipTest("optional sibling plugin hermes-quota-viewer is not installed")
        spec = importlib.util.spec_from_file_location("hermes_quota_viewer_test", quota_dir / "__init__.py")
        viewer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(viewer)
        quota_tool = {}
        viewer.register(type("Context", (), {"register_tool": lambda self, **kwargs: quota_tool.update(kwargs)})())
        self.assertEqual(quota_tool["toolset"], "code_quota_viewer")
        self.assertEqual(quota_tool["name"], "quota_status")

    def test_owner_token_required_for_status_and_cancel(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            old_root = tm._root
            tm._root = lambda: Path(temp)
            tid = "d" * 32
            token = "private-owner-token"
            tm._write(tid, {"task_id": tid, "state": "running",
                            "owner_hash": __import__("hashlib").sha256(token.encode()).hexdigest()})
            try:
                self.assertIn("not owned", tm.dispatch({"action": "status", "task_id": tid})["error"])
                status = tm.dispatch({"action": "status", "task_id": tid, "owner_token": token})
                self.assertEqual(status["state"], "running")
                self.assertNotIn("owner_hash", status)
            finally:
                tm._root = old_root

    def test_status_is_compact_by_default_and_history_is_opt_in(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            old_root = tm._root
            tm._root = lambda: Path(temp)
            task_id = "e" * 32
            token = "status-owner"
            events = [{"event": "attempt_started", "phase": "worker_running"},
                      {"event": "task_completed", "phase": "completed"}]
            tm._write(task_id, {"task_id": task_id, "state": "completed", "phase": "completed",
                                "events": events,
                                "owner_hash": __import__("hashlib").sha256(token.encode()).hexdigest()})
            try:
                compact = tm.dispatch({"action": "status", "task_id": task_id, "owner_token": token})
                self.assertNotIn("events", compact)
                self.assertEqual(compact["event_count"], 2)
                self.assertEqual(compact["latest_event"], events[-1])
                self.assertNotIn("owner_hash", compact)
                detailed = tm.dispatch({"action": "status", "task_id": task_id,
                                        "owner_token": token, "include_history": True})
                self.assertEqual(detailed["events"], events)
                self.assertNotIn("event_count", detailed)
                self.assertNotIn("latest_event", detailed)
                self.assertNotIn("owner_hash", detailed)
            finally:
                tm._root = old_root

    def test_status_recovers_abandoned_task_only_after_repo_lock_is_free(self):
        import subprocess
        import tempfile
        token = "recovery-owner"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            (repo / "seed").write_text("seed")
            subprocess.run(["git", "-C", str(repo), "add", "seed"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)
            (repo / "partial.py").write_text("uncommitted = True\n")
            tid = "e" * 32
            old_root = tm._root
            tm._root = lambda: root / "home"
            tm._write(tid, {"task_id": tid, "state": "running", "repository": str(repo.resolve()),
                            "mode": "change", "base_head": subprocess.run(
                                ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                                text=True, check=True).stdout.strip(),
                            "owner_hash": __import__("hashlib").sha256(token.encode()).hexdigest()})
            try:
                status = tm.dispatch({"action": "status", "task_id": tid, "owner_token": token})
                self.assertEqual(status["state"], "interrupted")
                self.assertIn("partial.py", status["checkpoint"]["changed_files"])
                self.assertIn("not relaunched", status["message"])
            finally:
                tm._root = old_root

    def test_real_worker_process_group_is_interrupted_then_reaped(self):
        import subprocess
        import sys
        code = ("import subprocess,sys,time; "
                "subprocess.Popen([sys.executable,'-c','import signal,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)']); time.sleep(60)")
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            tm._terminate_process_group(proc, grace_seconds=0.1)
            self.assertIsNotNone(proc.poll())
        finally:
            if proc.poll() is None:
                tm._terminate_process_group(proc, grace_seconds=0.1)
        with self.assertRaises(ProcessLookupError):
            import os
            os.killpg(proc.pid, 0)

    def test_task_text_is_not_written_to_state(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            old = tm._root
            tm._root = lambda: Path(d)
            try:
                # Exercise serialization contract directly.
                tm._write("a" * 32, {"task_id": "a" * 32, "state": "queued", "task_fingerprint": "x"})
                raw = (Path(d) / "runtime" / "cli-tasks" / ("a" * 32 + ".json")).read_text()
                self.assertNotIn("private task prompt", raw)
            finally:
                tm._root = old

    def test_plugin_profile_home_wins_over_process_environment(self):
        import tempfile
        import types
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "runtime"
            module_path = root / "source" / "profiles" / "code" / "plugins" / "cli-router" / "task_manager.py"
            constants = types.ModuleType("hermes_constants")
            constants.get_default_hermes_root = lambda: runtime
            with patch.object(tm, "__file__", str(module_path)), \
                 patch.dict("os.environ", {"HERMES_HOME": str(root / "launch")}), \
                 patch.dict("sys.modules", {"hermes_constants": constants}):
                self.assertEqual(tm._root(), runtime / "profiles" / "code")

    def test_task_manager_uses_environment_home_without_hermes_constants(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            module_path = Path(temp) / "plugins" / "cli-router" / "task_manager.py"
            with patch.object(tm, "__file__", str(module_path)), \
                 patch.dict("os.environ", {"HERMES_HOME": temp}), \
                 patch.dict("sys.modules", {"hermes_constants": None}):
                self.assertEqual(tm._root(), Path(temp))

    def test_transition_history_is_bounded(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            old_root = tm._root
            tm._root = lambda: Path(temp)
            data = {"task_id": "a" * 32, "state": "queued"}
            try:
                for index in range(tm.MAX_TASK_EVENTS + 5):
                    tm._transition(data, "routing", f"event_{index}")
                persisted = tm._read(data["task_id"])
                self.assertEqual(len(persisted["events"]), tm.MAX_TASK_EVENTS)
                self.assertEqual(persisted["events"][0]["event"], "event_5")
                self.assertEqual(persisted["events"][-1]["event"], f"event_{tm.MAX_TASK_EVENTS + 4}")
                self.assertEqual(persisted["phase"], "routing")
                self.assertTrue(persisted["updated_at"])
            finally:
                tm._root = old_root

    def test_submit_is_visible_during_routing_without_persisting_task_text(self):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            old_root = tm._root
            tm._root = lambda: root / "home"
            seen = []
            route = {"cli": "codex", "model": "gpt-6-luna", "effort": "low"}
            task_text = "private task prompt api_key=supersecret"

            def assess(_task, _candidates, **_kwargs):
                path = next((root / "home" / "runtime" / "cli-tasks").glob("*.json"))
                seen.append(json.loads(path.read_text()))
                return []

            try:
                with patch.object(tm.ai_memory_bridge, "verify_runtime"), \
                     patch.object(tm, "_quota", return_value={"fresh": True, "cache": {}}), \
                     patch.object(tm, "_subscriptions", return_value=[route]), \
                     patch.object(tm, "_installed_models", return_value={}), \
                     patch.object(tm, "_worker_capable", side_effect=lambda candidates: candidates), \
                     patch.object(tm, "_jev_choose", side_effect=assess), \
                     patch.object(tm, "_free_models", return_value=[]):
                    result = tm.dispatch({"action": "submit", "repository": str(repo), "task": task_text,
                                          "mode": "analysis", "acceptance_criteria": [{"type": "git_diff_clean"}]})
                self.assertEqual(seen[0]["phase"], "subscription_routing")
                self.assertEqual([event["event"] for event in seen[0]["events"]],
                                 ["task_submitted", "quota_check_started", "subscription_routing_started"])
                self.assertEqual(result["state"], "blocked")
                self.assertEqual(result["events"][-1]["event"], "task_blocked")
                status = tm.dispatch({"action": "status", "task_id": result["task_id"],
                                      "owner_token": result["owner_token"]})
                self.assertNotIn("owner_hash", status)
                raw = (root / "home" / "runtime" / "cli-tasks" / f"{result['task_id']}.json").read_text()
                self.assertNotIn(task_text, raw)
                self.assertNotIn("supersecret", raw)
            finally:
                tm._root = old_root

    def test_worker_records_attempt_verification_and_completion_timeline(self):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            (repo / "seed").write_text("seed")
            subprocess.run(["git", "-C", str(repo), "add", "seed"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)
            old_root = tm._root
            tm._root = lambda: root / "home"
            task_id = "b" * 32
            tm._write(task_id, {"task_id": task_id, "state": "queued"})
            outcome = {"outcome": "needs_verification", "summary": "done",
                       "ai_memory": {"workstream_id": str(uuid.UUID(task_id)),
                                     "workstream_name": tm.ai_memory_bridge.workstream_name(task_id),
                                     "latest_sequence": 1}}
            checks = {"checks": {"git_diff_clean": {"ok": True}},
                      "changed_files": ["done.py"], "head_unchanged": False}
            try:
                with patch.object(tm.ai_memory_bridge, "verify_runtime"), \
                     patch.object(tm, "_run_candidate", return_value=outcome), \
                     patch.object(tm, "_external_verification", return_value={"passed": True, "summary": "passed"}), \
                 patch.object(tm, "_independent_checks", return_value=checks):
                    tm._worker(task_id, repo, "private worker prompt", "change",
                               [{"type": "file_changed", "path": "done.py"}],
                               [{"cli": "codex", "model": "gpt-6-luna", "effort": "low"}], None)
                data = tm._read(task_id)
                events = [event["event"] for event in data["events"]]
                self.assertEqual(data["state"], "completed")
                self.assertEqual(data["phase"], "completed")
                self.assertEqual((data["current_attempt"], data["candidate_count"]), (1, 1))
                self.assertIn("attempt_started", events)
                self.assertIn("verification_started", events)
                self.assertIn("verification_finished", events)
                self.assertEqual(events[-1], "task_completed")
                attempt = data["attempts"][0]
                self.assertEqual(attempt["attempt"], 1)
                self.assertTrue(attempt["started_at"])
                self.assertTrue(attempt["finished_at"])
                raw = json.dumps(data)
                self.assertNotIn("private worker prompt", raw)
            finally:
                tm._root = old_root

    def test_cancel_records_lifecycle_transition(self):
        import tempfile
        task_id = "c" * 32
        with tempfile.TemporaryDirectory() as temp:
            old_root = tm._root
            tm._root = lambda: Path(temp)
            tm._write(task_id, {"task_id": task_id, "state": "running",
                                "owner_hash": __import__("hashlib").sha256(b"owner").hexdigest()})
            try:
                result = tm.dispatch({"action": "cancel", "task_id": task_id, "owner_token": "owner"})
                self.assertEqual(result["phase"], "cancelling")
                self.assertEqual(result["events"][-1]["event"], "cancellation_requested")
                self.assertNotIn("owner_hash", result)
            finally:
                tm._CANCEL.discard(task_id)
                tm._root = old_root

    def test_candidate_attempts_are_once_in_order_and_verification_can_advance(self):
        import subprocess
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            (repo / "seed.txt").write_text("seed\n")
            subprocess.run(["git", "-C", str(repo), "add", "seed.txt"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)
            old_root = tm._root
            tm._root = lambda: root / "home"
            task_id = "b" * 32
            tm._write(task_id, {"task_id": task_id, "state": "queued"})
            routes = [{"cli": "codex", "model": f"model-{n}", "effort": "low"} for n in range(15)]
            verification = iter([
                *([{"checks": {"baseline": {"ok": True}}, "changed_files": []}] * 12),
                {"checks": {"baseline": {"ok": True}}, "changed_files": []},
                {"checks": {"baseline": {"ok": False}}, "changed_files": ["partial.py"]},
                {"checks": {"baseline": {"ok": True}}, "changed_files": ["done.py"]},
            ])
            lock = tm._lock_path(repo).open("a+")
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            counts = {}
            def synced_result(*args, **kwargs):
                task = args[4]
                counts[task] = counts.get(task, 0) + 1
                result = ("model_unavailable", "model absent") if counts[task] <= 12 else ("needs_verification", "partial")
                if counts[task] == 15:
                    result = ("needs_verification", "done")
                return {"outcome": result[0], "reason": result[1], "summary": result[1],
                        "ai_memory": {"workstream_id": str(uuid.UUID(task)),
                                      "workstream_name": tm.ai_memory_bridge.workstream_name(task),
                                      "latest_sequence": counts[task]}}
            def previous_context(_cwd, stream, *, required_marker=None):
                task = next(task for task in counts if str(uuid.UUID(task)) == stream)
                packet = f"Code task {task} attempt {counts[task]}"
                if required_marker is not None and required_marker not in packet:
                    raise tm.ai_memory_bridge.AiMemoryError(
                        "prior attempt is absent from canonical memory; failover stopped")
                return (packet, counts[task])
            with patch.object(tm, "_run_candidate", side_effect=synced_result) as run, \
                 patch.object(tm.ai_memory_bridge, "verify_runtime"), \
                 patch.object(tm.ai_memory_bridge, "read_context", side_effect=previous_context), \
                 patch.object(tm, "_jev_handoff", return_value=True), \
                 patch.object(tm, "_external_verification", return_value={"passed": True, "summary": "passed"}), \
                 patch.object(tm, "_independent_checks", side_effect=lambda *a: next(verification)):
                tm._worker(task_id, repo, "task", "change",
                           [{"type": "file_exists", "path": "seed.txt"}], routes, None, lock)
            data = tm._read(task_id)
            self.assertEqual([call.args[0]["model"] for call in run.call_args_list], [f"model-{n}" for n in range(15)])
            self.assertEqual(data["attempts"][-1]["outcome"], "verified")
            self.assertEqual(data["state"], "completed")
            self.assertIn("partial.py", run.call_args_list[14].kwargs["memory_context"])
            self.assertIn("independent_verification", run.call_args_list[14].kwargs["memory_context"])
            exhausted_id = "d" * 32
            tm._write(exhausted_id, {"task_id": exhausted_id, "state": "queued"})
            lock = tm._lock_path(repo).open("a+")
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            def exhausted(*args, **kwargs):
                task = args[4]
                counts[task] = counts.get(task, 0) + 1
                return {"outcome": "model_unavailable", "reason": "absent",
                        "ai_memory": {"workstream_id": str(uuid.UUID(task)),
                                      "workstream_name": tm.ai_memory_bridge.workstream_name(task),
                                      "latest_sequence": counts[task]}}
            with patch.object(tm, "_run_candidate", side_effect=exhausted) as run_all, \
                 patch.object(tm.ai_memory_bridge, "verify_runtime"), \
                 patch.object(tm, "_jev_handoff", return_value=True), \
                 patch.object(tm.ai_memory_bridge, "read_context", side_effect=previous_context):
                tm._worker(exhausted_id, repo, "task", "change", [], routes, None, lock)
            self.assertEqual(run_all.call_count, 15)
            tm._root = old_root

    def test_missing_prior_attempt_from_memory_stops_before_retry(self):
        import fcntl
        import subprocess
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            (repo / "seed").write_text("seed")
            subprocess.run(["git", "-C", str(repo), "add", "seed"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)
            old_root = tm._root; tm._root = lambda: root / "home"
            task_id = "9" * 32
            tm._write(task_id, {"task_id": task_id, "state": "queued"})
            lock = tm._lock_path(repo).open("a+"); fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            ref = {"workstream_id": str(uuid.UUID(task_id)),
                   "workstream_name": tm.ai_memory_bridge.workstream_name(task_id),
                   "latest_sequence": 1}
            checks = {"checks": {"diff": {"ok": False}}, "changed_files": ["partial.py"],
                      "head_unchanged": True}
            try:
                def missing_marker(_cwd, _stream, *, required_marker=None):
                    if required_marker is not None:
                        raise tm.ai_memory_bridge.AiMemoryError(
                            "prior attempt is absent from canonical memory; failover stopped")
                    return ("ledger without attempt marker", 1)
                with patch.object(tm.ai_memory_bridge, "verify_runtime"), \
                     patch.object(tm.ai_memory_bridge, "read_context", side_effect=missing_marker), \
                     patch.object(tm, "_run_candidate", return_value={"outcome": "needs_verification",
                         "summary": "partial", "ai_memory": ref}) as run, \
                     patch.object(tm, "_external_verification", return_value={"passed": True, "summary": "passed"}), \
                 patch.object(tm, "_independent_checks", return_value=checks):
                    tm._worker(task_id, repo, "task", "change", [],
                               [{"cli": "codex", "model": "m1", "effort": "low"},
                                {"cli": "codex", "model": "m2", "effort": "low"}], None, lock)
                data = tm._read(task_id)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(data["state"], "blocked")
                self.assertIn("no next worker was started", data["message"])
            finally:
                tm._root = old_root

    def test_jev_handoff_stop_prevents_next_cli_attempt(self):
        import fcntl
        import subprocess
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            (repo / "seed").write_text("seed")
            subprocess.run(["git", "-C", str(repo), "add", "seed"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)
            old_root = tm._root; tm._root = lambda: root / "home"
            task_id = "8" * 32
            tm._write(task_id, {"task_id": task_id, "state": "queued"})
            lock = tm._lock_path(repo).open("a+"); fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            ref = {"workstream_id": str(uuid.UUID(task_id)),
                   "workstream_name": tm.ai_memory_bridge.workstream_name(task_id),
                   "latest_sequence": 2}
            checks = {"checks": {"diff": {"ok": False}}, "changed_files": ["partial.py"],
                      "head_unchanged": True}
            try:
                with patch.object(tm.ai_memory_bridge, "verify_runtime"), \
                     patch.object(tm.ai_memory_bridge, "read_context",
                                  return_value=(f"Code task {task_id} attempt 1", 2)), \
                     patch.object(tm, "_run_candidate", return_value={"outcome": "needs_verification",
                         "summary": "partial", "ai_memory": ref}) as run, \
                     patch.object(tm, "_jev_handoff", return_value=False) as assess, \
                     patch.object(tm, "_external_verification", return_value={"passed": True, "summary": "passed"}), \
                 patch.object(tm, "_independent_checks", return_value=checks):
                    tm._worker(task_id, repo, "task", "change", [],
                               [{"cli": "codex", "model": "m1", "effort": "low"},
                                {"cli": "codex", "model": "m2", "effort": "low"}], None, lock)
                data = tm._read(task_id)
                self.assertEqual(run.call_count, 1)
                assess.assert_called_once()
                self.assertEqual(data["state"], "blocked")
                self.assertEqual(data["ai_memory"]["handoff_decision"], "stop")
            finally:
                tm._root = old_root

    def test_duplicate_repository_lock_rejects_before_routing(self):
        import fcntl
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            import subprocess
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            old_root = tm._root
            tm._root = lambda: repo / "state"
            lock = tm._lock_path(repo).open("a+")
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with patch.object(tm, "_quota", side_effect=AssertionError("routing should not start")):
                    result = tm.dispatch({"action": "submit", "repository": str(repo), "task": "x", "mode": "analysis",
                                          "acceptance_criteria": [{"type": "file_exists", "path": "README"}]})
                self.assertIn("already owns", result["error"])
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                lock.close()
                tm._root = old_root

    def test_nested_paths_share_canonical_repo_lock_without_expanding_worker_scope(self):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            left, right = root / "left", root / "right"
            left.mkdir(); right.mkdir()
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            old_root = tm._root
            tm._root = lambda: root / "state"
            try:
                self.assertEqual(tm._lock_path(left), tm._lock_path(right))
                self.assertEqual(tm._repo(str(left)), left.resolve())
            finally:
                tm._root = old_root

    def test_submit_requires_nonempty_acceptance_and_empty_checks_cannot_pass(self):
        import subprocess
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            with patch.object(tm, "_quota", side_effect=AssertionError("invalid task reached routing")):
                result = tm.dispatch({"action": "submit", "repository": str(repo), "task": "inspect",
                                      "mode": "analysis", "acceptance_criteria": []})
            self.assertIn("acceptance_criteria", result["error"])
            self.assertFalse(tm._acceptance(repo, [], [], {"checks": {}})["passed"])
            record = {"checks": {"git_diff_clean": {"ok": True, "summary": "git diff --check passed"}}}
            self.assertTrue(tm._acceptance(repo, [{"type": "git_diff_clean"}], [], record)["passed"])
            record["checks"]["git_diff_clean"]["ok"] = False
            self.assertFalse(tm._acceptance(repo, [{"type": "git_diff_clean"}], [], record)["passed"])

    def test_git_metadata_checks_disable_repo_fsmonitor_execution(self):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            sentinel = root / "outside-write"
            helper = root / "fsmonitor"
            helper.write_text(f"#!/bin/sh\nprintf touched > '{sentinel}'\nprintf '\\n'\n")
            helper.chmod(0o700)
            subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", str(helper)], check=True)
            result = tm._git_readonly(repo, "status", "--short")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(sentinel.exists())

    def test_jev_failure_blocks_without_worker_fallback(self):
        import subprocess
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            old_root = tm._root; tm._root = lambda: root / "state"
            q = {"cache": {"providers": {}}, "age": 1, "fresh": True}
            routes = [{"cli": "codex", "provider": "openai-codex", "model": "gpt-6-luna", "effort": "low"}]
            try:
                with patch.object(tm.ai_memory_bridge, "verify_runtime"), \
                     patch.object(tm, "_quota", return_value=q), patch.object(tm, "_subscriptions", return_value=routes), \
                     patch.object(tm, "_jev_choose", side_effect=RuntimeError("Jev unavailable")), \
                     patch.object(tm, "_run_candidate", side_effect=AssertionError("worker fallback launched")):
                    result = tm.dispatch({"action": "submit", "repository": str(repo), "task": "inspect", "mode": "analysis",
                                          "acceptance_criteria": [{"type": "file_exists", "path": "README"}]})
                self.assertEqual(result["state"], "blocked")
                self.assertEqual(result["attempts"], [])
            finally:
                tm._root = old_root

    def test_memory_outage_blocks_before_quota_or_jev_assessment(self):
        import subprocess
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            old_root = tm._root; tm._root = lambda: root / "state"
            try:
                with patch.object(tm, "_ai_memory_mode", return_value="enabled"), \
                     patch.object(tm.ai_memory_bridge, "verify_runtime",
                                  side_effect=tm.ai_memory_bridge.AiMemoryError("ai-memory server unavailable")), \
                     patch.object(tm, "_quota", side_effect=AssertionError("quota ran during memory outage")), \
                     patch.object(tm, "_jev_choose", side_effect=AssertionError("Jev ran during memory outage")):
                    result = tm.dispatch({"action": "submit", "repository": str(repo), "task": "inspect",
                                          "mode": "analysis",
                                          "acceptance_criteria": [{"type": "file_exists", "path": "README"}]})
                self.assertEqual(result["state"], "blocked")
                self.assertIn("ai-memory prerequisite failed", result["message"])
            finally:
                tm._root = old_root


    def test_openrouter_credential_reader_uses_only_openrouter_entry(self):
        import tempfile
        import types
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            auth = Path(temp) / ".local/share/opencode/auth.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(json.dumps({"openrouter": {"type": "api", "key": "synthetic-openrouter-key"},
                                        "opencode": {"type": "api", "key": "synthetic-paid-key"}}))
            fake_secret_scope = types.ModuleType("agent.secret_scope")
            fake_secret_scope.get_secret = lambda _: None
            with patch.dict("sys.modules", {"agent.secret_scope": fake_secret_scope}), patch.object(Path, "home", return_value=Path(temp)):
                self.assertEqual(tm._openrouter_credential(), "synthetic-openrouter-key")
            auth.write_text(json.dumps({"opencode": {"type": "api", "key": "synthetic-paid-key"}}))
            with patch.dict("sys.modules", {"agent.secret_scope": fake_secret_scope}), patch.object(Path, "home", return_value=Path(temp)):
                self.assertIsNone(tm._openrouter_credential())

    def test_free_route_requires_fresh_exact_key_and_known_exhaustion_stops(self):
        import types
        from unittest.mock import patch
        package = types.ModuleType("_hermes_code_quota_plugin.quota_providers.openrouter")
        package._resolve_credentials = lambda: [("Key 2 (OpenCode)", "selected-key"),
                                                 ("Key 1 (native)", "other-key")]
        quota = {"fresh": True, "cache": {"providers": {"openrouter": {
            "windows": [{"label": "Key 2 (OpenCode) free models daily requests",
                         "used_percent": 78, "remaining": 11, "limit": 50}],
            "details": ["Account wallet reports shared account credits"],
        }}}}
        with patch.object(tm, "_quota_plugin", return_value=types.SimpleNamespace()), \
             patch.dict("sys.modules", {"_hermes_code_quota_plugin.quota_providers.openrouter": package}):
            self.assertEqual(tm._openrouter_quota_available(quota, "selected-key"), (True, "quota available"))
            self.assertFalse(tm._openrouter_quota_available(quota, "other-key")[0])
            exhausted = json.loads(json.dumps(quota))
            exhausted["cache"]["providers"]["openrouter"]["windows"][0]["remaining"] = 0
            exhausted["cache"]["providers"]["openrouter"]["windows"][0]["used_percent"] = 100
            self.assertFalse(tm._openrouter_quota_available(exhausted, "selected-key")[0])
            shared = json.loads(json.dumps(quota))
            shared["cache"]["providers"]["openrouter"]["details"].append("free request quota uses a shared pool")
            self.assertTrue(tm._openrouter_quota_available(shared, "selected-key")[0])
            unknown = json.loads(json.dumps(quota))
            unknown["cache"]["providers"]["openrouter"]["windows"][0]["remaining"] = None
            unknown["cache"]["providers"]["openrouter"]["windows"][0]["used_percent"] = None
            self.assertTrue(tm._openrouter_quota_available(unknown, "selected-key")[0])
            unavailable = json.loads(json.dumps(unknown))
            unavailable["cache"]["providers"]["openrouter"]["unavailable_reason"] = "authentication failed"
            self.assertFalse(tm._openrouter_quota_available(unavailable, "selected-key")[0])

    def test_openrouter_quota_error_includes_exception_cause(self):
        from unittest.mock import patch
        quota = {"fresh": True, "cache": {"providers": {"openrouter": {
            "windows": [{"label": "Key 1 (native) free models daily requests", "remaining": 10}],
        }}}}
        with patch.object(tm, "_quota_plugin", side_effect=RuntimeError("quota plugin boom")):
            available, reason = tm._openrouter_quota_available(quota, "selected-key")
        self.assertEqual(available, False)
        self.assertIn("RuntimeError", reason)
        self.assertIn("quota plugin boom", reason)

    def test_quota_view_expands_accounts_and_marks_unknown_remaining(self):
        import json
        import importlib.util
        import types
        from unittest.mock import patch
        quota_dir = ROOT.parent / "hermes-quota-viewer"
        if not quota_dir.is_dir():
            self.skipTest("optional sibling plugin hermes-quota-viewer is not installed")
        spec = importlib.util.spec_from_file_location("quota_viewer_test", quota_dir / "__init__.py")
        package = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(package)
        quota = {"cache": {"fetched_at": "2026-10-18T03:00:00+00:00", "providers": {
            "openai-codex": {"accounts": [
                {"label": "first", "windows": [{"label": "weekly", "used_percent": None, "reset_at": "later"}]},
                {"label": "second", "windows": [{"label": "weekly", "used_percent": 20, "reset_at": "later"}]},
            ]},
            "openrouter": {"windows": [{"label": "Key 1 free models daily requests", "used_percent": 86,
                                          "remaining": 7, "limit": 50, "unit": "requests"}],
                            "details": ["Account wallet via OpenCode: USD 1.00 remaining (shared account credits, shown once; not summed across keys)",
                                        "provider error: opaque-secret-991", "free request quota uses a shared pool"],
                            "account_balances": [{"currency": "USD", "total_balance": "1"}]},
        }}, "age": 5, "fresh": True}
        cache = types.SimpleNamespace(quota_cache_age_seconds=lambda: 5,
                                      read_quota_cache=lambda: quota["cache"])
        with patch.object(package, "_cache_module", return_value=cache):
            result = json.loads(package._handler({"refresh": False}))
        self.assertEqual([x["account"] for x in result["accounts"]], ["first", "second", "openrouter"])
        self.assertEqual(result["accounts"][0]["availability"], "unknown")
        self.assertEqual(result["accounts"][1]["windows"][0]["remaining_percent"], 80)
        self.assertEqual(result["accounts"][2]["free_model_limits"][0]["remaining"], 7)
        self.assertEqual(result["accounts"][2]["free_model_limits"][0]["unit"], "requests")
        self.assertEqual(result["accounts"][2]["paid_balances"][-1], {
            "account": "OpenCode", "currency": "USD", "remaining": "1.00", "unit": "USD", "shared": True})
        self.assertEqual(result["accounts"][2]["shared_pools"], ["provider reports a shared or pooled quota"])
        self.assertNotIn("opaque-secret-991", json.dumps(result))
        self.assertNotIn("details", result["accounts"][2])

    def test_cancelled_task_does_not_start_a_candidate(self):
        import subprocess
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            task_id = "c" * 32
            old_root = tm._root
            tm._root = lambda: root / "home"
            tm._write(task_id, {"task_id": task_id, "state": "queued"})
            tm._CANCEL.add(task_id)
            try:
                with patch.object(tm, "_run_candidate", side_effect=AssertionError("cancelled task launched")):
                    tm._worker(task_id, repo, "task", "analysis", [], [{"cli": "codex", "model": "m", "effort": "low"}], None)
                self.assertEqual(tm._read(task_id)["state"], "cancelled")
            finally:
                tm._CANCEL.discard(task_id)
                tm._root = old_root

    def test_cancel_sends_signal_to_owned_process_group(self):
        import tempfile
        from unittest.mock import patch
        class Process:
            pid = 31337
            def poll(self): return None
        task_id = "2" * 32
        with tempfile.TemporaryDirectory() as temp:
            old_root = tm._root; tm._root = lambda: Path(temp)
            tm._write(task_id, {"task_id": task_id, "state": "running",
                                "owner_hash": __import__("hashlib").sha256(b"owner").hexdigest()})
            proc = Process(); tm._PROCS[task_id] = proc
            try:
                with patch.object(tm, "_terminate_process_group") as terminate:
                    result = tm.dispatch({"action": "cancel", "task_id": task_id, "owner_token": "owner"})
                self.assertEqual(result["state"], "cancelling")
                terminate.assert_called_once_with(proc)
            finally:
                tm._PROCS.pop(task_id, None); tm._CANCEL.discard(task_id); tm._root = old_root

    def test_changed_free_offer_is_skipped_without_launch(self):
        import subprocess
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            task_id = "3" * 32
            old_root = tm._root; tm._root = lambda: root / "home"
            tm._write(task_id, {"task_id": task_id, "state": "queued"})
            candidate = {"cli": "opencode", "provider": "openrouter", "model": "x/free:free",
                         "effort": "low", "context": 65536, "api_rank": 1}
            try:
                with patch.object(tm, "_free_models", return_value=[]), \
                     patch.object(tm, "_run_candidate", side_effect=AssertionError("changed offer launched")):
                    tm._worker(task_id, repo, "task", "change", [], [candidate], "key")
                data = tm._read(task_id)
                self.assertEqual(data["attempts"][0]["outcome"], "pricing_or_capability_changed")
                self.assertEqual(data["state"], "blocked")
            finally:
                tm._root = old_root

    def test_account_stop_checkpoints_partial_files_before_stopping(self):
        import subprocess
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            (repo / "seed").write_text("seed")
            subprocess.run(["git", "-C", str(repo), "add", "seed"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)
            task_id = "4" * 32
            old_root = tm._root; tm._root = lambda: root / "home"
            tm._write(task_id, {"task_id": task_id, "state": "queued"})
            try:
                with patch.object(tm, "_run_candidate", return_value={"outcome": "account_stop", "reason": "quota",
                                    "ai_memory": {"workstream_id": "a" * 36,
                                                  "workstream_name": tm.ai_memory_bridge.workstream_name(task_id),
                                                  "latest_sequence": 1}}), \
                     patch.object(tm.ai_memory_bridge, "verify_runtime"), \
                     patch.object(tm, "_external_verification", return_value={"passed": True, "summary": "passed"}), \
                 patch.object(tm, "_independent_checks", return_value={"checks": {}, "changed_files": ["partial.py"]}):
                    tm._worker(task_id, repo, "task", "change", [],
                               [{"cli": "codex", "model": "gpt-6-luna", "effort": "low"}], None)
                data = tm._read(task_id)
                self.assertEqual(data["state"], "blocked")
                self.assertEqual(data["verification"]["changed_files"], ["partial.py"])
            finally:
                tm._root = old_root

    def test_codex_invocation_keeps_approval_and_read_only_flags(self):
        from unittest.mock import patch
        events = [
            {"type": "item.completed", "item": {"type": "command_execution",
                                                "command": "cat plugin.yaml", "exit_code": 0}},
            {"type": "item.completed", "item": {"type": "agent_message",
                                                "text": "evidence\n" * 1200 + "password=" + "private" * 1000
                                                        + "\ncli-router 0.4.0\nVERDICT: PASS"}},
        ]
        captured = {}
        class Process:
            returncode = 0
            def communicate(self, **kwargs):
                captured["prompt"] = kwargs["input"]
                return "\n".join(json.dumps(event) for event in events), ""
        def start(command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs["env"]
            return Process()
        with patch.dict("os.environ", {"AI_MEMORY_AUTH_TOKEN": "mock-token"}), \
             patch.object(tm.ai_memory_bridge, "_parent_env", return_value={"AI_MEMORY_AUTH_TOKEN": "mock-token"}), \
             patch.object(tm.subprocess, "Popen", side_effect=start), \
             patch.object(tm.ai_memory_bridge, "verify_runtime"), \
             patch.object(tm.ai_memory_bridge, "find_workstream", return_value="a" * 36), \
             patch.object(tm.ai_memory_bridge, "verify_attempt", return_value=4):
            result = tm._run_candidate({"cli": "codex", "model": "gpt-6-luna", "effort": "low"},
                                       "inspect plugin.yaml", Path.cwd(), "analysis", "d" * 32,
                                       [{"type": "file_exists", "path": "plugin.yaml"}])
        command = captured["command"]
        self.assertEqual(result["outcome"], "needs_verification")
        self.assertEqual(len(result["summary"]), 4000)
        self.assertTrue(result["summary"].endswith("cli-router 0.4.0\nVERDICT: PASS"))
        self.assertEqual(tm._parse_verdict(result["summary"]), "PASS")
        self.assertNotIn("private", result["summary"])
        self.assertIn("password=[redacted]", result["summary"])
        self.assertEqual(command[0], tm.shutil.which("systemd-run"))
        self.assertEqual(command[command.index("--") + 1], tm.shutil.which("ai-memory"))
        self.assertIn("--require-server", command)
        self.assertIn("--no-autowire", command)
        self.assertIn("--fresh", command)
        self.assertIn("Never substitute prior-session context", captured["prompt"])
        self.assertEqual(command[command.index("codex") + 1:command.index("codex") + 4],
                         ["--ask-for-approval", "on-request", "exec"])
        self.assertIn("--sandbox", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertNotIn("--yolo", command)
        self.assertNotIn("OPENROUTER_API_KEY", captured["env"])

    def test_codex_precise_analysis_rejects_zero_tool_drift(self):
        from unittest.mock import patch
        raw = (ROOT / "tests/fixtures/codex/stale_answer.jsonl").read_text(encoding="utf-8")
        class Process:
            returncode = 0
            def communicate(self, **kwargs):
                return raw, ""
        with patch.dict("os.environ", {"AI_MEMORY_AUTH_TOKEN": "mock-token"}), \
             patch.object(tm.ai_memory_bridge, "_parent_env", return_value={"AI_MEMORY_AUTH_TOKEN": "mock-token"}), \
             patch.object(tm.subprocess, "Popen", return_value=Process()), \
             patch.object(tm.ai_memory_bridge, "verify_runtime"), \
             patch.object(tm.ai_memory_bridge, "find_workstream", return_value="a" * 36), \
             patch.object(tm.ai_memory_bridge, "verify_attempt", return_value=4):
            result = tm._run_candidate({"cli": "codex", "model": "gpt-6-luna", "effort": "low"},
                                       "inspect sites/1.1.3", Path.cwd(), "analysis", "d" * 32,
                                       [{"type": "file_exists", "path": "sites/1.1.3"}])
        self.assertEqual(result["outcome"], "failed_verification")
        self.assertIn("no repository tool call", result["reason"])
        self.assertIn("stale answer", result["summary"])

    def test_opencode_worker_pins_both_models_and_provider(self):
        import tempfile
        from unittest.mock import patch
        class Process:
            returncode = 0
            def communicate(self, **kwargs): return "free result", ""
        captured = {}
        def start(command, **kwargs):
            env = kwargs["env"]
            config_arg = next(value.split("=", 1)[1] for value in command
                              if value.startswith("OPENCODE_CONFIG="))
            config = json.loads(Path(config_arg).read_text())
            captured.update(command=command, env=env, config=config, config_path=config_arg)
            return Process()
        # This argument-construction test must not depend on any pre-existing .opencode config.
        with tempfile.TemporaryDirectory() as temp, patch.object(tm.subprocess, "Popen", side_effect=start), \
             patch.object(tm, "_opencode_environment_safe", return_value=True), \
             patch.dict("os.environ", {"AI_MEMORY_AUTH_TOKEN": "mock-token", "OPENAI_API_KEY": "synthetic-paid-key", "ANTHROPIC_API_KEY": "synthetic-paid-key"}), \
             patch.object(tm.ai_memory_bridge, "_parent_env", return_value={"AI_MEMORY_AUTH_TOKEN": "mock-token"}), \
             patch.object(tm.ai_memory_bridge, "verify_runtime"), \
             patch.object(tm.ai_memory_bridge, "find_workstream", return_value="a" * 36), \
             patch.object(tm.ai_memory_bridge, "verify_attempt", return_value=4):
            result = tm._run_candidate({"cli": "opencode", "model": "x/free:free", "effort": None, "context": 65536},
                                       "change", Path(temp), "change", "e" * 32, [], "synthetic-openrouter-key")
        config = captured["config"]
        self.assertEqual(result["outcome"], "needs_verification")
        self.assertEqual(config["enabled_providers"], ["openrouter"])
        self.assertEqual(config["model"], "openrouter/x/free:free")
        self.assertEqual(config["small_model"], config["model"])
        self.assertEqual(config["plugin"], [])
        self.assertIn("--pure", captured["command"])
        self.assertNotIn("--variant", captured["command"])
        self.assertEqual(captured["env"]["OPENROUTER_API_KEY"], "synthetic-openrouter-key")
        self.assertNotIn("OPENAI_API_KEY", captured["env"])
        self.assertNotIn("ANTHROPIC_API_KEY", captured["env"])
        self.assertFalse(Path(captured["config_path"]).exists())

    def test_opencode_review_aliases_snapshot_to_memory_repository(self):
        import tempfile
        from unittest.mock import patch
        class Process:
            returncode = 0
            def communicate(self, **kwargs): return "VERDICT: PASS", ""
        captured = {}
        def start(command, **kwargs):
            launcher = command[command.index("--executable") + 1]
            captured.update(command=command, script=Path(launcher).read_text(), launcher=launcher)
            return Process()
        with tempfile.TemporaryDirectory() as temp, patch.object(tm.subprocess, "Popen", side_effect=start), \
             patch.object(tm, "_opencode_environment_safe", return_value=True), \
             patch.object(tm, "_installed_cli", return_value="/usr/bin/opencode"), \
             patch.object(tm.shutil, "which", side_effect=lambda name, **_: f"/usr/bin/{name}"), \
             patch.dict("os.environ", {"AI_MEMORY_AUTH_TOKEN": "mock-token"}), \
             patch.object(tm.ai_memory_bridge, "_parent_env", return_value={"AI_MEMORY_AUTH_TOKEN": "mock-token"}), \
             patch.object(tm.ai_memory_bridge, "verify_runtime"), \
             patch.object(tm.ai_memory_bridge, "find_workstream", return_value="a" * 36), \
             patch.object(tm.ai_memory_bridge, "verify_attempt", return_value=4):
            snapshot, repo = Path(temp) / "snapshot", Path(temp) / "repo"
            snapshot.mkdir()
            repo.mkdir()
            result = tm._run_candidate({"cli": "opencode", "model": "x/free:free", "effort": None, "context": 65536},
                                       "verify", snapshot, "change", "e" * 32, [], "synthetic-openrouter-key",
                                       role="verify", memory_repo=repo, new_workstream=False)
        command, script = captured["command"], captured["script"]
        home = next(value.split("=", 1)[1] for value in command if value.startswith("HOME="))
        self.assertEqual(result["outcome"], "needs_verification")
        self.assertEqual(command[command.index("--dir") + 1], str(repo))
        self.assertIn("--fresh", command)
        self.assertIn(f"--bind {snapshot} {repo}", script)
        self.assertIn(f"--chdir {repo}", script)
        self.assertIn(f"--bind {home} {home}", script)
        self.assertTrue(captured["launcher"].startswith(home + "/"))
        self.assertTrue(captured["launcher"].endswith("/opencode"))
        self.assertFalse(Path(home).exists())

    def test_opencode_analysis_refuses_without_inheritable_repo_lock(self):
        task_id = "f" * 32
        tm._LOCK_FDS[task_id] = 1
        try:
            import tempfile
            with tempfile.TemporaryDirectory() as temp, \
                 patch.object(tm, "_opencode_environment_safe", return_value=True):
                result = tm._run_candidate({"cli": "opencode", "model": "x/free:free", "effort": None, "context": 65536},
                                           "inspect", Path(temp), "analysis", task_id, [], "synthetic-openrouter-key")
            self.assertEqual(result["outcome"], "unavailable")
            self.assertIn("process ownership", result["reason"])
        finally:
            tm._LOCK_FDS.pop(task_id, None)

    def test_timeout_terminates_worker_process_group_and_stops(self):
        import subprocess
        import signal
        from unittest.mock import patch
        class Process:
            pid = 424242
            returncode = None
            def communicate(self, **kwargs):
                self.timeouts = getattr(self, "timeouts", []) + [kwargs["timeout"]]
                raise subprocess.TimeoutExpired("codex", kwargs["timeout"])
            def wait(self, **kwargs): return -signal.SIGTERM
        process = Process()
        with patch.dict("os.environ", {"AI_MEMORY_AUTH_TOKEN": "mock-token"}), \
             patch.object(tm.ai_memory_bridge, "_parent_env", return_value={"AI_MEMORY_AUTH_TOKEN": "mock-token"}), \
             patch.object(tm.subprocess, "Popen", return_value=process), \
             patch.object(tm.os, "killpg") as killpg, patch.object(tm, "MAX_TASK_SECONDS", 2), \
             patch.object(tm.ai_memory_bridge, "verify_runtime"), \
             patch.object(tm.ai_memory_bridge, "find_workstream", return_value="a" * 36), \
             patch.object(tm.ai_memory_bridge, "verify_attempt", return_value=1):
            result = tm._run_candidate({"cli": "codex", "model": "gpt-6-luna", "effort": "low"},
                                       "inspect", Path.cwd(), "analysis", "1" * 32, [])
        self.assertEqual(result["outcome"], "timeout")
        self.assertEqual(killpg.call_count, 3)
        self.assertEqual(killpg.call_args_list[0].args, (process.pid, signal.SIGINT))
        self.assertEqual(killpg.call_args_list[1].args, (process.pid, signal.SIGTERM))
        self.assertEqual(killpg.call_args_list[2].args, (process.pid, signal.SIGKILL))
        self.assertEqual(process.timeouts, [2, 5])


    def test_preexisting_dirty_files_do_not_fail_the_workers_verification(self):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            (repo / "app.css").write_text("a {}\n")
            subprocess.run(["git", "-C", str(repo), "add", "app.css"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)
            (repo / "scratch.py").write_text("def broken(:\n")  # left behind before the task
            baseline = tm._path_digests(repo, tm._git_changed_paths(repo))
            (repo / "app.css").write_text("a { color: red; }\n")  # the worker's change
            checks = tm._independent_checks(repo, "", "change", baseline)
            self.assertEqual(checks["changed_files"], ["app.css"])
            self.assertNotIn("python_compile", checks["checks"])
            (repo / "scratch.py").write_text("def still_broken(:\n")  # now the worker touched it
            checks = tm._independent_checks(repo, "", "change", baseline)
            self.assertFalse(checks["checks"]["python_compile"]["ok"])

    def test_invalid_criteria_error_lists_the_supported_types(self):
        result = tm.dispatch({"action": "submit", "repository": "/nonexistent", "task": "x", "mode": "change",
                              "acceptance_criteria": [{"type": "css_valid"}]})
        self.assertFalse(result["ok"])
        for kind in ("file_exists", "file_changed", "python_compiles", "git_diff_clean"):
            self.assertIn(kind, result["error"])

    def test_kimi_routes_offer_k31_and_k28_not_k3(self):
        quota = {"fresh": True, "cache": {"providers": {
            "kimi": {"windows": [{"used_percent": 20}]},
        }}}
        installed = {"kimi": {
            "kimi-code/k3.1-pro": ["low", "high", "max"],
            "kimi-code/k3": ["low", "high", "max"],
            "kimi-code/kimi-for-coding": ["low", "high", "max"],
        }}
        routes = tm._subscriptions(quota, installed)
        self.assertEqual([route["model"] for route in routes],
                         ["kimi-code/k3.1-pro", "kimi-code/kimi-for-coding"])
        self.assertEqual([route["effort"] for route in routes], [None, None])
        self.assertIn("K3.1 Pro", tm.ROUTE_INSTRUCTIONS)
        self.assertIn("K2.8", tm.ROUTE_INSTRUCTIONS)

    def test_kimi_31_profile_prefers_updated_akita_to_openrouter(self):
        rows = [
            {"id": "acme/reasoner", "benchmarks": {"artificial_analysis": {"intelligence_index": 58}},
             "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "moonshotai/kimi-k3.1-pro",
             "benchmarks": {"artificial_analysis": {"intelligence_index": 55.1, "coding_index": None}},
             "pricing": {"prompt": "0.000002", "completion": "0.00001"}},
        ]
        index = tm.route_benchmark.openrouter_index(rows)
        candidate = {"cli": "kimi", "model": "kimi-code/k3.1-pro", "effort": None}
        profile = tm._profiled(candidate, index)["profile"]
        self.assertEqual(profile["composite_quality"], 92.1)
        self.assertEqual(profile["akita_v4"]["score"], 92.11)
        self.assertEqual(profile["composite_basis"], ["akita_v4"])
        self.assertEqual(profile["openrouter"]["usd_per_mtok"], 8.0)
        self.assertEqual(profile["akita_v4"]["run_cost_usd"], 12.67)
        self.assertEqual(profile["akita_v4"]["wall_minutes"], 110)

    def test_claude_routes_are_included_in_subscription_rotation_with_unknown_quota(self):
        quota = {"fresh": True, "cache": {"providers": {
            "anthropic": {"windows": [{"used_percent": 20}]},
        }}}
        installed = {"claude": {model: ["low"] for model in tm.CLAUDE_MODELS}}
        routes = tm._subscriptions(quota, installed)
        self.assertEqual(len(routes), 3)
        self.assertEqual([r["cli"] for r in routes], ["claude", "claude", "claude"])
        self.assertEqual([r["model"] for r in routes], ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"])
        for r in routes:
            self.assertEqual(r["quota_reason"], tm.CLAUDE_QUOTA_UNKNOWN)
        self.assertNotIn("Claude routes", tm.ROUTE_INSTRUCTIONS)

    def test_haiku_profile_uses_intelligence_only_while_coding_is_pending(self):
        rows = [
            {"id": "acme/reasoner", "benchmarks": {"artificial_analysis": {"intelligence_index": 58}},
             "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "anthropic/claude-haiku-5.5",
             "benchmarks": {"artificial_analysis": {"intelligence_index": 43.4, "coding_index": None}},
             "pricing": {"prompt": "0.0000001", "completion": "0.0000005"}},
        ]
        index = tm.route_benchmark.openrouter_index(rows)
        candidate = {"cli": "claude", "model": "claude-haiku-5-5", "effort": "low"}
        profile = tm._profiled(candidate, index)["profile"]
        self.assertEqual(profile["composite_quality"], 74.8)
        self.assertEqual(profile["composite_basis"], ["aa_intelligence_only"])
        self.assertEqual(profile["openrouter"]["usd_per_mtok"], 0.4)
        self.assertIn("No Akita v4 result", profile["note"])
        self.assertIn("no complete engineering index", profile["note"])

    def test_worker_thread_inherits_the_request_context(self):
        # The gateway selects the profile via a context variable; the worker's Jev handoff reads
        # profile config, so the thread must run in a copy of the submitting request's context.
        import contextvars
        import subprocess
        import tempfile
        import threading
        from unittest.mock import patch
        profile = contextvars.ContextVar("profile_probe", default="root")
        seen, done = {}, threading.Event()

        def fake_worker(*_args):
            seen["profile"] = profile.get()
            done.set()

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            old_root = tm._root; tm._root = lambda: root / "home"
            token = profile.set("code")
            try:
                with patch.object(tm.ai_memory_bridge, "verify_runtime"), \
                     patch.object(tm, "_quota", return_value={"fresh": True, "cache": {}}), \
                     patch.object(tm, "_subscriptions", return_value=[{"cli": "claude", "model": "m", "effort": "medium"}]), \
                     patch.object(tm, "_installed_models", return_value={}), \
                     patch.object(tm, "_worker_capable", side_effect=lambda c: c), \
                     patch.object(tm, "_jev_choose", side_effect=lambda task, c, **_: c), \
                     patch.object(tm, "_free_models", return_value=[]), \
                     patch.object(tm, "_worker", side_effect=fake_worker):
                    result = tm.dispatch({"action": "submit", "repository": str(repo), "task": "t", "mode": "change",
                                          "acceptance_criteria": [{"type": "git_diff_clean"}]})
                    self.assertTrue(done.wait(5), result)
            finally:
                profile.reset(token)
                tm._root = old_root
        self.assertEqual(seen.get("profile"), "code")

    def test_jev_sees_benchmark_profiles_but_launches_the_exact_route(self):
        rows = [{"id": "anthropic/claude-opus-5", "benchmarks": {"artificial_analysis": {"coding_index": 78}},
                 "pricing": {"prompt": "0.000005", "completion": "0.000025"}},
                {"id": "google/gemini-3.8-flash", "benchmarks": {"artificial_analysis": {"coding_index": 76.3}},
                 "pricing": {"prompt": "0.000000825", "completion": "0.000004125"}},
                {"id": "google/gemini-3.8-flash:batch"},
                {"id": "anthropic/claude-opus-5.5", "benchmarks": {"artificial_analysis": {"intelligence_index": 57.6}},
                 "pricing": {"prompt": "0.000004", "completion": "0.00002"}},
                {"id": "acme/coder:free", "benchmarks": {"artificial_analysis": {"coding_index": 39}},
                 "pricing": {"prompt": "0", "completion": "0"}}]
        index = tm.route_benchmark.openrouter_index(rows)
        claude = {"cli": "claude", "provider": "anthropic", "model": "claude-opus-5-5", "effort": "medium"}
        agy = {"cli": "agy", "provider": "antigravity", "model": "gemini-3.8-flash-high", "effort": "high"}
        kimi = {"cli": "kimi", "provider": "kimi", "model": "kimi-code/kimi-for-coding", "effort": None}
        free = {"cli": "opencode", "provider": "openrouter", "model": "acme/coder:free", "effort": None}
        unknown = {"cli": "codex", "provider": "openai-codex", "model": "gpt-9", "effort": "low"}
        opus = tm._profiled(claude, index)["profile"]
        # Exact Akita data is primary, not averaged with OpenRouter.
        self.assertEqual(opus["composite_quality"], 100.0)
        self.assertEqual(opus["composite_basis"], ["akita_v4"])
        self.assertEqual(opus["openrouter"]["coding_rank"], 3)
        flash = tm._profiled(agy, index)["profile"]
        # The article publishes an exact 90%-coverage breadth index for this route.
        self.assertEqual(flash["composite_quality"], 67.9)
        self.assertEqual(flash["openrouter"]["coding_rank"], 2)
        self.assertEqual(flash["openrouter"]["usd_per_mtok"], 3.3)
        self.assertIn("K2.8", tm._profiled(kimi, index)["profile"]["note"])
        self.assertEqual(tm._profiled(free, index)["profile"]["composite_quality"], 50.0)
        self.assertNotIn("profile", tm._profiled(unknown, index))
        self.assertNotIn("profile", claude)  # the launch record is never mutated

    def test_benchmark_index_fails_open_to_its_snapshot(self):
        import tempfile
        bench = tm.route_benchmark
        rows = [{"id": "x/y", "benchmarks": {"artificial_analysis": {"coding_index": 50}},
                 "pricing": {"prompt": "0", "completion": "0"}}]
        with tempfile.TemporaryDirectory() as temp:
            snap = Path(temp) / "route-benchmark.json"
            bench._CACHE.update(at=0.0, index=None)
            self.assertIn("x/y", bench.load(lambda: rows, snap, now=1000)["models"])
            self.assertIn("x/y", bench.load(lambda: 1 / 0, snap, now=1000 + 10)["models"])  # cached
            bench._CACHE.update(at=0.0, index=None)
            self.assertIn("x/y", bench.load(lambda: 1 / 0, snap, now=10 ** 9)["models"])  # snapshot
            bench._CACHE.update(at=0.0, index=None)
            snap.unlink()
            self.assertEqual(bench.load(lambda: 1 / 0, snap, now=10 ** 9)["models"], {})
            bench._CACHE.update(at=0.0, index=None)
        self.assertIn("cheapest and fastest", tm.ROUTE_INSTRUCTIONS)

    def test_agy_attempt_is_confirmed_from_the_hook_captured_session_page(self):
        bridge = tm.ai_memory_bridge
        tid = "a" * 32
        marker = f"Code task {tid} attempt 1"
        sid = "704822dd-e1f0-4aaa-a044-83e946c32b2c"
        stderr = f"ai-memory: finalized the antigravity-cli session {sid}\n"
        page = (f"# {marker}. You are a CLI…\n\n## Session metadata\n\n- x\n\n## Prompts\n\n"
                f"1. {marker}. You are a CLI coding worker…\n\n## Raw observations\n\n- stop\n")
        calls = []

        def fake_command(args, cwd, timeout=20):
            calls.append(args)
            if args[0] == "workstream-search":
                return "[]"
            if args[0] == "read-page":
                return page
            raise AssertionError(args)

        with patch.object(bridge, "read_context", side_effect=AssertionError("strict reader used")), \
             patch.object(bridge, "_command", side_effect=fake_command), \
             patch.object(bridge.time, "sleep"):
            self.assertEqual(bridge.verify_attempt(Path("/r"), "ws", tid, 1, cli="agy", run_stderr=stderr), 0)
            self.assertIn(["read-page", "--path", f"sessions/{sid}.md"], calls)
            # Another attempt's marker, another CLI, or an ambiguous run is never accepted.
            with self.assertRaises(bridge.AiMemoryError):
                bridge.verify_attempt(Path("/r"), "ws", tid, 2, cli="agy", run_stderr=stderr)
            with self.assertRaises(bridge.AiMemoryError):
                bridge.verify_attempt(Path("/r"), "ws", tid, 1, cli="claude", run_stderr=stderr)
            with self.assertRaises(bridge.AiMemoryError):
                bridge.verify_attempt(Path("/r"), "ws", tid, 1, cli="agy",
                                      run_stderr=stderr + stderr.replace(sid[:8], "0" * 8))

    def test_worker_prompt_leads_with_the_attempt_marker(self):
        # The hook-captured session page keeps only a prompt's first ~80 characters.
        source = Path(tm.__file__).read_text()
        self.assertIn('prompt = (f"Code task {task_id} attempt {attempt_index}. "', source)

    def test_worker_sandbox_blanks_secrets_outside_the_repository(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for rel in (".config/ai-memory/client.env", ".local/share/ai-memory/auth-token",
                        ".hermes/.env", ".hermes/profiles/code/.env", "honcho/.env", "site/.env"):
                (home / rel).parent.mkdir(parents=True, exist_ok=True)
                (home / rel).write_text("SECRET=1")
            (home / ".local/share/ai-memory/hook-spool").mkdir()
            repo = home / "site"
            masked = tm._secret_masks("claude", repo, home, home)
            blanked = {masked[i + 2] for i, a in enumerate(masked) if a == "--ro-bind"}
            self.assertIn(str(home / ".config/ai-memory/client.env"), blanked)
            self.assertIn(str(home / "honcho/.env"), blanked)
            self.assertIn(str(home / ".hermes/profiles/code/.env"), blanked)
            self.assertNotIn(str(home / "site/.env"), blanked)  # the repository's own .env stays
            self.assertIn(str(home / ".local/share/ai-memory/hook-spool"), masked)
            agy = tm._secret_masks("agy", repo, home, home)
            self.assertNotIn(str(home / ".local/share/ai-memory/auth-token"), agy)  # its hooks need it
            self.assertNotIn("--tmpfs", agy)
            self.assertIn(str(home / ".hermes/.env"), agy)

if __name__ == "__main__":
    unittest.main()

class QuotaRecheckTests(unittest.TestCase):
    def test_refresh_and_reject_route_that_entered_reserve(self):
        candidate = {"cli": "codex", "provider": "openai-codex", "model": "gpt-6-luna", "effort": "medium"}
        fresh = {"fresh": True, "cache": {"providers": {"openai-codex": {
            "windows": [{"used_percent": 95}]}}}}
        with patch.object(tm, "_quota", side_effect=[{"fresh": False}, fresh]) as quota, \
             patch.object(tm, "_installed_models", return_value={"codex": {"gpt-6-luna": ["medium"]}}):
            self.assertFalse(tm._candidate_has_quota(candidate))
            self.assertEqual([c.kwargs for c in quota.call_args_list], [{"refresh": False}, {"refresh": True}])
