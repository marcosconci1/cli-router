import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ai_memory_bridge as bridge
import diagnostic_journal
import doctor
import hermes_adapter
import problems
import task_manager as tm

SECRET = "sk-abcdefghijklmnop1234"


# --- registry / docs drift guard -------------------------------------------------------------

def test_registry_is_complete_and_documented():
    docs = (ROOT / "docs" / "troubleshooting.md").read_text(encoding="utf-8")
    headings = set(re.findall(r"(?m)^## (.+?)\s*$", docs))
    assert set(problems.CODES) >= {
        "AIM_CLI_MISSING", "AIM_AUTH_MISSING", "AIM_VERSION_UNSUPPORTED", "AIM_SERVER_UNAVAILABLE",
        "AIM_CHECK_FAILED", "AIM_ATTEMPT_UNCONFIRMED", "JEV_PLUGIN_NOT_LOADED", "JEV_NOT_CONFIGURED",
        "JEV_MODEL_UNSUPPORTED", "JEV_REQUEST_FAILED"}
    for code, spec in problems.CODES.items():
        assert code == code.upper()
        for key in ("component", "title", "effect", "fix"):
            assert spec.get(key), (code, key)
        assert spec["component"] in {"ai-memory", "jev"}
        assert code in headings, code
        section = docs.split(f"## {code}", 1)[1].split("\n## ", 1)[0]
        for part in ("Symptom", "Cause", "Fix", "How to verify"):
            assert part in section, (code, part)
    assert headings == set(problems.CODES)


def test_every_code_in_source_exists_in_registry():
    found = set()
    for name in ("task_manager.py", "ai_memory_bridge.py", "doctor.py", "problems.py"):
        found |= set(re.findall(r"\b((?:AIM|JEV)_[A-Z_]+)\b", (ROOT / name).read_text(encoding="utf-8")))
    assert found - set(problems.CODES) == set()


# --- helpers ---------------------------------------------------------------------------------

def _cfg(modular=None, plugin=None, context=True):
    """Patch config readers: cli-router.yaml mapping and Hermes plugin settings."""
    class Ctx:
        def get_config(self, key, default=None):
            return (plugin or {}).get(key, default)
    stack = [patch.object(tm, "_load_modular_config", return_value=modular or {}),
             patch.object(hermes_adapter, "_CONTEXT", Ctx() if context else None)]
    return stack


def _ai_memory_missing():
    raise bridge.AiMemoryError("missing", code="AIM_CLI_MISSING")


class _Cfg:
    def __init__(self, **kw):
        self.stack = _cfg(**kw)

    def __enter__(self):
        for p in self.stack:
            p.start()

    def __exit__(self, *a):
        for p in reversed(self.stack):
            p.stop()


@pytest.mark.parametrize("modular,plugin,installed,expected", [
    ({}, {}, False, False),
    ({}, {}, True, True),
    ({"ai-memory": "enabled"}, {}, False, True),
    ({"ai-memory": "auto"}, {}, False, True),
    ({"ai-memory": "disabled"}, {}, True, False),
    ({}, {"cli-router": {"ai-memory": "auto"}}, False, True),
    ({}, {"cli-router": {"ai-memory": "disabled"}}, True, False),
])
def test_is_chosen_ai_memory(modular, plugin, installed, expected):
    with _Cfg(modular=modular, plugin=plugin), patch.object(
            bridge, "_cli", return_value="/bin/ai-memory" if installed else None,
            side_effect=None if installed else _ai_memory_missing):
        assert problems.is_chosen("ai-memory") is expected


@pytest.mark.parametrize("modular,plugin,expected", [
    ({}, {}, False),
    ({"jev": True}, {}, True),
    ({"jev": False}, {"connection": {"backend": "openrouter", "model": "m"}}, False),
    ({}, {"connection": {"backend": "openrouter", "model": "m"}}, True),
    ({}, {"connection": {}}, True),
])
def test_is_chosen_jev(modular, plugin, expected):
    with _Cfg(modular=modular, plugin=plugin):
        assert problems.is_chosen("jev") is expected


