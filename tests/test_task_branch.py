import subprocess
import sys
from functools import partial
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import task_manager as tm


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgSign=false", *args],
        check=True, capture_output=True, text=True,
    ).stdout.rstrip("\n")


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "module").mkdir(parents=True)
    git(repo, "init", "-qb", "main")
    (repo / "module" / "file").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "baseline")
    return repo


@pytest.mark.parametrize("linked", [False, True])
def test_select_task_branch_with_dirty_module(repo, tmp_path, linked):
    worktree = repo
    if linked:
        worktree = tmp_path / "linked"
        git(repo, "worktree", "add", "-qb", "linked-base", str(worktree))
    module = worktree / "module"
    base = git(module, "rev-parse", "HEAD")
    tracked = module / "file"
    tracked.write_text("staged\n")
    git(module, "add", "file")
    tracked.write_text("unstaged\n")
    (module / "untracked").write_text("user file\n")
    status = git(worktree, "status", "--porcelain")

    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(f'#!/bin/sh\nprintf ran > "{tmp_path / "hook-ran"}"\n')
    hook.chmod(0o700)
    git(repo, "config", "--local", "core.hooksPath", str(hook.parent))

    tm._prepare_task_branch(module, "task/demo")
    tm._prepare_task_branch(module, "task/demo")
    assert git(module, "symbolic-ref", "HEAD") == "refs/heads/task/demo"
    assert git(module, "rev-parse", "HEAD") == base
    assert git(worktree, "status", "--porcelain") == status
    assert git(worktree, "show", ":module/file") == "staged"
    assert tracked.read_text() == "unstaged\n"
    assert (module / "untracked").read_text() == "user file\n"
    assert not (tmp_path / "hook-ran").exists()
    assert git(repo, "rev-parse", "refs/heads/main") == base
    if linked:
        assert git(repo, "symbolic-ref", "HEAD") == "refs/heads/main"


@pytest.mark.parametrize("linked", [False, True])
def test_worker_commits_with_read_only_head(repo, tmp_path, linked):
    worktree = repo
    if linked:
        worktree = tmp_path / "linked"
        git(repo, "worktree", "add", "-qb", "linked-base", str(worktree))
    base = git(worktree, "rev-parse", "HEAD")
    tm._prepare_task_branch(worktree, "task/demo")
    home = tmp_path / "empty-home"
    home.mkdir()
    isolated, launcher = tm._sandbox_launcher("kimi", "/bin/sh", worktree, "change", home=home)

    def inside(command, *args, check=True):
        return subprocess.run([launcher, "-c", command, "test", *args],
                              check=check, capture_output=True, text=True)

    def worker_git(*args, check=True):
        return inside('exec git -c user.name=Test -c user.email=test@example.invalid '
                      '-c commit.gpgSign=false "$@"', *args, check=check)

    try:
        assert worker_git("symbolic-ref", "HEAD").stdout.strip() == "refs/heads/task/demo"
        inside("printf 'worker edit\\n' > module/file")
        worker_git("add", "module/file")
        worker_git("commit", "-qm", "implementation")
        assert git(repo, "show", "refs/heads/task/demo:module/file") == "worker edit"
        assert git(repo, "rev-parse", "refs/heads/main") == base
        assert git(worktree, "status", "--porcelain") == ""
        assert worker_git("switch", "--detach", check=False).returncode
        assert worker_git("config", "--local", "test.sandbox", "writable", check=False).returncode
        hook = repo / ".git" / "hooks" / "test-hook"
        assert inside('printf ran > "$1"', str(hook), check=False).returncode
        assert not hook.exists()
        assert git(worktree, "symbolic-ref", "HEAD") == "refs/heads/task/demo"
    finally:
        isolated.cleanup()


@pytest.fixture
def worker_state(tmp_path, monkeypatch):
    task_id = "a" * 32
    routes = [{"cli": "kimi", "model": name, "effort": None} for name in ("first", "second")]
    monkeypatch.setattr(tm, "_root", lambda: tmp_path / "router-home")
    monkeypatch.setattr(tm, "_candidate_has_quota", lambda _: True)
    tm._write(task_id, {"task_id": task_id, "state": "queued", "branch": "task/demo",
                        "ai_memory": {"mode": "standalone"}})
    return task_id, routes


def test_branch_collision_blocks_before_provider(repo, worker_state, monkeypatch):
    task_id, routes = worker_state
    git(repo, "branch", "task/demo")
    run = Mock()
    monkeypatch.setattr(tm, "_run_candidate", run)

    tm._worker(task_id, repo, "change", "change", [{"type": "git_diff_clean"}], routes, None)

    data = tm._read(task_id)
    assert data["state"] == "blocked"
    assert data["events"][-1]["event"] == "task_branch_blocked"
    assert "already exists" in data["message"]
    assert git(repo, "symbolic-ref", "HEAD") == "refs/heads/main"
    run.assert_not_called()


def test_branch_selected_before_worker_baseline_and_review(repo, worker_state, monkeypatch):
    task_id, routes = worker_state
    base = git(repo, "rev-parse", "HEAD")

    def run(*_args, branch, **_kwargs):
        assert branch == "task/demo"
        assert git(repo, "symbolic-ref", "HEAD") == "refs/heads/task/demo"
        assert tm._read(task_id)["base_head"] == base
        (repo / "module" / "file").write_text("worker edit\n")
        git(repo, "commit", "-qam", "implementation")
        return {"outcome": "needs_verification"}

    review = Mock(return_value={"passed": True})
    monkeypatch.setattr(tm, "_run_candidate", run)
    monkeypatch.setattr(tm, "_independent_review_enabled", lambda: True)
    monkeypatch.setattr(tm, "_external_verification", review)

    tm._worker(task_id, repo, "change", "change", [{"type": "file_changed", "path": "module/file"}], routes, None)

    assert tm._read(task_id)["state"] == "completed"
    assert review.call_args.kwargs == {"memory_mode": "standalone", "base_head": base, "branch": "task/demo"}
    assert git(repo, "symbolic-ref", "HEAD") == "refs/heads/task/demo"


