import json
import subprocess
import uuid
from pathlib import Path
from unittest.mock import patch

import task_manager as tm


def run_with_ledger(tmp_path, *, marker_present):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "seed").write_text("seed")
    subprocess.run(["git", "-C", str(repo), "add", "seed"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)

    task_id = "c" * 32
    stream_id = str(uuid.UUID(task_id))
    marker = f"Code task {task_id} attempt 1"
    events = [
        {"sequence": sequence, "agent": "codex", "kind": "message",
         "role": "user" if marker_present and sequence == 1 else "assistant",
         "content": marker if marker_present and sequence == 1 else f"verification output {sequence}"}
        for sequence in range(1, 41)
    ]
    run_outcomes = [
        {"outcome": "quota_limited", "reason": "rotate", "ai_memory": {
            "workstream_id": stream_id, "workstream_name": tm.ai_memory_bridge.workstream_name(task_id),
            "latest_sequence": 40}},
        {"outcome": "account_stop", "reason": "stop", "ai_memory": {
            "workstream_id": stream_id, "workstream_name": tm.ai_memory_bridge.workstream_name(task_id),
            "latest_sequence": 40}},
    ]

    with patch.object(tm, "_root", return_value=tmp_path / "home"), \
         patch.object(tm, "_candidate_has_quota", return_value=True), \
         patch.object(tm.ai_memory_bridge, "verify_runtime"), \
         patch.object(tm.ai_memory_bridge, "_command", return_value=json.dumps(events)), \
         patch.object(tm, "_run_candidate", side_effect=run_outcomes) as run, \
         patch.object(tm, "_independent_checks", return_value={"changed_files": []}), \
         patch.object(tm, "_jev_handoff", return_value=True):
        tm._write(task_id, {"task_id": task_id, "state": "queued"})
        tm._worker(task_id, repo, "task", "change", [], [
            {"cli": "codex", "model": "first", "effort": "low"},
            {"cli": "codex", "model": "second", "effort": "low", "account": "other"},
        ], None)
        data = tm._read(task_id)
    return data, run, marker


def test_marker_outside_context_window_still_allows_next_attempt(tmp_path):
    data, run, marker = run_with_ledger(tmp_path, marker_present=True)

    assert run.call_count == 2
    assert marker not in run.call_args_list[1].kwargs["memory_context"]
    assert data["attempts"][0]["outcome"] == "quota_limited"


def test_marker_absent_from_full_ledger_keeps_failover_stop(tmp_path):
    data, run, _ = run_with_ledger(tmp_path, marker_present=False)

    assert run.call_count == 1
    assert data["state"] == "blocked"
    assert data["ai_memory"]["error"] == (
        "prior attempt is absent from canonical memory; failover stopped")