def test_realistic_plugin_context_reads_each_plugin_key():
    class Context:
        def get_config(self, key, default=None):
            return {"ai-memory": "disabled", "jev": False,
                    "connection": {"backend": "openrouter", "model": "typesafe/jev-1.14"}}.get(key, default)
    with patch.object(tm, "_load_modular_config", return_value={}), \
         patch.object(hermes_adapter, "_CONTEXT", Context()), \
         patch.object(bridge, "_cli", side_effect=AssertionError("disabled choice probes CLI")):
        assert problems.is_chosen("ai-memory") is False
        assert problems.is_chosen("jev") is False
    with patch.object(tm, "_load_modular_config", return_value={"ai-memory": "auto", "jev": True}), \
         patch.object(hermes_adapter, "_CONTEXT", Context()):
        assert problems.is_chosen("ai-memory") is True
        assert problems.is_chosen("jev") is True


# --- classification, make, add ---------------------------------------------------------------

@pytest.mark.parametrize("message,code", [
    ("ai-memory CLI is unavailable", "AIM_CLI_MISSING"),
    ("ai-memory authentication is unavailable in the task runner", "AIM_AUTH_MISSING"),
    ("ai-memory 3.1.0 or newer is required for managed workstreams", "AIM_VERSION_UNSUPPORTED"),
    ("ai-memory server health/authentication is unavailable", "AIM_SERVER_UNAVAILABLE"),
    ("ai-memory version check failed (OSError)", "AIM_CHECK_FAILED"),
    ("ai-memory request failed (OSError)", "AIM_CHECK_FAILED"),
])
def test_bridge_errors_carry_codes(message, code):
    with patch.object(bridge.shutil, "which", return_value=None if "CLI" in message else "/x/ai-memory"), \
         patch.dict(bridge.os.environ, {} if "authentication is unavailable in" in message else
                    {"AI_MEMORY_AUTH_TOKEN": "t"}, clear=True), \
         patch.object(bridge.subprocess, "run") as run:
        import subprocess
        if "required" in message:
            run.return_value = subprocess.CompletedProcess([], 0, "ai-memory 1.0.0", "")
        elif "server" in message:
            run.side_effect = [subprocess.CompletedProcess([], 0, "ai-memory 3.3.3", ""),
                               subprocess.CompletedProcess([], 0, "nothing", "")]
        elif "version check" in message:
            run.side_effect = OSError("boom")
        else:
            run.side_effect = OSError("boom")
        with pytest.raises(bridge.AiMemoryError) as info:
            if "request failed" in message:
                bridge._command(["status"], Path("."))
            else:
                bridge.verify_runtime(Path("."))
    assert str(info.value) == message or message.split(" (")[0] in str(info.value)
    assert problems.classify_ai_memory_error(info.value) == code


def test_classify_unknown_errors():
    assert problems.classify_ai_memory_error(RuntimeError("x")) == "AIM_CHECK_FAILED"
    assert bridge.AiMemoryError("x").code is None


def test_make_redacts_and_bounds():
    entry = problems.make("AIM_CHECK_FAILED", f"token={SECRET} api_key=abc at /home/someone/repo/file " + "x" * 900)
    assert entry["code"] == "AIM_CHECK_FAILED" and entry["chosen"] is True
    assert entry["component"] == "ai-memory"
    assert entry["docs"] == "docs/troubleshooting.md#aim_check_failed"
    assert len(entry["detail"]) <= 300
    assert SECRET not in entry["detail"] and "/home/someone" not in entry["detail"]
    assert entry["title"] == problems.CODES["AIM_CHECK_FAILED"]["title"]


def test_add_dedupes_by_code_in_order():
    data = {}
    assert problems.add(data, problems.make("JEV_REQUEST_FAILED", "a")) is True
    assert problems.add(data, problems.make("AIM_CLI_MISSING", "b")) is True
    assert problems.add(data, problems.make("JEV_REQUEST_FAILED", "c")) is False
    assert [e["code"] for e in data["degraded"]] == ["JEV_REQUEST_FAILED", "AIM_CLI_MISSING"]


# --- status view -----------------------------------------------------------------------------

def test_status_view_omits_empty_degraded():
    base = {"task_id": "a" * 32, "state": "completed", "owner_hash": "x"}
    assert "degraded" not in tm._status_view({**base, "degraded": []})
    entry = problems.make("AIM_CLI_MISSING", "d")
    assert tm._status_view({**base, "degraded": [entry]})["degraded"] == [entry]


