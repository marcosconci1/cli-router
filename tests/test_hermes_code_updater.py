from contextlib import nullcontext
import fcntl
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import task_manager

spec = importlib.util.spec_from_file_location("code_updater", ROOT / "scripts" / "update-hermes-code.py")
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)

OLD = "a" * 40
NEW = "b" * 40


@pytest.fixture
def install(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "code"
    tasks = home / "runtime" / "cli-tasks"
    tasks.mkdir(parents=True)
    target = home / "plugins" / updater.PLUGIN
    target.mkdir(parents=True)
    (target / "source").write_text(OLD)
    metadata = {updater.PLUGIN: {"revision": OLD, "pinned": False}, "another-plugin": {"revision": "keep"}}

    fake_hermes = ModuleType("hermes_cli")
    fake_hermes.plugins_cmd = SimpleNamespace(_read_install_metadata=lambda: metadata.copy())
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins_auto_update",
                        SimpleNamespace(plugin_update_lock=lambda _: nullcontext()))

    def change(source, destination, new_metadata):
        (destination / "source").write_text((source / "source").read_text())
        metadata.update(new_metadata)

    def publish(destination, revision, *, bootstrap):
        (destination / "source").write_text(revision)
        metadata[updater.PLUGIN] = {"revision": revision, "pinned": False}

    monkeypatch.setitem(sys.modules, "hermes_cli.plugins_transaction",
                        SimpleNamespace(publish_plugin=lambda a, b, _old, new: change(a, b, new)))
    monkeypatch.setattr(updater, "_remote_head", Mock(return_value=NEW))
    monkeypatch.setattr(updater, "_tests_passed", Mock(return_value=True))
    monkeypatch.setattr(updater, "_head", lambda tree: (tree / "source").read_text())
    monkeypatch.setattr(updater, "_verify_checkout", Mock())
    monkeypatch.setattr(updater, "_publish", Mock(side_effect=publish))
    monkeypatch.setattr(updater, "_reload", Mock())
    return SimpleNamespace(home=home, tasks=tasks, target=target, metadata=metadata,
                           record=home / "runtime" / "cli-router-updater" / "status.json")


def test_verified_update_then_unchanged(install):
    assert f"Activated {NEW}" in updater.update(install.home)
    assert json.loads(install.record.read_text()) == {"activated": NEW}
    assert updater._head(install.target) == NEW
    assert install.metadata[updater.PLUGIN]["revision"] == NEW
    assert "Already active" in updater.update(install.home)
    updater._reload.assert_called_once_with(install.home)
    updater._tests_passed.assert_called_once_with(NEW)


@pytest.mark.parametrize("status, conclusion", [("in_progress", None), ("completed", "failure"),
                                              ("completed", "success")])
def test_github_ci_requires_a_successful_push_for_exact_main_sha(monkeypatch, status, conclusion):
    from io import BytesIO

    runs = {"workflow_runs": [
        {"head_sha": OLD, "head_branch": "main", "event": "push", "status": "completed", "conclusion": "success"},
        {"head_sha": NEW, "head_branch": "main", "event": "pull_request", "status": "completed",
         "conclusion": "success"},
        {"head_sha": NEW, "head_branch": "main", "event": "push", "status": status, "conclusion": conclusion},
    ]}
    retrieve = Mock(return_value=BytesIO(json.dumps(runs).encode()))
    monkeypatch.setattr(updater, "urlopen", retrieve)
    assert updater._tests_passed(NEW) is (conclusion == "success")
    assert f"head_sha={NEW}" in retrieve.call_args.args[0].full_url


def test_defers_for_ci_and_all_active_states(install):
    updater._tests_passed.return_value = False
    assert "Waiting" in updater.update(install.home)
    updater._tests_passed.return_value = True
    task = install.tasks / f"{'0' * 32}.json"
    for state in ("queued", "running", "cancelling"):
        task.write_text(json.dumps({"state": state}))
        assert "Deferred" in updater.update(install.home)
    updater._publish.assert_not_called()
    updater._reload.assert_not_called()


