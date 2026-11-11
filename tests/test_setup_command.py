import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import doctor
import setup_command


def _ck(name, status="ok", hint="h"):
    return {"name": name, "status": status, "hint": hint}


CHECKS = [_ck("config"), _ck("bubblewrap"), _ck("git"), _ck("systemd-run"),
          _ck("cli:claude"), _ck("cli:codex"), _ck("cli:agy", "warn"), _ck("cli:kimi", "warn"),
          _ck("ai-memory", "skip", "disabled"), _ck("plugin-wiring")]


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "myprofile"
        self.home.mkdir()
        (self.home / "config.yaml").write_text("plugins:\n  enabled:\n    - cli-router\n")
        env = patch.dict("os.environ", {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        import os
        os.environ.pop("CLI_ROUTER_CONFIG", None)
        p = patch.object(doctor, "run_checks", return_value=[dict(c) for c in CHECKS])
        p.start()
        self.addCleanup(p.stop)
        q = patch.object(doctor._task_manager_module(), "_config_path",
                         side_effect=lambda: next((x for x in [self.home / "cli-router.yaml"] if x.is_file()), None))
        q.start()
        self.addCleanup(q.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_it(self, check=False):
        lines = []
        code = setup_command.run_setup(self.home, check, lines.append)
        return code, lines

    def test_creates_config_and_output(self):
        code, lines = self.run_it()
        self.assertEqual(code, 0)
        self.assertTrue((self.home / "cli-router.yaml").is_file())
        self.assertIn("profile: myprofile", lines[0])
        self.assertTrue(lines[0].startswith("cli-router "))
        self.assertIn("Next:", lines)
        self.assertEqual(sum("plugin" in l for l in lines if l.startswith("[")), 1)
        self.assertIn("[ok]    CLIs found: claude, codex", lines)
        self.assertTrue(any("agy not installed (optional)" in l for l in lines))
        self.assertTrue(any(l.startswith("[new]") for l in lines))
        self.assertTrue(any("(defaults; edit to taste)" in l for l in lines))

    def test_existing_bytes_kept_and_idempotent(self):
        path = self.home / "cli-router.yaml"
        path.write_bytes(b"jev: false\n# mine\n")
        self.run_it()
        self.run_it()
        self.assertEqual(path.read_bytes(), b"jev: false\n# mine\n")
        self.run_it()
        _, lines = self.run_it()
        self.assertIn("[ok]    cli-router.yaml present (kept as is)", lines)

    def test_second_run_idempotent(self):
        self.run_it()
        first = (self.home / "cli-router.yaml").read_bytes()
        _, lines = self.run_it()
        self.assertEqual((self.home / "cli-router.yaml").read_bytes(), first)
        self.assertFalse(any(l.startswith("[new]") for l in lines))

    def test_check_creates_nothing(self):
        code, lines = self.run_it(check=True)
        self.assertEqual(code, 0)
        self.assertFalse((self.home / "cli-router.yaml").exists())
        self.assertIn("[skip]  cli-router.yaml not created (--check)", lines)

    def test_exit_code_on_fail(self):
        bad = [dict(c) for c in CHECKS]
        bad[1]["status"] = "fail"
        with patch.object(doctor, "run_checks", return_value=bad):
            code, _ = self.run_it(check=True)
        self.assertEqual(code, 1)

    def test_no_cli_fails(self):
        none = [dict(c, status="warn") if c["name"].startswith("cli:") else dict(c) for c in CHECKS]
        with patch.object(doctor, "run_checks", return_value=none):
            code, lines = self.run_it(check=True)
        self.assertEqual(code, 1)
        self.assertIn("[fail]  no provider CLI found", lines)

    def test_missing_quota_cache_is_skipped(self):
        checks = [dict(c) for c in CHECKS]
        checks.append(_ck("quota-cache", "warn", "quota cache not found at /unused/path"))
        with patch.object(doctor, "run_checks", return_value=checks):
            code, lines = self.run_it(check=True)
        self.assertEqual(code, 0)
        self.assertIn("[skip]  quota cache fills in after the first task", lines)
        self.assertFalse(any("quota-cache:" in line for line in lines))

    def test_stale_quota_cache_remains_warning(self):
        checks = [dict(c) for c in CHECKS]
        checks.append(_ck("quota-cache", "warn", "quota cache is stale"))
        with patch.object(doctor, "run_checks", return_value=checks):
            code, lines = self.run_it(check=True)
        self.assertEqual(code, 0)
        self.assertIn("[warn]  quota-cache: quota cache is stale", lines)

    def test_saved_platforms_listed(self):
        (self.home / "config.yaml").write_text(
            "plugins:\n  enabled:\n    - cli-router\nplatform_toolsets:\n  discord:\n    - cli_router\n  cli:\n    - x\n")
        _, lines = self.run_it(check=True)
        self.assertIn("[ok]    tool reachable on: discord", lines)

    def _load_plugin(self):
        import importlib.util, types
        name = "hermes_plugins_setup_probe.cli_router"
        parent = types.ModuleType("hermes_plugins_setup_probe")
        parent.__path__ = []
        spec = importlib.util.spec_from_file_location(
            name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
        module = importlib.util.module_from_spec(spec)
        sys.modules.update({"hermes_plugins_setup_probe": parent, name: module})
        self.addCleanup(lambda: [sys.modules.pop(k) for k in list(sys.modules)
                                 if k.startswith("hermes_plugins_setup_probe")])
        spec.loader.exec_module(module)
        return module

    def test_register(self):
        module = self._load_plugin()
        calls = []

        class Ctx:
            def register_skill(self, *a, **k):
                pass

            def register_cli_command(self, **kw):
                calls.append(kw)

        class Bare:
            def register_skill(self, *a, **k):
                pass
        import importlib
        adapter = importlib.import_module(module.__name__ + ".hermes_adapter")
        with patch.object(adapter, "register"):
            module.register(Ctx())
            module.register(Bare())
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "cli-router")
        self.assertTrue(callable(calls[0]["setup_fn"]) and callable(calls[0]["handler_fn"]))

    def test_parser(self):
        parser = argparse.ArgumentParser(prog="hermes cli-router")
        setup_command.setup_parser(parser)
        self.assertFalse(parser.parse_args(["setup"]).check)
        self.assertTrue(parser.parse_args(["setup", "--check"]).check)
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            parser.parse_args([])

    def test_handle_exits_with_code(self):
        with patch.object(setup_command, "run_setup", return_value=1):
            with self.assertRaises(SystemExit) as cm:
                setup_command.handle(argparse.Namespace(check=True))
        self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