def test_wait_view_includes_degraded_and_omits_empty_or_missing():
    token = "owner-token"
    owner_hash = hashlib.sha256(token.encode()).hexdigest()
    with tempfile.TemporaryDirectory() as temp, patch.object(tm, "_root", return_value=Path(temp)):
        entry = problems.make("AIM_CLI_MISSING", "details")
        for task_id, extra in (("a" * 32, {"degraded": [entry]}),
                               ("b" * 32, {"degraded": []}),
                               ("c" * 32, {})):
            tm._write(task_id, {"task_id": task_id, "state": "completed", "owner_hash": owner_hash,
                                **extra})

        result = tm.dispatch({"action": "wait", "task_id": "a" * 32, "owner_token": token,
                              "timeout_seconds": 0})
        assert result["degraded"][0]["code"] == "AIM_CLI_MISSING"
        for task_id in ("b" * 32, "c" * 32):
            result = tm.dispatch({"action": "wait", "task_id": task_id, "owner_token": token,
                                  "timeout_seconds": 0})
            assert "degraded" not in result


# --- end to end through submit ---------------------------------------------------------------

def _submit(configured_mode, verify_error=None, modular=None, plugin=None, context=True):
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    repo = root / "repo"
    repo.mkdir()
    state = root / "state"
    candidate = {"cli": "codex", "provider": "openai-codex", "model": "gpt-6-luna", "effort": "low"}
    quota = {"fresh": True, "cache": {"providers": {}}}
    with _Cfg(modular=modular, plugin=plugin, context=context), \
         patch.object(bridge, "_cli", side_effect=_ai_memory_missing), \
         patch.object(tm.ai_memory_bridge, "verify_runtime", side_effect=verify_error), \
         patch.object(tm, "_root", return_value=state), \
         patch.object(tm, "_repo", return_value=repo), \
         patch.object(tm, "_repo_key", return_value="repo-key"), \
         patch.object(tm, "_lock_path", return_value=root / "repo.lock"), \
         patch.object(tm, "_recover_orphaned_tasks"), \
         patch.object(tm, "_ai_memory_mode", return_value=configured_mode), \
         patch.object(tm, "_quota", return_value=quota), \
         patch.object(tm, "_subscriptions", return_value=[candidate]), \
         patch.object(tm, "_installed_models", return_value={}), \
         patch.object(tm, "_candidate_has_quota", return_value=True), \
         patch.object(tm, "_jev_choose", side_effect=lambda t, c, **k: c), \
         patch.object(tm.threading.Thread, "start", return_value=None):
        result = tm.dispatch({"action": "submit", "repository": str(repo), "task": "inspect",
                              "mode": "analysis", "cli": "codex",
                              "acceptance_criteria": [{"type": "file_exists", "path": "README.md"}]})
        data = tm._read(result["task_id"])
        view = tm.dispatch({"action": "status", "task_id": result["task_id"],
                            "owner_token": result["owner_token"]})
        journal = (state / "runtime" / "cli-tasks" / "events.log.jsonl")
        events = [json.loads(l) for l in journal.read_text().splitlines()] if journal.exists() else []
        report = diagnostic_journal.build_report(state, [])[1]
    temporary.cleanup()
    return result, data, view, events, report


WARNING = "ai-memory unavailable; running standalone (single attempt, no handoff memory)"


def test_chosen_auto_server_down_is_reported_everywhere():
    err = bridge.AiMemoryError(
        f"ai-memory server health/authentication is unavailable token={SECRET} at /home/private/repo")
    err.code = "AIM_SERVER_UNAVAILABLE"
    result, data, view, events, report = _submit(
        "auto", err, modular={"ai-memory": "auto"})
    for source in (result, data, view):
        assert source["degraded"][0]["code"] == "AIM_SERVER_UNAVAILABLE"
    assert "unavailable" in data["degraded"][0]["detail"]
    assert WARNING in data["route_warnings"]
    degraded = [e for e in events if e["kind"] == "degraded"]
    assert len(degraded) == 1
    assert degraded[0]["code"] == "AIM_SERVER_UNAVAILABLE"
    assert degraded[0]["component"] == "ai-memory" and degraded[0]["task_id"] == result["task_id"]
    assert "AIM_SERVER_UNAVAILABLE" in report
    assert SECRET not in json.dumps(data["degraded"])
    assert SECRET not in json.dumps(degraded)
    assert SECRET not in report


