import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import task_manager as tm


def _record(**updates):
    return {
        "task_id": "a" * 32,
        "owner_hash": hashlib.sha256(b"owner-token").hexdigest(),
        "state": "completed",
        "phase": "completed",
        **updates,
    }


def test_compact_status_shows_route_and_ordered_deduplicated_routes_used():
    data = _record(
        route={"cli": "claude", "provider": "anthropic", "model": "opus", "effort": "high",
               "api_rank": 1, "token": "do-not-show"},
        attempts=[
            {"cli": "claude", "model": "opus", "effort": "high", "token": "attempt-secret"},
            {"cli": "claude", "model": "opus", "effort": "high"},
            {"cli": "codex", "model": "gpt", "effort": "low", "path": "/private/repo"},
        ],
        verification={"external": {"reviews": [
            {"route": {"cli": "kimi", "model": "k2.8", "effort": "medium", "prompt": "private"}},
            {"route": {"cli": "kimi", "model": "k2.8", "effort": "medium"}},
            {"route": {"cli": "claude", "model": "opus", "effort": "high"}},
        ]}},
    )

    result = tm._status_view(data)

    assert result["route"] == {"cli": "claude", "model": "opus", "effort": "high"}
    assert result["routes_used"] == [
        {"role": "worker", "cli": "claude", "model": "opus", "effort": "high"},
        {"role": "worker", "cli": "codex", "model": "gpt", "effort": "low"},
        {"role": "review", "cli": "kimi", "model": "k2.8", "effort": "medium"},
        {"role": "review", "cli": "claude", "model": "opus", "effort": "high"},
    ]
    encoded = str({"route": result["route"], "routes_used": result["routes_used"]})
    for secret in ("token", "do-not-show", "attempt-secret", "private", "/private/repo", "anthropic"):
        assert secret not in encoded


def test_routes_used_is_omitted_before_any_route_and_history_shape_is_unchanged():
    data = _record(message="queued")

    compact = tm._status_view(data)
    detailed = tm._status_view(data, include_history=True)

    assert "route" not in compact
    assert "routes_used" not in compact
    assert detailed == {key: value for key, value in data.items() if key != "owner_hash"}


def test_compact_status_ignores_malformed_route_records_without_crashing():
    result = tm._status_view(_record(
        route="invalid",
        attempts=[None, {"cli": "codex"}, {"cli": "codex", "model": "gpt"}],
        verification={"external": {"reviews": [None, {"route": "invalid"}, {"route": {"model": "review"}}]}},
    ))

    assert "route" not in result
    assert result["routes_used"] == [
        {"role": "worker", "cli": "codex", "model": "gpt", "effort": None},
    ]


def test_routes_used_is_bounded_to_ten_entries():
    result = tm._status_view(_record(attempts=[
        {"cli": "codex", "model": f"worker-{index}", "effort": "low"}
        for index in range(12)
    ]))

    assert len(result["routes_used"]) == 10


def test_attempt_start_message_names_route():
    route = {"cli": "claude", "model": "claude-opus-5-5", "effort": "high"}
    data = _record(state="running")

    from unittest.mock import patch
    with patch.object(tm, "_write"):
        tm._transition(data, "worker_running", "attempt_started",
                       "CLI worker is running (claude / claude-opus-5-5)", event_message=
                       "CLI worker is running (claude / claude-opus-5-5)", route=route)

    assert data["message"] == "CLI worker is running (claude / claude-opus-5-5)"
    assert data["events"][-1]["event"] == "attempt_started"
    assert data["events"][-1]["message"] == "CLI worker is running (claude / claude-opus-5-5)"
