import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import task_manager as tm


def route(model, effort=None, cli="agy", account="account"):
    return {"cli": cli, "provider": "antigravity" if cli == "agy" else cli,
            "account": account, "model": model, "effort": effort}


@pytest.mark.parametrize("text,expected", [
    ("Individual quota reached. Resets in 82h34m42s", 82 * 3600 + 34 * 60 + 42),
    ("Resets in 34m42s", 34 * 60 + 42),
    ("Resets in 42s", 42),
    ("Resets in 5m", 300),
    ("quota exhausted without a reset hint", None),
])
def test_reset_hint_parsing(text, expected):
    assert tm._parse_reset_seconds(text) == expected


def test_cooldown_file_is_private_atomic_and_expires(tmp_path):
    candidate = route("gemini-4-argon-low", "low")
    with patch.object(tm, "_root", return_value=tmp_path):
        until = tm._record_quota_cooldown(candidate, "Resets in 2s", now=1000)
        path = tm._cooldown_path()
        assert until == 1002
        assert os.stat(path).st_mode & 0o777 == 0o600
        assert not list(path.parent.glob("*.tmp"))
        assert tm._route_cooling_down(candidate, now=1001)
        assert not tm._route_cooling_down(candidate, now=1002)
        assert json.loads(path.read_text()) == {}


def test_cooldown_clamped_to_nearest_future_quota_window(tmp_path):
    candidate = route("gemini-4-argon-low", "low", cli="agy", account="account")
    # Parsed reset hint says 80h (~288000s). Nearest window reset is in 2 hours (7200s).
    now = 10000.0
    quota_payload = {
        "fresh": True,
        "cache": {
            "fetched_at": "2026-11-02T18:00:00Z",
            "providers": {
                "antigravity": {
                    "windows": [
                        {"label": "session", "used_percent": 10.0, "reset_at": "2026-11-02T19:00:00Z"},  # past or future depending on timestamp
                        {"label": "daily", "used_percent": 20.0, "reset_at": "2026-11-02T22:00:00Z"},
                    ]
                }
            }
        }
    }
    # Let's use concrete timestamp values
    # now = 10000.0
    # window 1 reset_at = 10000 + 7200 (17200.0) -> ISO format
    dt1 = datetime.fromtimestamp(17200.0, tz=timezone.utc).isoformat()
    dt2 = datetime.fromtimestamp(25000.0, tz=timezone.utc).isoformat()
    quota_payload["cache"]["providers"]["antigravity"]["windows"][0]["reset_at"] = dt1
    quota_payload["cache"]["providers"]["antigravity"]["windows"][1]["reset_at"] = dt2

    with patch.object(tm, "_root", return_value=tmp_path), \
         patch.object(tm, "_quota", return_value=quota_payload):
        until = tm._record_quota_cooldown(candidate, "Resets in 80h", now=now)
        assert until == 17200.0


def test_cooldown_capped_at_24h_when_quota_cache_absent_or_stale(tmp_path):
    candidate = route("gemini-4-argon-low", "low")
    now = 10000.0
    # 1. When cache is absent or raises
    with patch.object(tm, "_root", return_value=tmp_path), \
         patch.object(tm, "_quota", side_effect=RuntimeError("no cache")):
        until = tm._record_quota_cooldown(candidate, "Resets in 80h", now=now)
        assert until == now + 24 * 3600

    # 2. When cache is stale
    stale_payload = {
        "fresh": False,
        "cache": {
            "providers": {
                "antigravity": {
                    "windows": [
                        {"label": "session", "reset_at": datetime.fromtimestamp(now + 3600, tz=timezone.utc).isoformat()}
                    ]
                }
            }
        }
    }
    with patch.object(tm, "_root", return_value=tmp_path), \
         patch.object(tm, "_quota", return_value=stale_payload):
        until = tm._record_quota_cooldown(candidate, "Resets in 80h", now=now)
        assert until == now + 24 * 3600

    # 3. When cache has no windows
    no_windows_payload = {
        "fresh": True,
        "cache": {
            "providers": {
                "antigravity": {
                    "windows": []
                }
            }
        }
    }
    with patch.object(tm, "_root", return_value=tmp_path), \
         patch.object(tm, "_quota", return_value=no_windows_payload):
        until = tm._record_quota_cooldown(candidate, "Resets in 80h", now=now)
        assert until == now + 24 * 3600