@pytest.mark.parametrize("outcome", ["needs_verification", "model_unavailable"])
def test_worktree_switch_prevents_review_and_retry(repo, worker_state, monkeypatch, outcome):
    task_id, routes = worker_state
    data = tm._read(task_id)
    data["ai_memory"]["mode"] = "managed"
    tm._write(task_id, data)

    def run(*_args, **_kwargs):
        git(repo, "switch", "main")
        (repo / "module" / "file").write_text("partial edit\n")
        return {"outcome": outcome, "ai_memory": {"workstream_id": "b" * 36}}

    attempt = Mock(side_effect=run)
    review = Mock()
    monkeypatch.setattr(tm, "_run_candidate", attempt)
    monkeypatch.setattr(tm, "_external_verification", review)
    monkeypatch.setattr(tm.ai_memory_bridge, "verify_runtime", lambda *_: None)
    monkeypatch.setattr(tm.ai_memory_bridge, "read_context_with_wait", lambda *_, **__: ("context", 1))
    monkeypatch.setattr(tm, "_jev_handoff", lambda *_, **__: True)

    tm._worker(task_id, repo, "change", "change", [{"type": "file_changed", "path": "module/file"}], routes, None)

    data = tm._read(task_id)
    assert data["state"] == "blocked"
    assert "no longer checked out" in data["message"]
    assert (repo / "module" / "file").read_text() == "partial edit\n"
    assert data["attempts"][0]["changed_files"] == ["module/file"]
    attempt.assert_called_once()
    review.assert_not_called()


@pytest.mark.parametrize(("branch", "matching"), [(None, True), ("task/demo", True), (None, False)])
def test_both_reviewers_get_commit_evidence_despite_dirty_main(repo, tmp_path, monkeypatch, branch, matching):
    base = git(repo, "rev-parse", "HEAD")
    ref = "refs/heads/task/demo"
    file = repo / "module" / "file"
    file.write_text("committed change\n")
    git(repo, "add", ".")
    commit = git(repo, "commit-tree", git(repo, "write-tree"), "-p", base, "-m", "implementation")
    git(repo, "update-ref", ref, commit)
    git(repo, "reset", "-q", "HEAD")
    if not matching:
        file.write_text("different snapshot bytes\n")
    assert git(repo, "status", "--porcelain") == " M module/file"

    # A local CLI fixture verifies the prompt and checks the Git evidence inside the review sandbox.
    reviewer = tmp_path / "reviewer"
    reviewer.write_text(f"#!{sys.executable}\n" + dedent(f"""\
        import subprocess
        import sys
        from pathlib import Path

        prompt = sys.argv[sys.argv.index("-p") + 1]
        assert "The router captured base commit {base}." in prompt
        if {bool(branch)!r}:
            assert 'The requested task ref is "{ref}".' in prompt
        else:
            assert "Commit on task/demo" in prompt and "branch named only in" in prompt
        assert "HEAD read-only" in prompt and "dirty status" in prompt
        assert "Inspect the commit range" in prompt and "committed files with the snapshot" in prompt

        def git(*args):
            return subprocess.check_output(["git", *args])

        assert git("symbolic-ref", "HEAD") == b"refs/heads/main\\n"
        assert git("status", "--porcelain") == b" M module/file\\n"
        assert git("rev-list", "{base}..{ref}").decode().strip() == {commit!r}
        matches = git("show", "{ref}:module/file") == Path("module/file").read_bytes()
        print("VERDICT: PASS" if matches else "VERDICT: FAIL")
    """))
    reviewer.chmod(0o700)
    home = tmp_path / "empty-home"
    home.mkdir()
    which = tm.shutil.which
    monkeypatch.setattr(tm.shutil, "which", lambda name, **kw: str(reviewer) if name == "kimi" else which(name, **kw))
    monkeypatch.setattr(tm, "_root", lambda: tmp_path / "router-home")
    monkeypatch.setattr(tm, "_sandbox_launcher", partial(tm._sandbox_launcher, home=home))
    monkeypatch.setattr(tm, "_candidate_has_quota", lambda _: True)
    monkeypatch.setattr(tm.worker_resources, "scope_command", lambda command, *_: command)
    monkeypatch.setattr(tm.worker_resources, "acquire",
                        lambda *_, **__: SimpleNamespace(fd=-1, fds=(), close=lambda: None))
    routes = [{"cli": "kimi", "model": name, "effort": None} for name in ("cheap", "strong")]
    monkeypatch.setattr(tm, "_jev_choose", lambda *_, **__: routes)
    task = "Commit the change" if branch else "Commit on task/demo"

    review = tm._external_verification("a" * 32, repo, task, [], routes, routes[0], None, 2,
                                       memory_mode="standalone", base_head=base, branch=branch)

    verdict = "PASS" if matching else "FAIL"
    assert review["passed"] is matching, review["summary"]
    assert [attempt["verdict"] for attempt in review["reviews"]] == [verdict, verdict], review["summary"]
