import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import doctor
import problems
import setup_command

SECRET = "sk-abcdefghijklmnop1234"


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

    def run_it(self, check=False, **options):
        lines = []
        code = setup_command.run_setup(self.home, check, lines.append, **options)
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
        self.assertIn("`hermes cli-router setup --check` creates and changes no config; it only reports.", lines)

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

    def test_unflagged_nonterminal_records_no_choices(self):
        code, lines = self.run_it(terminal=False)
        config = (self.home / "cli-router.yaml").read_text()
        self.assertEqual(code, 0)
        self.assertRegex(config, r"(?m)^\s+#\s*jev: true$")
        self.assertRegex(config, r"(?m)^\s+#\s*ai-memory: auto$")
        self.assertIn("choices: none recorded (Jev and ai-memory not chosen)", "\n".join(lines))

    def test_selected_choices_written_and_recognized(self):
        self.run_it(terminal=False, jev="on", ai_memory="enabled")
        config = (self.home / "cli-router.yaml").read_text()
        self.assertRegex(config, r"(?m)^  jev: true$")
        self.assertRegex(config, r"(?m)^  ai-memory: enabled$")
        with patch.object(doctor._task_manager_module(), "_load_modular_config",
                          return_value={"jev": True, "ai-memory": "enabled"}):
            self.assertTrue(problems.is_chosen("jev"))
            with patch.object(problems, "_plugin_settings", return_value={}):
                self.assertTrue(problems.is_chosen("ai-memory"))

    def test_existing_config_conflict_warns_and_strict_fails_without_writing(self):
        path = self.home / "cli-router.yaml"
        original = b"cli-router:\n  jev: false\n# preserve\n"
        path.write_bytes(original)
        code, lines = self.run_it(terminal=False, jev="on", ai_memory="auto")
        self.assertEqual(code, 0)
        self.assertTrue(any("already exists; --jev on not applied" in line for line in lines))
        self.assertTrue(any("already exists; --ai-memory auto not applied" in line for line in lines))
        self.assertEqual(path.read_bytes(), original)
        code, _ = self.run_it(terminal=False, jev="on", strict=True)
        self.assertEqual(code, 1)
        self.assertEqual(path.read_bytes(), original)

    def test_actual_config_validation_is_retained_and_json_fails(self):
        path = self.home / "cli-router.yaml"
        original = b"cli-router:\n  jev: [broken\n"
        path.write_bytes(original)
        checks = [dict(c) for c in CHECKS]
        checks[0] = doctor._check_config()
        with patch.object(doctor, "run_checks", return_value=checks):
            out = []
            code = setup_command.run_setup(self.home, out=out.append, json_output=True, terminal=False)
        payload = json.loads(out[0])
        config_check = next(c for c in payload["checks"] if c["name"] == "config")
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(config_check["status"], "fail")
        self.assertEqual(path.read_bytes(), original)

    def test_quoted_comment_choices_are_parsed_without_conflict(self):
        path = self.home / "cli-router.yaml"
        path.write_text("cli-router:\n  jev: false # comment\n  ai-memory: 'auto'\n")
        _, lines = self.run_it(terminal=False, jev="off", ai_memory="auto")
        self.assertIn("choices recorded: ai-memory auto, Jev off", "\n".join(lines))
        self.assertFalse(any("not applied" in line for line in lines))

    def test_exclusive_creation_loser_keeps_and_reads_winning_file(self):
        target = self.home / "cli-router.yaml"
        winner = b"cli-router:\n  jev: false # other writer\n  ai-memory: 'auto'\n"
        original_open = Path.open

        def racing_open(path, mode="r", *args, **kwargs):
            if path == target and mode == "xb":
                with original_open(path, "wb") as stream:
                    stream.write(winner)
                raise FileExistsError(path)
            return original_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", racing_open):
            _, lines = self.run_it(terminal=False, jev="off", ai_memory="auto")
        self.assertEqual(target.read_bytes(), winner)
        self.assertIn("choices recorded: ai-memory auto, Jev off", "\n".join(lines))
        self.assertFalse(any("not applied" in line for line in lines))

    def test_check_with_choice_flag_is_usage_error(self):
        parser = argparse.ArgumentParser()
        setup_command.setup_parser(parser)
        with patch("sys.stderr"), self.assertRaises(SystemExit) as cm:
            parser.parse_args(["setup", "--check", "--jev", "on"])
        self.assertEqual(cm.exception.code, 2)

    def test_interactive_choices_and_enter_skip(self):
        answers = iter(["y", ""])
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=lambda _: next(answers)) as inp:
            _, lines = self.run_it()
        config = (self.home / "cli-router.yaml").read_text()
        self.assertIn("  jev: true", config)
        self.assertRegex(config, r"(?m)^\s+#\s*ai-memory: auto$")
        self.assertIn("choices recorded: Jev on", "\n".join(lines))
        self.assertEqual(inp.call_count, 2)

    def test_no_input_never_prompts(self):
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=AssertionError):
            self.run_it(no_input=True)

    def test_json_is_single_object_with_exit_code_and_no_home(self):
        out = []
        checks = [dict(c) for c in CHECKS]
        checks.append(_ck("quota-cache", "warn", f"token={SECRET} at {self.home}/cache"))
        with patch.object(doctor, "run_checks", return_value=checks):
            code = setup_command.run_setup(self.home, out=out.append, json_output=True, terminal=False)
        self.assertEqual(len(out), 1)
        doc = json.loads(out[0])
        self.assertEqual(doc["exit_code"], code)
        self.assertEqual(set(doc), {"version", "profile", "ok", "strict", "exit_code", "choices", "checks"})
        self.assertEqual(doc["choices"], {"ai-memory": "unset", "jev": "unset"})
        self.assertNotIn(str(self.home), out[0])
        self.assertNotIn(SECRET, out[0])
        self.assertTrue(all({"name", "status", "hint"} <= set(c) for c in doc["checks"]))

    def test_main_accepts_choice_flags_and_help_documents_exit_codes(self):
        stdout = io.StringIO()
        with patch.dict("os.environ", {"HERMES_HOME": str(self.home)}), \
             patch.object(doctor, "run_checks", return_value=[dict(c) for c in CHECKS]), \
             contextlib.redirect_stdout(stdout):
            code = setup_command.main(["--jev", "off", "--ai-memory", "disabled", "--no-input", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["exit_code"], code)
        config = (self.home / "cli-router.yaml").read_text()
        self.assertIn("  jev: false", config)
        self.assertIn("  ai-memory: disabled", config)
        parser = argparse.ArgumentParser()
        setup_command.setup_parser(parser)
        with patch("sys.stdout", new=io.StringIO()) as captured, \
             self.assertRaises(SystemExit):
            parser.parse_args(["setup", "--help"])
        self.assertIn("Exit codes", captured.getvalue())

    def test_chosen_failure_shows_fix_docs_and_json_details(self):
        checks = [dict(c) for c in CHECKS]
        checks[8] = {"name": "ai-memory", "status": "warn", "chosen": True,
                     "code": "AIM_CLI_MISSING", "hint": "chosen but unavailable"}
        with patch.object(doctor, "run_checks", return_value=checks):
            _, lines = self.run_it(terminal=False, ai_memory="enabled")
        joined = "\n".join(lines)
        self.assertIn("        fix: " + problems.CODES["AIM_CLI_MISSING"]["fix"], joined)
        self.assertIn("        docs: docs/troubleshooting.md#aim_cli_missing", joined)
        output = []
        with patch.object(doctor, "run_checks", return_value=checks):
            code = setup_command.run_setup(self.home, out=output.append, json_output=True, terminal=False)
        doc = json.loads(output[0])
        self.assertEqual(doc["exit_code"], code)
        check = next(c for c in doc["checks"] if c["name"] == "ai-memory")
        self.assertEqual(check["code"], "AIM_CLI_MISSING")
        self.assertTrue(check["chosen"])
        self.assertTrue(check["fix"] and check["docs"])

    def test_unchosen_failure_has_no_fix_lines(self):
        checks = [dict(c) for c in CHECKS]
        checks[8] = {"name": "ai-memory", "status": "skip", "chosen": False,
                     "code": "AIM_CLI_MISSING", "hint": "not chosen"}
        with patch.object(doctor, "run_checks", return_value=checks):
            _, lines = self.run_it(terminal=False)
        self.assertFalse(any(line.startswith("        fix:") or line.startswith("        docs:") for line in lines))


if __name__ == "__main__":
    unittest.main()