def test_not_chosen_changes_nothing():
    for modular in ({}, {"ai-memory": "disabled"}):
        result, data, view, events, _ = _submit(
            "auto", RuntimeError("down"), modular=modular)
        assert "degraded" not in result and "degraded" not in view
        assert not data.get("degraded")
        assert WARNING in data["route_warnings"]
        assert not [e for e in events if e["kind"] == "degraded"]


def test_healthy_chosen_path_has_no_degraded_key():
    result, data, view, events, _ = _submit("auto", None, modular={"ai-memory": "auto"})
    assert "degraded" not in result and "degraded" not in view
    assert data["ai_memory"]["mode"] == "managed"
    assert not [e for e in events if e["kind"] == "degraded"]


def test_enabled_mode_hard_stop_explains_itself():
    err = bridge.AiMemoryError("ai-memory CLI is unavailable")
    err.code = "AIM_CLI_MISSING"
    result, data, view, events, _ = _submit("enabled", err, modular={"ai-memory": "enabled"})
    assert result["state"] == "blocked"
    assert result["degraded"][0]["code"] == "AIM_CLI_MISSING"


# --- Jev -------------------------------------------------------------------------------------

def _jev(data, plugin, modular=None, service=None):
    cand = [{"cli": "codex", "model": "m", "effort": "low"}]
    with _Cfg(modular=modular or {}, plugin=plugin), \
         patch.object(tm, "_root", return_value=Path(tempfile.mkdtemp())), \
         patch.object(tm, "_write"), \
         patch.object(tm, "_filter_candidates_by_model_toggle", side_effect=lambda c: c), \
         patch.object(tm, "_benchmark_index", return_value={}), \
         patch.object(hermes_adapter, "jev_service", side_effect=service if isinstance(service, Exception) else None,
                      return_value=service if not isinstance(service, Exception) else None):
        return tm._jev_choose("task", cand, task_id="a" * 32, in_memory_data=data)


GOOD = {"backend": "openrouter", "model": "typesafe/jev-1.14"}


def test_jev_chosen_plugin_not_loaded():
    data = {"task_id": "a" * 32}
    _jev(data, {"connection": GOOD}, service=RuntimeError("Jev plugin is not loaded"))
    assert [e["code"] for e in data["degraded"]] == ["JEV_PLUGIN_NOT_LOADED"]
    assert any("Jev work ranking failed" in w for w in data["route_warnings"])


def test_jev_other_codes():
    data = {"task_id": "a" * 32}
    _jev(data, {"connection": {"backend": "x", "model": "y"}}, service=MagicMock())
    assert data["degraded"][0]["code"] == "JEV_MODEL_UNSUPPORTED"
    data = {"task_id": "a" * 32}
    _jev(data, {}, modular={"jev": True}, service=MagicMock())
    assert data["degraded"][0]["code"] == "JEV_NOT_CONFIGURED"
    data = {"task_id": "a" * 32}
    svc = MagicMock()
    svc.run.return_value = {"ok": False}
    _jev(data, {"connection": GOOD}, service=svc)
    assert data["degraded"][0]["code"] == "JEV_REQUEST_FAILED"
    assert svc.run.call_count == 2


def test_jev_failure_detail_is_sanitized_and_two_calls_only():
    class Service:
        def __init__(self): self.calls = 0
        def run(self, request):
            self.calls += 1
            return {"ok": False, "error": "missing_credential", "status_code": 401,
                    "request": {"prompt": "private prompt"}, "headers": {"Authorization": "Bearer secret"},
                    "body": "sk-abcdefghijklmnop1234 /home/private/file"}
    service = Service()
    with pytest.raises(RuntimeError) as caught:
        tm._jev_run(service, {"prompt": "private prompt"}, "failed")
    assert service.calls == 2
    data = {"task_id": "a" * 32}
    with patch.object(problems, "is_chosen", return_value=True):
        tm._record_jev_degraded(data["task_id"], caught.value, data)
    detail = data["degraded"][0]["detail"]
    assert "missing_credential" in detail and "401" in detail
    assert "private prompt" not in detail and "Bearer secret" not in detail
    assert "sk-abcdefghijklmnop1234" not in detail and "/home/private" not in detail