def test_submission_and_updater_share_the_lock(install, monkeypatch):
    monkeypatch.setattr(task_manager, "_task_dir", lambda: install.tasks)
    monkeypatch.setattr(task_manager, "_journal_startup", lambda: None)
    submit = Mock()
    monkeypatch.setattr(task_manager, "_start_submission", submit)
    monkeypatch.setattr(task_manager, "_repo", lambda _: install.target)
    args = {"action": "submit", "task": "test", "mode": "change",
            "acceptance_criteria": [{"type": "file_exists", "path": "source"}]}
    with (install.tasks / updater.SUBMISSION_LOCK).open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert "updating" in task_manager.dispatch(args)["error"]
    submit.assert_not_called()
    with (install.tasks / updater.SUBMISSION_LOCK).open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        assert "accepting a task" in updater.update(install.home)
    updater._publish.assert_not_called()


def test_failed_reload_restores_previous_checkout_and_holds_revision(install):
    updater._reload.side_effect = [RuntimeError("reload failed"), None]
    with pytest.raises(RuntimeError, match="reload failed"):
        updater.update(install.home)
    assert updater._head(install.target) == OLD
    assert install.metadata[updater.PLUGIN]["revision"] == OLD
    assert install.metadata["another-plugin"]["revision"] == "keep"
    assert json.loads(install.record.read_text()) == {"failed": NEW}
    assert "Holding failed revision" in updater.update(install.home)
    updater._publish.assert_called_once()


def test_bootstrap_backs_up_dirty_pinned_checkout(install):
    install.metadata[updater.PLUGIN]["pinned"] = True
    (install.target / "private-patch").write_text("local routing work")
    assert f"Activated {NEW}" in updater.update(install.home, bootstrap=True)
    backup, = install.record.parent.glob("bootstrap-*")
    assert (backup / "checkout" / "private-patch").read_text() == "local routing work"
    assert json.loads((backup / "install-record.json").read_text())["pinned"] is True



def test_native_bootstrap_publishes_tracking_metadata_with_verified_tree(install, monkeypatch):
    metadata = install.metadata
    metadata[updater.PLUGIN]["pinned"] = True
    plugin_cmd = sys.modules["hermes_cli"].plugins_cmd

    def clone(staged, origin, ref):
        assert origin == updater.ORIGIN
        assert ref is None
        staged.mkdir()
        (staged / "source").write_text(NEW)

    def native_publish(staged, target, before, after, *, target_digest):
        assert target == install.target
        assert before[updater.PLUGIN]["pinned"]
        assert after[updater.PLUGIN] == {"pinned": False, "revision": NEW, "source": updater.ORIGIN}
        assert after["another-plugin"] == before["another-plugin"]
        assert (staged / "source").read_text() == NEW
        assert target_digest == OLD
        plugin_cmd._scan_merged_tree.assert_called_once_with(staged, updater.ORIGIN, None, force=False)

    plugin_cmd._clone_plugin_repo = clone
    plugin_cmd._read_manifest_for_install = Mock(return_value={"name": updater.PLUGIN})
    plugin_cmd._check_manifest_version = Mock()
    plugin_cmd._scan_merged_tree = Mock()
    transaction = sys.modules["hermes_cli.plugins_transaction"]
    transaction._refresh_declared_dependencies = Mock()
    transaction.publish_plugin = Mock(side_effect=native_publish)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins_cmd_catalog",
                        SimpleNamespace(refuse_if_installed_removed=Mock()))
    monkeypatch.setitem(sys.modules, "pm.store",
                        SimpleNamespace(tree_digest=lambda tree: (tree / "source").read_text()))
    updater._bootstrap(install.target, NEW)
    transaction.publish_plugin.assert_called_once()
