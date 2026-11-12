"""AGY stays routable but never participates in managed ai-memory attempts."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import task_manager as tm


AGY = {"cli": "agy", "provider": "antigravity", "model": "gemini-3.8-flash-low", "effort": "low"}
CODEX = {"cli": "codex", "provider": "openai-codex", "model": "gpt-6-luna", "effort": "low"}
KIMI = {"cli": "kimi", "provider": "kimi", "model": "kimi-for-coding", "effort": None}
TASK_ID = "a" * 32


def _worker(tmp_path, routes, outcome):
    repo = tmp_path / "repo"
    repo.mkdir()
    data = {"task_id": TASK_ID, "state": "queued", "ai_memory": {"mode": "managed"}}
    with patch.object(tm, "_root", return_value=tmp_path / "state"), \
         patch.object(tm, "_lock_path", return_value=tmp_path / "repo.lock"), \
         patch.object(tm, "_candidate_has_quota", return_value=True), \
         patch.object(tm, "_git_readonly", return_value=SimpleNamespace(stdout="head")), \
         patch.object(tm, "_git_changed_paths", return_value=[]), \
         patch.object(tm, "_path_digests", return_value={}), \
         patch.object(tm, "_independent_checks", return_value={"changed_files": []}), \
         patch.object(tm, "_run_candidate", return_value=outcome) as run, \
         patch.object(tm.ai_memory_bridge, "verify_runtime", side_effect=AssertionError("runtime check called")), \
         patch.object(tm.ai_memory_bridge, "read_context", side_effect=AssertionError("memory context read")), \
         patch.object(tm, "_jev_handoff", side_effect=AssertionError("handoff called")):
        tm._write(TASK_ID, data)
        tm._worker(TASK_ID, repo, "inspect and fix", "change", [], routes, None)
        result = tm._read(TASK_ID)
    return result, run


def test_agy_remains_worker_capable_with_managed_memory_enabled():
    agy = dict(AGY)
    with patch.object(tm.shutil, "which", return_value="/usr/bin/bwrap"):
        assert agy in tm._worker_capable([agy])
    assert tm._candidate_memory_mode(agy, "managed") == "standalone"
    assert tm._candidate_memory_mode(CODEX, "managed") == "managed"


def test_agy_sandbox_filters_memory_mcp_and_hooks_but_preserves_unrelated_config(tmp_path):
    home = tmp_path / "home"
    repo = home / "repo"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    repo.mkdir(parents=True)
    global_config = home / ".gemini" / "config" / "mcp_config.json"
    global_config.parent.mkdir(parents=True)
    global_config.write_text(
        '{"mcpServers":{"ai-memory":{"command":"ai-memory","env":{"TOKEN":"synthetic"}},'
        '"docs":{"command":"docs-mcp"}}}'
    )
    global_hooks = home / ".gemini" / "config" / "hooks.json"
    global_hooks.write_text(
        '{"memory-capture":{"SessionStart":[{"command":"ai-memory hook"}]},'
        '"format":{"PostToolUse":[{"command":"prettier"}]}}'
    )
    workspace_config = repo / ".agents" / "mcp_config.json"
    workspace_config.parent.mkdir()
    workspace_config.write_text(
        '{"mcpServers":{"memory":{"serverUrl":"http://127.0.0.1:19876/mcp"},'
        '"tests":{"command":"pytest-mcp"}}}'
    )
    workspace_hooks = repo / ".agents" / "hooks.json"
    workspace_hooks.write_text(
        '{"ai-memory-sync":{"SessionStart":[{"command":"ai-memory hook"}]},'
        '"lint":{"PostToolUse":[{"command":"ruff"}]}}'
    )
    plugin_config = home / ".gemini" / "config" / "plugins" / "sample" / "mcp_config.json"
    plugin_config.parent.mkdir(parents=True)
    plugin_config.write_text(
        '{"mcpServers":{"ai-memory-plugin":{"command":"ai-memory"},'
        '"browser":{"command":"browser-mcp"}}}'
    )
    plugin_hooks = plugin_config.with_name("hooks.json")
    plugin_hooks.write_text(
        '{"capture-memory":{"SessionStart":[{"command":"ai-memory hook"}]},'
        '"format":{"PostToolUse":[{"command":"prettier"}]}}'
    )
    oauth_tokens = home / ".gemini" / "antigravity-cli" / "mcp_oauth_tokens.json"
    oauth_tokens.parent.mkdir(parents=True)
    oauth_tokens.write_text(
        '{"ai-memory":{"accessToken":"synthetic-oauth"},'
        '"docs":{"accessToken":"synthetic-unrelated"}}'
    )

    visible_repo = tmp_path / "visible-repo"
    with patch.dict("os.environ", {"AI_MEMORY_SERVER_URL": "http://127.0.0.1:19876/mcp"}):
        bindings = tm._agy_memory_config_binds(repo, scratch, home, visible_repo)
    overlays = {Path(bindings[i + 1]): Path(bindings[i + 2])
                for i, arg in enumerate(bindings) if arg == "--bind"}
    visible_workspace_config = visible_repo / ".agents" / "mcp_config.json"
    visible_workspace_hooks = visible_repo / ".agents" / "hooks.json"
    assert set(overlays.values()) == {global_config, global_hooks, visible_workspace_config,
                                      visible_workspace_hooks, plugin_config, plugin_hooks, oauth_tokens}
    filtered = {destination: __import__("json").loads(source.read_text())
                for source, destination in overlays.items()}
    assert list(filtered[global_config]["mcpServers"]) == ["docs"]
    assert list(filtered[global_hooks]) == ["format"]
    assert list(filtered[visible_workspace_config]["mcpServers"]) == ["tests"]
    assert list(filtered[visible_workspace_hooks]) == ["lint"]
    assert list(filtered[plugin_config]["mcpServers"]) == ["browser"]
    assert list(filtered[plugin_hooks]) == ["format"]
    assert list(filtered[oauth_tokens]) == ["docs"]
    overlay_text = " ".join(source.read_text() for source in overlays)
    assert '"TOKEN": "synthetic"' not in overlay_text
    assert "synthetic-oauth" not in overlay_text
    assert "synthetic-unrelated" in overlay_text
    assert "ai-memory" in global_config.read_text()  # host configuration is untouched


def test_agy_filters_generic_loopback_memory_server_without_environment_url(tmp_path):
    home = tmp_path / "home"
    repo = home / "repo"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    config = repo / ".agents" / "mcp_config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        '{"mcpServers":{"memory":{"serverUrl":"http://127.0.0.1:19876/mcp"},'
        '"memory-search":{"serverUrl":"https://search.example/mcp"}}}'
    )
    with patch.dict("os.environ", {}, clear=True):
        bindings = tm._agy_memory_config_binds(repo, scratch, home)
    filtered_path = Path(bindings[1])
    assert "127.0.0.1" not in filtered_path.read_text()
    assert "https://search.example/mcp" in filtered_path.read_text()


def test_agy_config_file_symlinks_fail_closed_without_touching_external_target(tmp_path):
    home = tmp_path / "home"
    repo = home / "repo"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    repo.mkdir(parents=True)

    external_file = tmp_path / "external" / "server-config.json"
    external_file.parent.mkdir()
    external_file.write_text(
        '{"mcpServers":{"ai-memory":{"command":"memory"},"docs":{"command":"docs"}}}'
    )
    config_link = home / ".gemini" / "config" / "mcp_config.json"
    config_link.parent.mkdir(parents=True)
    config_link.symlink_to(external_file)

    before = external_file.read_bytes()
    with pytest.raises(RuntimeError, match="symlink"):
        tm._agy_memory_config_binds(repo, scratch, home)
    assert "ai-memory" in external_file.read_text()
    assert external_file.read_bytes() == before
    assert not (scratch / "agy-memory-policy").exists()


def test_agy_config_directory_symlink_fails_closed_without_touching_external_target(tmp_path):
    home = tmp_path / "home"
    repo = home / "repo"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    repo.mkdir(parents=True)
    external_dir = tmp_path / "external-plugin"
    external_dir.mkdir()
    external_config = external_dir / "mcp_config.json"
    external_config.write_text('{"mcpServers":{"ai-memory":{"command":"memory"}}}')
    before = external_config.read_bytes()
    plugin_link = home / ".gemini" / "config" / "plugins" / "linked-plugin"
    plugin_link.parent.mkdir(parents=True)
    plugin_link.symlink_to(external_dir, target_is_directory=True)

    with pytest.raises(RuntimeError, match="symlink"):
        tm._agy_memory_config_binds(repo, scratch, home)
    assert external_config.read_bytes() == before
    assert not (scratch / "agy-memory-policy").exists()


def test_agy_broken_config_symlink_fails_closed_without_creating_target(tmp_path):
    home = tmp_path / "home"
    repo = home / "repo"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    repo.mkdir(parents=True)
    broken_target = tmp_path / "external" / "missing-config.json"
    link = home / ".gemini" / "config" / "mcp_config.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(broken_target)

    with pytest.raises(RuntimeError, match="symlink"):
        tm._agy_memory_config_binds(repo, scratch, home)
    assert not broken_target.exists()
    assert link.is_symlink()


def test_agy_nonregular_named_config_fails_closed_without_creating_overlay(tmp_path):
    home = tmp_path / "home"
    repo = home / "repo"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    repo.mkdir(parents=True)
    config_directory = home / ".gemini" / "config" / "mcp_config.json"
    config_directory.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="config file"):
        tm._agy_memory_config_binds(repo, scratch, home)
    assert config_directory.is_dir()
    assert not (scratch / "agy-memory-policy").exists()


@pytest.mark.parametrize("config_name", ["mcp_config.json", "hooks.json", "mcp_oauth_tokens.json"])
@pytest.mark.parametrize("read_failure", [False, True], ids=["malformed", "unreadable"])
def test_agy_invalid_or_unreadable_config_is_replaced_by_empty_overlay(tmp_path, monkeypatch,
                                                                       read_failure, config_name):
    home = tmp_path / "home"
    repo = home / "repo"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    repo.mkdir(parents=True)
    config = home / ".gemini" / ("antigravity-cli" if config_name == "mcp_oauth_tokens.json" else "config") / config_name
    config.parent.mkdir(parents=True)
    config.write_text("{ malformed" if not read_failure else '{"mcpServers":{}}')
    original_read_text = Path.read_text

    if read_failure:
        original_open = tm.os.open
        def unreadable(path, *args, **kwargs):
            if path == config.name:
                raise PermissionError("synthetic unreadable config")
            return original_open(path, *args, **kwargs)
        monkeypatch.setattr(tm.os, "open", unreadable)

    bindings = tm._agy_memory_config_binds(repo, scratch, home)
    overlay = Path(bindings[1])
    assert __import__("json").loads(overlay.read_text()) == {}
    assert original_read_text(config) != "{}"


@pytest.mark.parametrize("mode", ["analysis", "change"])
def test_agy_launcher_applies_memory_config_overlay(tmp_path, mode):
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = ["--ro-bind", "/synthetic/filtered", "/synthetic/original"]
    with patch.object(tm.shutil, "which", return_value="/usr/bin/bwrap"), \
         patch.object(tm, "_secret_masks", return_value=[]), \
         patch.object(tm, "_git_metadata_binds", return_value=[]), \
         patch.object(tm, "_agy_memory_config_binds", return_value=marker) as binds:
        scratch, launcher = tm._sandbox_launcher("agy", "/synthetic/agy", repo, mode, home=tmp_path)
    try:
        binds.assert_called_once()
        assert " ".join(marker) in Path(launcher).read_text()
    finally:
        scratch.cleanup()


def test_agy_success_is_recorded_without_claiming_memory(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    checks = {"checks": {"diff": {"ok": True}}, "changed_files": ["change.py"],
              "head_unchanged": False}
    with patch.object(tm, "_root", return_value=tmp_path / "state"), \
         patch.object(tm, "_lock_path", return_value=tmp_path / "repo.lock"), \
         patch.object(tm, "_candidate_has_quota", return_value=True), \
         patch.object(tm, "_git_readonly", return_value=SimpleNamespace(stdout="head")), \
         patch.object(tm, "_git_changed_paths", return_value=[]), \
         patch.object(tm, "_path_digests", return_value={}), \
         patch.object(tm, "_independent_checks", return_value=checks), \
         patch.object(tm, "_acceptance", return_value={"passed": True}), \
         patch.object(tm, "_independent_review_enabled", return_value=False), \
         patch.object(tm, "_run_candidate", return_value={"outcome": "needs_verification", "summary": "done"}) as run:
        tm._write(TASK_ID, {"task_id": TASK_ID, "state": "queued", "ai_memory": {"mode": "managed"}})
        tm._worker(TASK_ID, repo, "inspect and fix", "change", [], [AGY], None)
        result = tm._read(TASK_ID)
    assert result["state"] == "completed"
    assert run.call_args.kwargs["memory_mode"] == "standalone"
    attempt = result["attempts"][0]
    assert attempt["outcome"] == "verified"
    assert attempt["ai_memory"] is None
    assert attempt["memory_policy"] == "standalone_no_memory"
    assert "last_attempt_marker" not in result["ai_memory"]


@pytest.mark.parametrize("failure", ["quota_limited", "model_unavailable", "failed_verification"])
def test_agy_failure_stops_without_next_worker_or_handoff(tmp_path, failure):
    result, run = _worker(tmp_path, [AGY, CODEX], {"outcome": failure})
    assert result["state"] == "blocked"
    assert len(result["attempts"]) == 1
    assert result["attempts"][0]["cli"] == "agy"
    assert result["attempts"][0]["memory_policy"] == "standalone_no_memory"
    assert "AGY attempt failed" in result["message"]
    assert run.call_count == 1
    assert run.call_args.kwargs["memory_context"] == ""
    assert run.call_args.kwargs["memory_mode"] == "standalone"


def test_agy_review_failure_stops_review_rotation_but_codex_keeps_managed_control(tmp_path):
    routes = [AGY, CODEX]
    with patch.object(tm, "_candidate_has_quota", return_value=True), \
         patch.object(tm, "_jev_choose", side_effect=lambda _task, candidates, **_kwargs: candidates), \
         patch.object(tm, "_run_candidate", return_value={"outcome": "quota_limited"}) as run, \
         patch.object(tm.ai_memory_bridge, "read_context", side_effect=AssertionError("review handoff read")):
        review = tm._external_verification(TASK_ID, tmp_path, "review", [], routes, CODEX, None, 1,
                                           memory_mode="managed")
    assert run.call_count == 1
    assert run.call_args.args[0]["cli"] == "agy"
    assert run.call_args.kwargs["memory_mode"] == "managed"  # review function passes policy; launcher overrides AGY
    assert review["reviews"][0]["worker_outcome"] == "quota_limited"
    assert review["reviews"][0]["memory_policy"] == "standalone_no_memory"
    assert not review["complete"]

    with patch.object(tm, "_candidate_has_quota", return_value=True), \
         patch.object(tm, "_jev_choose", side_effect=lambda _task, candidates, **_kwargs: candidates), \
         patch.object(tm, "_run_candidate", side_effect=[
             {"outcome": "quota_limited", "ai_memory": {"workstream_id": "stream"}},
             {"outcome": "needs_verification", "summary": "VERDICT: PASS",
              "ai_memory": {"workstream_id": "stream"}},
         ]) as control_run, \
         patch.object(tm.ai_memory_bridge, "read_context", return_value=(f"Code task {TASK_ID} attempt 1", 1)):
        control = tm._external_verification(TASK_ID, tmp_path, "review", [], [CODEX, KIMI], CODEX, None, 1,
                                            memory_mode="managed")
    assert control_run.call_count == 2
    assert control_run.call_args_list[0].kwargs["memory_mode"] == "managed"
    assert control["reviews"][0]["worker_outcome"] == "quota_limited"


def test_agy_fail_verdict_after_non_agy_implementer_is_terminal(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    checks = {"checks": {"diff": {"ok": True}}, "changed_files": ["change.py"],
              "head_unchanged": False}
    routes = [CODEX, AGY, KIMI]
    with patch.object(tm, "_root", return_value=tmp_path / "state"), \
         patch.object(tm, "_lock_path", return_value=tmp_path / "repo.lock"), \
         patch.object(tm, "_candidate_has_quota", return_value=True), \
         patch.object(tm, "_git_readonly", return_value=SimpleNamespace(stdout="head")), \
         patch.object(tm, "_git_changed_paths", return_value=[]), \
         patch.object(tm, "_path_digests", return_value={}), \
         patch.object(tm, "_independent_checks", return_value=checks), \
         patch.object(tm, "_acceptance", return_value={"passed": True}), \
         patch.object(tm.ai_memory_bridge, "verify_runtime"), \
         patch.dict("os.environ", {"AI_MEMORY_AUTH_TOKEN": "synthetic-token"}), \
         patch.object(tm, "_jev_choose", side_effect=lambda _task, candidates, **_kwargs:
                      sorted(candidates, key=lambda candidate: candidate["cli"] != "agy")), \
         patch.object(tm, "_run_candidate", side_effect=[
             {"outcome": "needs_verification", "summary": "implemented",
              "ai_memory": {"workstream_id": "stream"}},
             {"outcome": "needs_verification", "summary": "VERDICT: FAIL"},
         ]) as run, \
         patch.object(tm.ai_memory_bridge, "read_context",
                      return_value=(f"Code task {TASK_ID} attempt 1", 1)), \
         patch.object(tm, "_jev_handoff", return_value=True):
        tm._write(TASK_ID, {"task_id": TASK_ID, "state": "queued", "ai_memory": {"mode": "managed"}})
        tm._worker(TASK_ID, repo, "inspect and fix", "change", [], routes, None)
        result = tm._read(TASK_ID)

    assert run.call_count == 2  # Codex implementer and one AGY reviewer; no repair worker.
    assert run.call_args_list[1].args[0]["cli"] == "agy"
    assert result["state"] == "blocked"
    assert len(result["attempts"]) == 1
    assert "AGY reviewer failed" in result["message"]
    assert result["verification"]["external"]["agy_review_failed"] is True


@pytest.mark.parametrize("swap_parent", [False, True])
def test_agy_read_rejects_symlink_swap_after_inspection(tmp_path, monkeypatch, swap_parent):
    home = tmp_path / "home"
    repo = home / "repo"
    repo.mkdir(parents=True)
    config = home / ".gemini" / "config" / "mcp_config.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"mcpServers":{"docs":{"command":"docs"}}}')
    external = tmp_path / "external"
    external.mkdir()
    target = external / config.name
    target.write_text('{"mcpServers":{"ai-memory":{"command":"synthetic"}}}')
    before = target.read_bytes()
    original_open = tm.os.open
    swapped = False

    def racing_open(path, *args, **kwargs):
        nonlocal swapped
        trigger = "config" if swap_parent else config.name
        if path == trigger and not swapped:
            swapped = True
            if swap_parent:
                config.parent.rename(config.parent.with_name("old-config"))
                config.parent.symlink_to(external, target_is_directory=True)
            else:
                config.unlink()
                config.symlink_to(target)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(tm.os, "open", racing_open)
    bindings = tm._agy_memory_config_binds(repo, tmp_path / "scratch", home)
    assert swapped
    assert Path(bindings[1]).read_text() == "{}"
    assert target.read_bytes() == before


def test_model_discovery_lists_agy_models():
    listing = "gemini-3.8-flash-low  Fast\ngemini-4-argon-high  Deep\nunrelated-model\n"
    with patch.object(tm.shutil, "which", side_effect=lambda name, *a, **k: "/usr/bin/agy" if name == "agy" else None), \
         patch.object(tm, "_installed_cli", return_value=None), \
         patch.object(tm.subprocess, "run", return_value=SimpleNamespace(stdout=listing, returncode=0)):
        models = tm._installed_models()
    assert models["agy"] == {"gemini-3.8-flash-low": ["low"], "gemini-4-argon-high": ["high"]}


def test_status_view_keeps_long_summaries():
    long_summary = "x" * 2500 + "\nVERDICT: PASS"
    view = tm._status_view({"task_id": TASK_ID, "state": "succeeded", "summary": long_summary,
                            "events": []}, include_history=True)
    assert view["summary"].endswith("VERDICT: PASS")