def test_handoff_context_exception_records_unconfirmed_before_block():
    # Focused on the failure signal while retaining the blocked transition contract.
    data = {"task_id": "f" * 32, "ai_memory": {"mode": "managed", "workstream_id": "e" * 36}}
    with _Cfg(modular={"ai-memory": "enabled"}), patch.object(tm, "_journal"):
        tm._record_attempt_unconfirmed(data, "ledger marker missing at /home/user/.env token=abc")
    assert data["degraded"][0]["code"] == "AIM_ATTEMPT_UNCONFIRMED"
    assert "ledger marker missing" in data["degraded"][0]["detail"]
    assert "/home/user" not in data["degraded"][0]["detail"]


def test_jev_not_chosen_adds_nothing():
    data = {"task_id": "a" * 32}
    _jev(data, {}, service=RuntimeError("Jev plugin is not loaded"))
    assert "degraded" not in data


# --- memory_blocked ---------------------------------------------------------------------------

def test_memory_blocked_adds_unconfirmed_with_last_stderr_line():
    data = {"task_id": "a" * 32}
    stderr = f"first line\n\nbwrap: Can't mount /home/me/x token={SECRET}\n\n"
    with _Cfg(modular={"ai-memory": "auto"}), patch.object(tm, "_root", return_value=Path(tempfile.mkdtemp())):
        tm._record_attempt_unconfirmed(data, stderr)
        tm._record_attempt_unconfirmed(data, "other")
    assert [e["code"] for e in data["degraded"]] == ["AIM_ATTEMPT_UNCONFIRMED"]
    detail = data["degraded"][0]["detail"]
    assert "Can't mount" in detail and SECRET not in detail and "/home/me" not in detail
    assert "first line" not in detail


def _run_external_review_memory_blocked(modular):
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    repo = root / "repo"
    repo.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "-C", str(repo), "add", "seed.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "seed"], check=True)
    task_id = "d" * 32
    owner_token = "review-owner"
    owner_hash = hashlib.sha256(owner_token.encode()).hexdigest()
    implementer = {"cli": "codex", "model": "implementation", "effort": "low"}
    reviewer = {"cli": "kimi", "model": "reviewer", "effort": None}
    stderr = "first diagnostic\n\nbwrap: Can't mkdir parents for /home/example/mod/.env: Read-only file system\n"
    outcomes = iter((
        {"outcome": "needs_verification", "summary": "changes ready",
         "ai_memory": {"workstream_id": "e" * 36, "latest_sequence": 1}},
        {"outcome": "memory_blocked", "reason": "ledger unavailable", "stderr": stderr},
    ))
    state = root / "state"
    with _Cfg(modular=modular), patch.object(bridge, "_cli", side_effect=_ai_memory_missing), \
         patch.object(tm, "_root", return_value=state), \
         patch.object(tm.ai_memory_bridge, "verify_runtime"), \
         patch.object(tm, "_candidate_has_quota", return_value=True), \
         patch.object(tm, "_jev_choose", return_value=[reviewer]), \
         patch.object(tm, "_run_candidate", side_effect=lambda *a, **k: next(outcomes)), \
         patch.object(tm, "_independent_checks", return_value={
             "checks": {"git_diff_clean": {"ok": True}},
             "changed_files": ["seed.txt"], "head_unchanged": False,
         }):
        tm._write(task_id, {"task_id": task_id, "state": "queued", "repository": str(repo),
                            "mode": "change", "owner_hash": owner_hash, "events": [],
                            "ai_memory": {"mode": "managed"}})
        tm._worker(task_id, repo, "review this", "change",
                   [{"type": "file_changed", "path": "seed.txt"}], [implementer, reviewer], None)
        data = tm._read(task_id)
        view = tm.dispatch({"action": "status", "task_id": task_id, "owner_token": owner_token})
        journal = state / "runtime" / "cli-tasks" / "events.log.jsonl"
        events = [json.loads(line) for line in journal.read_text().splitlines()] if journal.exists() else []
    temporary.cleanup()
    return data, view, [event for event in events if event["kind"] == "degraded"]


def test_external_review_memory_blocked_is_recorded_end_to_end():
    data, view, events = _run_external_review_memory_blocked({"ai-memory": "auto"})
    assert data["verification"]["external"]["reviews"][0]["worker_outcome"] == "memory_blocked"
    for source in (data, view):
        entry = next(item for item in source["degraded"] if item["code"] == "AIM_ATTEMPT_UNCONFIRMED")
        assert "bwrap: Can't mkdir parents for [path]: Read-only file system" == entry["detail"]
        assert "/home/example" not in entry["detail"]
        assert SECRET not in json.dumps(entry)
    assert len(events) == 1
    assert events[0]["code"] == "AIM_ATTEMPT_UNCONFIRMED"

    data, view, events = _run_external_review_memory_blocked({})
    assert "degraded" not in data and "degraded" not in view
    assert events == []