def test_cooldown_keeps_short_parsed_resets_as_is(tmp_path):
    candidate = route("gemini-4-argon-low", "low")
    now = 10000.0
    # Window reset is 2 hours away, but parsed error reset is only 15 minutes away
    dt_window = datetime.fromtimestamp(now + 7200, tz=timezone.utc).isoformat()
    quota_payload = {
        "fresh": True,
        "cache": {
            "providers": {
                "antigravity": {
                    "windows": [
                        {"label": "session", "reset_at": dt_window}
                    ]
                }
            }
        }
    }
    with patch.object(tm, "_root", return_value=tmp_path), \
         patch.object(tm, "_quota", return_value=quota_payload):
        until = tm._record_quota_cooldown(candidate, "Resets in 15m", now=now)
        assert until == now + 900


def test_family_cooldown_skips_routes_across_tasks(tmp_path):
    argon_low = route("gemini-4-argon-low", "low")
    argon_high = route("gemini-4-argon-high", "high")
    flash = route("gemini-3.8-flash-low", "low")
    eligible = [argon_low, argon_high, flash]
    with patch.object(tm, "_root", return_value=tmp_path), \
         patch.object(tm, "_quota", return_value={"fresh": True}), \
         patch.object(tm, "_installed_models", return_value={}), \
         patch.object(tm, "_subscriptions", return_value=eligible), \
         patch.object(tm.time, "time", return_value=1001):
        tm._record_quota_cooldown(argon_low, "429 without reset information", now=1000)
        assert not tm._route_cooling_down(argon_low, now=1001 + tm.DEFAULT_COOLDOWN_SECONDS)
        tm._record_quota_cooldown(argon_low, "429 without reset information", now=1000)
        assert not tm._candidate_has_quota(argon_high)
        assert tm._candidate_has_quota(flash)


def test_observed_counts_work_and_reviews_with_unreviewed_neutral(tmp_path):
    candidate = route("kimi-code/k3.1-pro", cli="kimi")
    recent = tm._stamp()
    attempts = [
        {**candidate, "outcome": outcome, "finished_at": recent}
        for outcome in ("verified", "unreviewed", "quota_limited", "timeout")
    ]
    attempts.append({**candidate, "outcome": "verified", "finished_at": "2000-01-01T00:00:00+00:00"})
    reviews = [
        {"route": candidate, "worker_outcome": "needs_verification", "passed": True, "verdict": "PASS"},
        {"route": candidate, "worker_outcome": "needs_verification", "passed": False, "verdict": None},
    ]
    task_id = "1" * 32
    with patch.object(tm, "_root", return_value=tmp_path):
        tm._write(task_id, {"task_id": task_id, "created_at": recent, "updated_at": recent,
                            "attempts": attempts, "verification": {"external": {"reviews": reviews}}})
        now = tm.datetime.now(tm.timezone.utc).timestamp()
        expected = {"attempts": 6, "verified": 2, "quota_limited": 1, "timeouts": 1}
        assert tm._observed(candidate, now=now) == expected
        assert tm._profiled(candidate, {"models": {}})["observed"] == expected
    assert tm.OBSERVED_INSTRUCTIONS in tm.ROUTE_INSTRUCTIONS


def test_jev_choices_and_handoffs_are_logged_without_prompts(tmp_path):
    task_id = "2" * 32
    candidate = route("kimi-code/k3.1-pro", cli="kimi")

    class Context:
        def get_config(self, name, default=None):
            return {"backend": "openrouter", "model": "typesafe/jev-1.14"}

    class Service:
        def __init__(self, ctx):
            pass

        def run(self, request):
            question = next(iter(request["questions"]))
            choice = "continue" if question == "handoff" else "route_0"
            return {"ok": True, "answers": {question: {"choice": choice}}}

    with patch.object(tm, "_root", return_value=tmp_path), \
         patch.object(tm.hermes_adapter, "_CONTEXT", Context()), \
         patch.object(tm.hermes_adapter, "_jev_service_cls", return_value=Service), \
         patch.object(tm, "_benchmark_index", return_value={"models": {}}):
        tm._write(task_id, {"task_id": task_id, "state": "running", "events": []})
        task_marker = "route-prompt-text-that-must-not-be-logged"
        assert tm._jev_choose(task_marker, [candidate], task_id=task_id) == [candidate]
        assert tm._jev_handoff(task_marker, {"outcome": "failed_verification"}, {}, candidate,
                               task_marker, task_id=task_id)
        data = tm._read(task_id)
    answers = [event for event in data["events"] if event["event"] == "jev_answer"]
    assert [(event["question"], event["choice"]) for event in answers] == [
        ("route", "route_0"), ("handoff", "continue"),
    ]
    assert answers[0]["options"][-1] == "stop"
    assert answers[1]["options"] == ["continue", "stop"]
    assert task_marker not in json.dumps(data)


