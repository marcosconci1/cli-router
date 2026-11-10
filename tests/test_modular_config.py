"""Regression tests for the optional per-install cli-router YAML config."""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hermes_adapter
import task_manager as tm


def write_config(tmp_path, text):
    (tmp_path / "cli-router.yaml").write_text(text, encoding="utf-8")
    return patch.object(tm, "_plugin_dir", return_value=tmp_path)


def test_file_config_applies_provider_enable_and_exact_per_cli_exclusions(tmp_path):
    config = write_config(tmp_path, """cli-router:
  providers:
    codex:
      enabled: true
      excluded_models: [gpt-6-luna]
""")
    routes = [{"cli": "codex", "model": "gpt-6-luna"},
              {"cli": "codex", "model": "gpt-6.1-luna"},
              {"cli": "kimi", "model": "gpt-6-luna"}]
    with config, patch.object(hermes_adapter, "_CONTEXT", None):
        assert tm._provider_toggles()["codex"] is True
        assert tm._filter_candidates_by_toggle(routes) == routes[1:]


def test_file_config_overrides_legacy_settings_and_empty_exclusions_mean_none(tmp_path):
    config = write_config(tmp_path, """cli-router:
  providers:
    codex:
      enabled: true
      excluded_models: []
""")
    context = type("Context", (), {"get_config": lambda self, name, default=None:
                                   {"routing.providers": {"codex": False}}})()
    routes = [{"cli": "codex", "model": "m1"}]
    with config, patch.object(hermes_adapter, "_CONTEXT", context):
        assert tm._filter_candidates_by_toggle(routes) == routes


def test_null_or_blank_exclusion_lists_are_empty(tmp_path):
    config = write_config(tmp_path, """cli-router:
  providers:
    codex:
      excluded_models:
    kimi:
      excluded_models: null
""")
    with config:
        assert tm._excluded_models() == {"codex": set(), "kimi": set()}


def test_exclusion_blocks_matching_pin_and_keeps_same_model_for_other_cli(tmp_path):
    config = write_config(tmp_path, """cli-router:
  providers:
    codex:
      excluded_models: [shared-model]
""")
    routes = [{"cli": "codex", "model": "shared-model"},
              {"cli": "kimi", "model": "shared-model"}]
    with config, patch.object(hermes_adapter, "_CONTEXT", None):
        candidates = tm._filter_candidates_by_toggle(routes)
        pinned = [candidate for candidate in candidates
                  if candidate["cli"] == "codex" and candidate["model"] == "shared-model"]
        assert pinned == []
        assert candidates == [routes[1]]


def test_per_cli_exclusions_filter_independent_review_candidates(tmp_path):
    config = write_config(tmp_path, """cli-router:
  providers:
    codex:
      excluded_models: [blocked]
""")
    routes = [{"cli": "codex", "model": "blocked"},
              {"cli": "kimi", "model": "reviewer-one"},
              {"cli": "agy", "model": "reviewer-two"}]
    captured = []
    with config, patch.object(hermes_adapter, "_CONTEXT", None), \
         patch.object(tm, "_candidate_has_quota", return_value=True), \
         patch.object(tm, "_jev_choose", side_effect=lambda task, pool, **_: captured.extend(pool) or pool), \
         patch.object(tm, "_run_candidate", return_value={"outcome": "needs_verification",
                                                           "summary": "VERDICT: PASS"}):
        result = tm._external_verification("8" * 32, tmp_path, "review", [], routes,
                                           routes[0], None, 1, memory_mode="standalone")
    assert [candidate["model"] for candidate in captured] == ["reviewer-one", "reviewer-two"]
    assert result["passed"] is True


@pytest.mark.parametrize("text", ["cli-router: [broken", "cli-router: {}\nextra: true\n",
                                    "cli-router:\n  providers:\n    codex:\n      excluded_models: nope\n"])
def test_malformed_config_is_reported(tmp_path, text):
    config = write_config(tmp_path, text)
    with config:
        with pytest.raises(tm.ConfigurationError, match="cli-router.yaml"):
            tm._load_modular_config()


def test_missing_file_keeps_legacy_memory_and_review_defaults(tmp_path):
    context = type("Context", (), {"get_config": lambda self, name, default=None:
                                   {"settings": {"ai-memory": "disabled"}}})()
    with patch.object(tm, "_plugin_dir", return_value=tmp_path), \
         patch.object(hermes_adapter, "_CONTEXT", context):
        assert tm._ai_memory_mode() == "disabled"
        assert tm._jev_enabled() is True
        assert tm._independent_review_enabled() is True


