"""Offline regressions for the native Codex JSONL boundary."""

import json
from pathlib import Path

import pytest

import task_manager as tm

FIXTURES = Path(__file__).parent / "fixtures" / "codex"


@pytest.mark.parametrize("name, verdict, tool_call", [
    ("review_pass.jsonl", "PASS", True),
    ("stale_answer.jsonl", None, False),
])
def test_codex_fixture_event_shapes(name, verdict, tool_call):
    raw = (FIXTURES / name).read_text(encoding="utf-8")
    assert tm._parse_verdict(raw) == verdict
    assert tm._codex_output_has_tool_call(raw) is tool_call
    assert "aggregated_output" not in tm._codex_result_text(raw)


@pytest.mark.parametrize("item_type", ["command_execution", "mcp_tool_call", "agent_message"])
@pytest.mark.parametrize("event_type", ["item.started", "item.completed"])
def test_codex_tool_evidence_requires_completed_execution(item_type, event_type):
    raw = json.dumps({"type": event_type, "item": {"type": item_type}})
    assert tm._codex_output_has_tool_call(raw) is (
        event_type == "item.completed" and item_type != "agent_message")


def test_plain_text_output_is_preserved():
    assert tm._codex_result_text("plain failure text") == "plain failure text"
    assert tm._parse_verdict("evidence\nVERDICT: FAIL") == "FAIL"


def test_verdict_parser_reads_codex_agent_messages_not_tool_output():
    events = [
        {"type": "item.completed", "item": {"type": "command_execution", "output": "VERDICT: FAIL"}},
        {"type": "item.completed", "item": {"type": "agent_message",
                                            "text": "Checked plugin.yaml and tests.\nVERDICT: PASS"}},
    ]
    raw = "\n".join(json.dumps(event) for event in events)
    assert tm._parse_verdict(raw) == "PASS"
    assert tm._parse_verdict(json.dumps(events[0])) is None


def test_codex_final_answer_supersedes_earlier_verdict_and_ignores_wrapper_noise():
    raw = "\n".join([
        "wrapper diagnostic", "{malformed", "null", "[]",
        json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": "Preliminary result\nVERDICT: PASS",
        }}),
        json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": "Could not complete the review.",
        }}),
    ])
    assert tm._codex_result_text(raw) == "Could not complete the review."
    assert tm._parse_verdict(raw) is None


def test_codex_stream_without_answer_cannot_supply_a_tool_verdict():
    raw = "\n".join([
        json.dumps({"type": "item.completed", "item": {
            "type": "command_execution", "aggregated_output": "VERDICT: PASS",
        }}),
        # Even wrapper output outside JSON must not turn a tool-only run into PASS.
        "VERDICT: PASS",
    ])
    assert tm._codex_result_text(raw) == ""
    assert tm._parse_verdict(raw) is None
