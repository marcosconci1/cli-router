"""The model-usage report reads task records and never prints secrets."""
import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cli-task-models.py"
spec = importlib.util.spec_from_file_location("cli_router_models", SCRIPT)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def write(directory, task_id, **fields):
    record = {"task_id": task_id, "created_at": "2026-11-02T02:00:00+00:00", "mode": "change",
              "repository": "/x/repo", "owner_hash": "SECRETHASH", **fields}
    (directory / f"{task_id}.json").write_text(json.dumps(record))


def test_lists_models_reviews_and_block_reasons(tmp_path, capsys):
    write(tmp_path, "a" * 32, state="completed",
          attempts=[{"attempt": 1, "cli": "agy", "model": "m1", "effort": "low", "outcome": "verified"}],
          verification={"external": {"reviews": [{"route": {"cli": "codex", "model": "m2"}, "passed": True,
                                                   "worker_outcome": "needs_verification"}]}})
    write(tmp_path, "b" * 32, state="blocked", message="Jev judged the persisted checkpoint unsuitable",
          attempts=[{"attempt": 1, "cli": "agy", "model": "m3", "outcome": "quota_limited"}])
    (tmp_path / "route-benchmark.json").write_text("{}")
    assert report.main(["--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "2 tasks" in out and "agy/m1@low" in out and "codex/m2 (review)" in out
    assert "Why tasks blocked" in out and "1x Jev judged the persisted checkpoint" in out
    assert "SECRETHASH" not in out


def test_one_task_by_prefix_and_json(tmp_path, capsys):
    write(tmp_path, "c" * 32, state="completed", attempts=[])
    assert report.main(["--dir", str(tmp_path), "--task", "cc"]) == 0
    assert '"task_id": "' + "c" * 32 in capsys.readouterr().out
    assert report.main(["--dir", str(tmp_path), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["states"] == {"completed": 1}
    assert "SECRETHASH" not in json.dumps(data)


def test_missing_directory_is_empty_not_an_error(tmp_path, capsys):
    assert report.main(["--dir", str(tmp_path / "none")]) == 0
    assert "0 tasks" in capsys.readouterr().out