def test_verdict_parser_accepts_only_a_final_window_standalone_line():
    assert tm._parse_verdict("evidence\nvErDiCt: pAsS") == "PASS"
    assert tm._parse_verdict("\n".join(["report"] * 19 + ["VERDICT: FAIL"])) == "FAIL"
    assert tm._parse_verdict("\n".join(["VERDICT: PASS"] + ["report"] * 20)) is None
    assert tm._parse_verdict("VERDICT: PASS with qualifications") is None
    assert tm._parse_verdict("full report without a decision") is None


def test_missing_verdict_is_unreviewed_not_failed(tmp_path):
    routes = [route("one", cli="kimi"), route("two", cli="kimi")]
    with patch.object(tm, "_candidate_has_quota", return_value=True), \
         patch.object(tm, "_jev_choose", return_value=routes), \
         patch.object(tm, "_run_candidate", return_value={
             "outcome": "needs_verification", "summary": "full report without a verdict",
         }):
        result = tm._external_verification("3" * 32, tmp_path, "review", [], routes, routes[0], None, 2)
    assert result["complete"]
    assert not result["passed"]
    assert not result["failed"]
    assert all(review["verdict"] is None for review in result["reviews"])


def review_result(tmp_path, routes, summaries):
    with patch.object(tm, "_candidate_has_quota", return_value=True), \
         patch.object(tm, "_jev_choose", side_effect=lambda task, candidates, **kwargs: candidates[:2]), \
         patch.object(tm, "_run_candidate", side_effect=[
             {"outcome": "needs_verification", "summary": summary} for summary in summaries
         ]) as run:
        result = tm._external_verification("4" * 32, tmp_path, "review", [], routes, routes[0], None, 2)
    return result, run


def test_two_codex_jsonl_pass_reviews_complete_verification(tmp_path):
    raw = (Path(__file__).parent / "fixtures/codex/review_pass.jsonl").read_text(encoding="utf-8")
    routes = [route("codex-reviewer", cli="codex"), route("kimi-reviewer", cli="kimi")]
    result, _ = review_result(tmp_path, routes, [raw, raw])
    assert result["complete"] and result["passed"]
    assert [review["verdict"] for review in result["reviews"]] == ["PASS", "PASS"]


def test_lower_tier_kimi_review_is_degraded_and_marked(tmp_path):
    strong = route("kimi-code/k3.1-pro", cli="kimi")
    lower = route("kimi-code/kimi-for-coding", cli="kimi")
    result, run = review_result(tmp_path, [strong, lower], ["VERDICT: PASS", "VERDICT: PASS"])
    assert result["passed"] and result["complete"]
    assert [call.args[0] for call in run.call_args_list] == [strong, lower]
    assert result["reviews"][0]["degraded_review"] is False
    assert result["reviews"][1]["degraded_review"] is True
    assert result["degraded_review"] is True
    assert "degraded_review: true" in result["summary"]


def test_degraded_review_is_not_used_with_two_proper_reviewers(tmp_path):
    strong = route("kimi-code/k3.1-pro", cli="kimi")
    lower = route("kimi-code/kimi-for-coding", cli="kimi")
    proper = route("proper-reviewer", cli="codex")
    result, run = review_result(tmp_path, [strong, lower, proper], ["VERDICT: PASS"] * 2)
    assert result["passed"]
    assert [call.args[0] for call in run.call_args_list] == [strong, proper]
    assert not result["degraded_review"]
    assert "degraded_review: true" not in result["summary"]


def test_degraded_fail_fails_and_missing_proper_pass_does_not_use_it(tmp_path):
    strong = route("kimi-code/k3.1-pro", cli="kimi")
    lower = route("kimi-code/kimi-for-coding", cli="kimi")
    failed, _ = review_result(tmp_path, [strong, lower], ["VERDICT: PASS", "VERDICT: FAIL"])
    assert failed["failed"] and not failed["passed"]
    unreviewed, run = review_result(tmp_path, [strong, lower], ["report without verdict"])
    assert not unreviewed["complete"] and not unreviewed["failed"]
    assert run.call_count == 1
