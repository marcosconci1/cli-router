import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import doctor
import hermes_adapter
import task_manager


class DoctorTests(unittest.TestCase):
    def test_run_checks_returns_list_of_dicts_with_required_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            checks = doctor.run_checks(temp)
            self.assertIsInstance(checks, list)
            for c in checks:
                self.assertIn("name", c)
                self.assertIn("status", c)
                self.assertIn("hint", c)
                self.assertIn(c["status"], {"ok", "warn", "fail", "skip"})

    def test_bubblewrap_check_pass(self):
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/bwrap"), \
             patch.object(doctor.subprocess, "run", return_value=MagicMock(returncode=0)):
            res = doctor._check_bubblewrap()
            self.assertEqual(res["name"], "bubblewrap")
            self.assertEqual(res["status"], "ok")

    def test_bubblewrap_check_restricted_namespace_is_fail(self):
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/bwrap"), \
             patch.object(doctor.subprocess, "run", return_value=MagicMock(returncode=1)):
            res = doctor._check_bubblewrap()
            self.assertEqual(res["name"], "bubblewrap")
            self.assertEqual(res["status"], "fail")
            self.assertIn("namespaces restricted", res["hint"])

    def test_bubblewrap_check_missing_is_fail(self):
        with patch.object(doctor.shutil, "which", return_value=None):
            res = doctor._check_bubblewrap()
            self.assertEqual(res["name"], "bubblewrap")
            self.assertEqual(res["status"], "fail")

    def test_git_and_systemd_run_checks(self):
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/git"):
            self.assertEqual(doctor._check_git()["status"], "ok")
        with patch.object(doctor.shutil, "which", return_value=None):
            self.assertEqual(doctor._check_git()["status"], "fail")

        with patch.object(doctor.shutil, "which", return_value="/usr/bin/systemd-run"):
            self.assertEqual(doctor._check_systemd_run()["status"], "ok")
        with patch.object(doctor.shutil, "which", return_value=None):
            self.assertEqual(doctor._check_systemd_run()["status"], "fail")

    def test_cli_checks_never_fail(self):
        for cli in ("claude", "codex", "agy", "kimi"):
            with patch.object(doctor.shutil, "which", return_value=None), \
                 patch.object(task_manager, "_installed_cli", return_value=None):
                res = doctor._check_cli(cli)
                self.assertIn(res["status"], {"ok", "warn"})
                self.assertNotEqual(res["status"], "fail")

    def test_ai_memory_requires_authenticated_server_status(self):
        cases = [
            (0, "ai-memory 3.3.3 (server)\nserver: http://localhost", "ok"),
            (0, "server_url=http://localhost", "warn"),
            (0, "server unreachable", "warn"),
            (0, "warning: (server) unavailable", "warn"),
            (0, "", "warn"),
            (1, "ai-memory 3.3.3 (server)", "warn"),
        ]
        for returncode, stdout, expected in cases:
            with self.subTest(stdout=stdout, returncode=returncode), \
                 patch.object(doctor.hermes_adapter, "has_context", return_value=True), \
                 patch.object(doctor.hermes_adapter, "get_config", return_value={"ai-memory": "enabled"}), \
                 patch.object(doctor.ai_memory_bridge, "_parent_env", return_value={"AI_MEMORY_AUTH_TOKEN": "synthetic"}), \
                 patch.object(doctor.shutil, "which", return_value="/usr/bin/ai-memory"), \
                 patch.object(doctor.subprocess, "run", side_effect=[
                     subprocess.CompletedProcess([], 0, "ai-memory 3.3.3", ""),
                     subprocess.CompletedProcess([], returncode, stdout, ""),
                 ]):
                self.assertEqual(doctor._check_ai_memory()["status"], expected)

    def test_ai_memory_missing_auth_timeout_and_failure_are_blockers(self):
        with patch.object(doctor.hermes_adapter, "has_context", return_value=True), \
             patch.object(doctor.hermes_adapter, "get_config", return_value={"ai-memory": "enabled"}), \
             patch.object(doctor.shutil, "which", return_value=None):
            self.assertEqual(doctor._check_ai_memory()["status"], "warn")
        with patch.object(doctor.shutil, "which", return_value="/usr/bin/ai-memory"), \
             patch.object(doctor.hermes_adapter, "has_context", return_value=True), \
             patch.object(doctor.hermes_adapter, "get_config", return_value={"ai-memory": "enabled"}), \
             patch.dict(os.environ, {}, clear=True):
            self.assertEqual(doctor._check_ai_memory()["status"], "warn")
        for error in (subprocess.TimeoutExpired("ai-memory", 10), OSError("private-secret")):
            with self.subTest(error=type(error).__name__), \
                 patch.object(doctor.hermes_adapter, "has_context", return_value=True), \
                 patch.object(doctor.hermes_adapter, "get_config", return_value={"ai-memory": "enabled"}), \
                 patch.object(doctor.shutil, "which", return_value="/usr/bin/ai-memory"), \
                 patch.object(doctor.ai_memory_bridge, "_parent_env", return_value={"AI_MEMORY_AUTH_TOKEN": "synthetic"}), \
                 patch.object(doctor.subprocess, "run", side_effect=error):
                result = doctor._check_ai_memory()
                self.assertEqual(result["status"], "warn")
                self.assertNotIn("private-secret", result["hint"])

    def test_bubblewrap_probe_errors_are_blockers(self):
        for error in (subprocess.TimeoutExpired("bwrap", 5), OSError("private-secret")):
            with self.subTest(error=type(error).__name__), \
                 patch.object(doctor.shutil, "which", return_value="/usr/bin/bwrap"), \
                 patch.object(doctor.subprocess, "run", side_effect=error):
                result = doctor._check_bubblewrap()
                self.assertEqual(result["status"], "fail")
                self.assertNotIn("private-secret", result["hint"])

    def test_jev_connection_check(self):
        # Standalone
        with patch.object(hermes_adapter, "has_context", return_value=False):
            res = doctor._check_jev_connection()
            self.assertEqual(res["status"], "skip")

        # An explicit Jev opt-in is chosen even without a Hermes context.
        with patch.object(doctor, "_modular_config", return_value={"jev": True}), \
             patch.object(hermes_adapter, "has_context", return_value=False):
            res = doctor._check_jev_connection()
            self.assertEqual(res["status"], "warn")
            self.assertTrue(res["chosen"])
            self.assertEqual(res["code"], "JEV_PLUGIN_NOT_LOADED")
            self.assertTrue(res["hint"].startswith("chosen but"))

        connection = {"backend": "openrouter", "model": "typesafe/jev-1.14"}

        class PluginContext:
            def get_config(self, key, default=None):
                return connection if key == "connection" else default

        ctx = PluginContext()

        # In Hermes, configured, but Jev service not loaded in-process
        with patch.object(hermes_adapter, "_CONTEXT", ctx), \
             patch.object(hermes_adapter, "_jev_service_cls", side_effect=RuntimeError("Jev plugin is not loaded")):
            res = doctor._check_jev_connection()
            self.assertEqual(res["status"], "warn")
            self.assertIn("not loaded", res["hint"])
            self.assertIn("deterministic fallback ranking", res["hint"])

        # In Hermes, configured, with Jev service loaded
        with patch.object(hermes_adapter, "_CONTEXT", ctx), \
             patch.object(hermes_adapter, "_jev_service_cls", return_value=MagicMock()):
            res = doctor._check_jev_connection()
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["hint"], "Jev ranking active for openrouter/typesafe/jev-1.14")

        # In Hermes, missing or malformed connection
        for config in (None, {}, {"backend": "openrouter"}, {"model": "typesafe/jev-1.14"}, "invalid"):
            with self.subTest(config=config):
                class ConfigContext:
                    def get_config(self, key, default=None):
                        return config if key == "connection" else default

                with patch.object(hermes_adapter, "_CONTEXT", ConfigContext()):
                    res = doctor._check_jev_connection()
                # An explicit connection value, even malformed or empty, is chosen.
                expected = "warn" if config is not None else "skip"
                self.assertEqual(res["status"], expected)
                self.assertIn("deterministic fallback ranking in use", res["hint"])

    def test_modular_config_controls_doctor_memory_and_jev_diagnostics(self):
        with patch.object(doctor, "_modular_config", return_value={"ai-memory": "disabled", "jev": False}), \
             patch.object(doctor.ai_memory_bridge, "verify_runtime",
                          side_effect=AssertionError("disabled ai-memory probed")), \
             patch.object(hermes_adapter, "_CONTEXT", MagicMock()):
            memory = doctor._check_ai_memory()
            jev = doctor._check_jev_connection()
        self.assertEqual(memory["status"], "skip")
        self.assertIn("disabled", memory["hint"])
        self.assertEqual(jev["status"], "skip")
        self.assertIn("deterministic ranking and handoff", jev["hint"])

        with patch.object(doctor, "_modular_config", side_effect=ValueError("bad config")):
            self.assertEqual(doctor._check_jev_connection()["status"], "fail")

    def test_plugin_wiring_check(self):
        fixtures = {
            "both present": (
                """plugins:
  enabled:
    - cli-router # installed plugin
  disabled: []
platform_toolsets:
  discord:
    - cli_router
""",
                "ok",
            ),
            "plugin missing": (
                """plugins:
  enabled:
    - another-plugin
platform_toolsets:
  discord:
    - cli_router
""",
                "fail",
            ),
            "toolset missing": (
                """plugins:
  enabled:
    - cli-router
platform_toolsets:
  discord:
    - other_toolset
""",
                "fail",
            ),
            "enabled only": ("plugins:\n  enabled:\n    - cli-router\n", "ok"),
            "empty platform_toolsets": (
                "plugins:\n  enabled:\n    - cli-router\nplatform_toolsets: {}\n", "ok"),
            "inline toolset list": (
                """plugins:
  enabled:
    - cli-router
platform_toolsets:
  cli: [hermes-cli, library, cli_router] # active tools
""",
                "ok",
            ),
        }
        for label, (config, expected) in fixtures.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                Path(temp, "config.yaml").write_text(config, encoding="utf-8")
                result = doctor._check_plugin_wiring(temp)
                self.assertEqual(result["status"], expected)
                if label == "toolset missing":
                    self.assertIn("TOOLSET name is cli_router (underscore)", result["hint"])
                    self.assertIn("missing toolset entry is silent", result["hint"])

        with tempfile.TemporaryDirectory() as temp:
            result = doctor._check_plugin_wiring(temp)
            self.assertEqual(result["status"], "fail")
            self.assertIn("active profile's config.yaml could not be located", result["hint"])

    def test_quota_cache_check(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            # Missing
            res = doctor._check_quota_cache(home)
            self.assertEqual(res["status"], "warn")

            # Fresh
            (home / "quota_cache.json").write_text('{"fetched_at": "2099-01-01T00:00:00Z", "providers": {}}')
            res = doctor._check_quota_cache(home)
            self.assertEqual(res["status"], "ok")

            # Stale (> 1h)
            (home / "quota_cache.json").write_text('{"fetched_at": "2020-01-01T00:00:00Z", "providers": {}}')
            res = doctor._check_quota_cache(home)
            self.assertEqual(res["status"], "warn")
            self.assertIn("older than 1h", res["hint"])

    def test_runtime_dir_check(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            res = doctor._check_runtime_dir(home)
            self.assertEqual(res["status"], "ok")
            self.assertTrue((home / "runtime" / "cli-tasks").is_dir())

    def test_main_cli_exit_code(self):
        with patch.object(doctor, "run_checks", return_value=[{"name": "test", "status": "ok", "hint": "ok"}]):
            self.assertEqual(doctor.main(), 0)

        with patch.object(doctor, "run_checks", return_value=[{"name": "test", "status": "fail", "hint": "failed"}]):
            self.assertEqual(doctor.main(), 1)

    def test_task_manager_dispatch_doctor_action(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(task_manager, "_root", return_value=Path(temp)):
                res = task_manager.dispatch({"action": "doctor"})
                self.assertTrue(res.get("ok"))
                self.assertIsInstance(res.get("checks"), list)

    def _config_check(self, text, hermes_home=False):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "profile"
            plugin = Path(temp) / "plugin"
            home.mkdir()
            plugin.mkdir()
            if text is not None:
                target = home if hermes_home else plugin
                (target / "cli-router.yaml").write_text(text, encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), \
                 patch.object(task_manager, "_root", return_value=home), \
                 patch.object(task_manager, "_plugin_dir", return_value=plugin):
                return doctor._check_config()

    def test_config_check_no_file(self):
        res = self._config_check(None)
        self.assertEqual((res["name"], res["status"]), ("config", "ok"))
        self.assertIn("defaults", res["hint"])

    def test_config_check_valid_prints_effective_values(self):
        res = self._config_check("cli-router:\n  providers:\n    codex:\n      enabled: false\n"
                                 "      excluded_models: [a, b]\n  jev: false\n  max-workers: 3\n")
        self.assertEqual(res["status"], "ok")
        for text in ("codex", "jev=False", "max-workers=3", "'codex': 2"):
            self.assertIn(text, res["hint"])
        self.assertIn("plugin/cli-router.yaml", res["hint"])
        self.assertIn("move it to the Hermes home", res["hint"])

    def test_config_check_reports_hermes_home_path(self):
        res = self._config_check("cli-router:\n  jev: false\n", hermes_home=True)
        self.assertEqual(res["status"], "ok")
        self.assertIn("profile/cli-router.yaml", res["hint"])
        self.assertNotIn("move it to the Hermes home", res["hint"])

    def test_config_check_invalid_fails(self):
        res = self._config_check("cli-router:\n  max-workers: 9\n")
        self.assertEqual(res["status"], "fail")
        self.assertIn("max-workers", res["hint"])

    def test_run_checks_includes_config(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertIn("config", [c["name"] for c in doctor.run_checks(temp)])


if __name__ == "__main__":
    unittest.main()