# --- doctor ----------------------------------------------------------------------------------

def test_doctor_ai_memory_chosen_failing_and_not_chosen():
    err = bridge.AiMemoryError("ai-memory CLI is unavailable")
    err.code = "AIM_CLI_MISSING"
    with patch.object(doctor, "_modular_config", return_value={"ai-memory": "auto"}), \
         patch.object(tm, "_load_modular_config", return_value={"ai-memory": "auto"}), \
         patch.object(doctor.ai_memory_bridge, "verify_runtime", side_effect=err):
        res = doctor._check_ai_memory()
    assert res["status"] == "warn" and res["hint"].startswith("chosen but")
    assert res["code"] == "AIM_CLI_MISSING" and res["chosen"] is True
    with patch.object(doctor, "_modular_config", return_value={}), \
         patch.object(tm, "_load_modular_config", return_value={}), \
         patch.object(bridge, "_cli", side_effect=_ai_memory_missing), \
         patch.object(hermes_adapter, "_CONTEXT", None), \
         patch.object(doctor.ai_memory_bridge, "verify_runtime", side_effect=err):
        res = doctor._check_ai_memory()
    assert res["status"] == "skip" and "not chosen" in res["hint"] and res["chosen"] is False


def test_doctor_jev_fresh_install_skips_and_chosen_warns():
    class Ctx:
        def get_config(self, key, default=None):
            return default
    with patch.object(doctor, "_modular_config", return_value={}), patch.object(tm, "_load_modular_config", return_value={}), patch.object(hermes_adapter, "_CONTEXT", Ctx()):
        res = doctor._check_jev_connection()
    assert res["status"] == "skip" and "not chosen" in res["hint"] and res["chosen"] is False

    class Ctx2:
        def get_config(self, key, default=None):
            return GOOD if key == "connection" else default
    with patch.object(doctor, "_modular_config", return_value={}), patch.object(tm, "_load_modular_config", return_value={}), patch.object(hermes_adapter, "_CONTEXT", Ctx2()), \
         patch.object(hermes_adapter, "_jev_service_cls", side_effect=RuntimeError("not loaded")):
        res = doctor._check_jev_connection()
    assert res["status"] == "warn" and res["hint"].startswith("chosen but")
    assert res["code"] == "JEV_PLUGIN_NOT_LOADED" and res["chosen"] is True
    with patch.object(doctor, "_modular_config", return_value={}), patch.object(tm, "_load_modular_config", return_value={}), patch.object(hermes_adapter, "_CONTEXT", Ctx2()), \
         patch.object(hermes_adapter, "_jev_service_cls", return_value=MagicMock()):
        assert doctor._check_jev_connection()["status"] == "ok"


def test_doctor_uses_direct_plugin_choices_with_modular_precedence():
    class Context:
        def get_config(self, key, default=None):
            return {"ai-memory": "disabled", "jev": False,
                    "connection": GOOD}.get(key, default)
    context = Context()
    with patch.object(hermes_adapter, "_CONTEXT", context), \
         patch.object(doctor, "_modular_config", return_value={}), \
         patch.object(tm, "_load_modular_config", return_value={}), \
         patch.object(doctor.ai_memory_bridge, "verify_runtime", side_effect=AssertionError("disabled check ran")):
        assert doctor._check_ai_memory()["status"] == "skip"
        assert doctor._check_jev_connection()["status"] == "skip"
    with patch.object(hermes_adapter, "_CONTEXT", context), \
         patch.object(doctor, "_modular_config", return_value={"ai-memory": "enabled", "jev": True}), \
         patch.object(tm, "_load_modular_config", return_value={"ai-memory": "enabled", "jev": True}), \
         patch.object(doctor.ai_memory_bridge, "verify_runtime"), \
         patch.object(hermes_adapter, "_jev_service_cls", return_value=MagicMock()):
        assert doctor._check_ai_memory()["chosen"] is True
        assert doctor._check_jev_connection()["chosen"] is True
