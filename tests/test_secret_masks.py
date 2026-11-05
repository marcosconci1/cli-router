"""Secret-mask regressions use an isolated home and never read real credentials."""

from pathlib import Path

import pytest

import task_manager as tm


def masked_paths(home, repo):
    scratch = home / "scratch"
    scratch.mkdir(exist_ok=True)
    args = tm._secret_masks("claude", repo, scratch, home)
    assert (scratch / "empty").read_bytes() == b""
    return {Path(args[i + 2]) for i, arg in enumerate(args) if arg == "--ro-bind"}


@pytest.mark.parametrize("relative", [
    ".env", ".env.production", "projects/nested/app/.env",
    "projects/nested/app/.env.local", ".aws/credentials", "nested/credentials.json",
])
def test_nested_secret_files_are_masked_but_repository_secrets_stay(tmp_path, relative):
    repo = tmp_path / "repo"
    outside = tmp_path / relative
    inside = repo / relative
    for path in (outside, inside):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic-secret")
    masked = masked_paths(tmp_path, repo)
    assert outside in masked
    assert inside not in masked
    assert outside.read_text() == "synthetic-secret"


def test_directory_symlinks_and_dependency_trees_are_not_traversed(tmp_path):
    home = tmp_path / "home"
    repo = home / "repo"
    repo.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    (external / ".env").write_text("synthetic")
    (home / "linked").symlink_to(external, target_is_directory=True)
    for name in (".git", ".venv", "node_modules", ".cache"):
        dependency = home / name
        dependency.mkdir()
        (dependency / ".env").write_text("synthetic")
    assert masked_paths(home, repo) == set()


def test_secret_file_symlinks_preserve_repository_target_exception(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    own = repo / ".env"
    own.write_text("repository-secret")
    alias = tmp_path / "credentials-repo"
    alias.symlink_to(own)
    external = tmp_path / "external.txt"
    external.write_text("outside-secret")
    secret_alias = tmp_path / ".env.link"
    secret_alias.symlink_to(external)
    masked = masked_paths(tmp_path, repo)
    assert alias not in masked
    assert own not in masked
    assert secret_alias not in masked
    assert external.resolve() in masked


def test_symlinked_secret_under_pytest_basetemp_is_not_masked(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    fixture = tmp_path / "pytest-of-fakeuser" / "pytest-42"
    fixture.mkdir(parents=True)
    real = fixture / "real.txt"
    real.write_text("fixture")
    (fixture / ".env.link").symlink_to(real)
    assert masked_paths(tmp_path, repo) == set()


def test_dangling_secret_symlink_produces_no_mask(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / ".env").symlink_to(tmp_path / "missing")
    assert masked_paths(tmp_path, repo) == set()


def test_real_home_scan_never_masks_ephemeral_tmpdir_trees(tmp_path, monkeypatch):
    """The widened **/.env.* glob must not bind ephemeral temp fixtures.

    TMPDIR lives under the Hermes scratch tree, pytest leaves fixture .env files
    there, and those paths vanish between runs. A bind whose source disappears
    kills bwrap before the worker starts, so the real-home scan must skip the
    temp root entirely. Callers that pass a synthetic home keep full coverage.
    """
    import os
    import shutil

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    probe_root = Path(os.environ.get("TMPDIR", "/tmp")) / "router-mask-probe"
    probe = probe_root / ".env"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text("PROBE=1")
    try:
        args = tm._secret_masks("claude", tmp_path, scratch)  # home=None: real-home scan
        assert not any("router-mask-probe" in str(a) for a in args)
    finally:
        shutil.rmtree(probe_root, ignore_errors=True)