def test_jev_disabled_ranks_deterministically_without_constructing_service():
    class Context:
        def get_config(self, name, default=None):
            return {}

    with patch.object(tm, "_plugin_dir", return_value=Path("/missing")), \
         patch.object(hermes_adapter, "_CONTEXT", Context()), \
         patch.object(tm, "_load_modular_config", return_value={"jev": False}), \
         patch.object(tm, "_jev_choose_pinned", side_effect=AssertionError("Jev called")), \
         patch.object(tm, "_fallback_rank", side_effect=lambda candidates, *_: candidates):
        assert tm._jev_choose("task", [{"cli": "codex", "model": "m1"}]) == [
            {"cli": "codex", "model": "m1"}]


def test_jev_disabled_handoff_uses_progress_rule_without_calling_service(tmp_path):
    task_id = "f" * 32
    with patch.object(tm, "_plugin_dir", return_value=tmp_path), \
         patch.object(tm, "_load_modular_config", return_value={"jev": False}), \
         patch.object(tm, "_root", return_value=tmp_path), \
         patch.object(hermes_adapter, "jev_service", side_effect=AssertionError("Jev called")):
        tm._write(task_id, {"task_id": task_id, "events": [], "route_warnings": []})
        assert tm._jev_handoff("task", {"changed_files": ["x"]}, {}, {}, "", task_id)
        assert not tm._jev_handoff("task", {"changed_files": []}, {}, {}, "", task_id)
        data = tm._read(task_id)
    assert any("deterministic handoff continuation used" in warning
               for warning in data["route_warnings"])


@pytest.mark.parametrize("acceptance_passes,expected_state", [(True, "completed"), (False, "failed_verification")])
def test_review_disabled_records_skip_and_keeps_acceptance_gate(tmp_path, acceptance_passes, expected_state):
    task_id = "9" * 32
    repo = tmp_path / "repo"
    repo.mkdir()
    route = {"cli": "codex", "model": "m1", "effort": "low"}
    memory = {"workstream_id": "stream", "latest_sequence": 1}
    checks = {"checks": {"git_diff_clean": {"ok": True}},
              "changed_files": ["changed.py"], "head_unchanged": False}
    with patch.object(tm, "_root", return_value=tmp_path), \
         patch.object(tm, "_plugin_dir", return_value=tmp_path), \
         patch.object(tm, "_load_modular_config", return_value={"independent-review": False}), \
         patch.object(tm, "_git_readonly", return_value=type("Result", (), {"stdout": str(repo)})()), \
         patch.object(tm, "_git_changed_paths", return_value=[]), \
         patch.object(tm, "_path_digests", return_value={}), \
         patch.object(tm, "_candidate_has_quota", return_value=True), \
         patch.object(tm, "_independent_checks", return_value=checks), \
         patch.object(tm, "_acceptance", return_value={"passed": acceptance_passes}), \
         patch.object(tm.ai_memory_bridge, "verify_runtime"), \
         patch.object(tm, "_run_candidate", return_value={"outcome": "needs_verification",
                                                           "summary": "implemented",
                                                           "ai_memory": memory}), \
         patch.object(tm, "_external_verification", side_effect=AssertionError("review launched")):
        tm._write(task_id, {"task_id": task_id, "state": "queued"})
        tm._worker(task_id, repo, "task", "change", [], [route], None)
        data = tm._read(task_id)
    assert data["state"] == expected_state
    if acceptance_passes:
        review = data["verification"]["external"]
        assert review["skipped"] is True
        assert "passed" not in review
        assert data["message"] == "acceptance checks passed; independent review skipped by configuration"
    else:
        review = data["verification"]["external"]
        assert review["skipped"] is True
        assert "local acceptance checks failed" in review["summary"]
        assert "passed" not in review



_YAML = Path("cli-router.yaml")


@pytest.mark.parametrize("text", [
    "cli-router:\n  jev: false\n    independent-review: false\n",
    "cli-router:\n  jev: false\n   independent-review: false\n",
    "cli-router:\n    jev: false\n  independent-review: false\n",
    "cli-router:\n  providers:\n    claude:\n      enabled: true\n     x: 1\n",
])
def test_misindented_entries_raise_with_file_and_line(text):
    with pytest.raises(tm.ConfigurationError, match=r"cli-router\.yaml: line \d+"):
        tm._parse_config_yaml(text, _YAML)


def test_misindent_error_names_line_number():
    with pytest.raises(tm.ConfigurationError, match="line 3"):
        tm._parse_config_yaml("cli-router:\n  jev: false\n    independent-review: false\n", _YAML)


def test_valid_nested_providers_with_comments_and_blanks_still_parse():
    text = ("# top\ncli-router:\n\n  # c\n  providers:\n    claude:\n      enabled: true\n"
            "      excluded_models:\n        - a\n        - b\n    codex:\n      enabled: false\n"
            "  jev: false\n")
    assert tm._parse_config_yaml(text, _YAML) == {"cli-router": {
        "providers": {"claude": {"enabled": True, "excluded_models": ["a", "b"]},
                      "codex": {"enabled": False}},
        "jev": False}}
