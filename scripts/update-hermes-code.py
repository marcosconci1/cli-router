#!/usr/bin/env python3
"""Deploy a CI-verified cli-router main revision to the Hermes Code profile."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PLUGIN = "cli-router"
REPOSITORY = "marcosconci1/cli-router"
ORIGIN = f"https://github.com/{REPOSITORY}.git"
SHA = re.compile(r"[0-9a-f]{40}")
TASK_FILE = re.compile(r"[0-9a-f]{32}\.json")
ACTIVE_STATES = {"queued", "running", "cancelling"}
SCRIPT = Path(__file__).resolve()
SUBMISSION_LOCK = ".plugin-update.lock"


def _git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], text=True, timeout=25, stdin=subprocess.DEVNULL,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    ).strip()


def _remote_head() -> str:
    revision, ref = _git("ls-remote", "--exit-code", ORIGIN, "refs/heads/main").split()
    if ref != "refs/heads/main" or not SHA.fullmatch(revision):
        raise RuntimeError("unexpected origin/main response")
    return revision


def _tests_passed(revision: str) -> bool:
    params = urlencode({"branch": "main", "event": "push", "head_sha": revision, "per_page": 1})
    url = f"https://api.github.com/repos/{REPOSITORY}/actions/workflows/tests.yml/runs?{params}"
    request = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "cli-router-updater"})
    with urlopen(request, timeout=15) as response:
        runs = json.load(response)["workflow_runs"]
    return any(run.get("head_sha") == revision and run.get("head_branch") == "main"
               and run.get("event") == "push" and run.get("status") == "completed"
               and run.get("conclusion") == "success" for run in runs)


def _busy(task_dir: Path) -> bool:
    for path in task_dir.glob("*.json"):
        if not TASK_FILE.fullmatch(path.name):
            continue
        try:
            with path.open(encoding="utf-8") as stream:
                if json.load(stream)["state"] in ACTIVE_STATES:
                    return True
        except FileNotFoundError:
            continue
    return False


def _head(tree: Path) -> str:
    return _git("-C", str(tree), "rev-parse", "HEAD")


def _verify_checkout(tree: Path, revision: str) -> None:
    checks = {
        ("rev-parse", "HEAD"): revision,
        ("symbolic-ref", "--short", "HEAD"): "main",
        ("rev-parse", "--abbrev-ref", "@{upstream}"): "origin/main",
        ("remote", "get-url", "origin"): ORIGIN,
        ("status", "--porcelain"): "",
    }
    for command, expected in checks.items():
        if _git("-C", str(tree), *command) != expected:
            raise RuntimeError(f"checkout verification failed for git {command[0]}")
    if not (tree / "scripts" / SCRIPT.name).is_file():
        raise RuntimeError("candidate is missing the updater required by the timer")


def _reload(home: Path) -> None:
    from gateway.control_socket import reload_gateway_plugins

    root = home.parent.parent
    answer = reload_gateway_plugins(root, profile_home=home)
    if answer is None:
        answer = reload_gateway_plugins(home)
    if (not answer or answer.get("reloaded") is not True or answer.get("home") != str(home)
            or PLUGIN not in answer.get("plugins", [])
            or not any(row.get("name") == PLUGIN and not row.get("missing_dependencies")
                       and "cli_router" in row.get("deferred", {}).get("tools", [])
                       for row in answer.get("activations", []))):
        raise RuntimeError("the Code gateway did not confirm cli-router activation")


def _bootstrap(target: Path, revision: str) -> None:
    from hermes_cli import plugins_cmd
    from hermes_cli.plugins_cmd_catalog import refuse_if_installed_removed
    from hermes_cli.plugins_transaction import _refresh_declared_dependencies, publish_plugin
    from pm.store import tree_digest

    refuse_if_installed_removed(PLUGIN, target)
    metadata = plugins_cmd._read_install_metadata()
    before = tree_digest(target)
    with tempfile.TemporaryDirectory(prefix=".install-", dir=target.parent) as temp:
        staged = Path(temp) / "checkout"
        plugins_cmd._clone_plugin_repo(staged, ORIGIN, None)
        _verify_checkout(staged, revision)
        manifest = plugins_cmd._read_manifest_for_install(staged)
        if manifest.get("name") != PLUGIN:
            raise RuntimeError("unexpected plugin name at origin/main")
        plugins_cmd._check_manifest_version(manifest, PLUGIN)
        plugins_cmd._scan_merged_tree(staged, ORIGIN, None)
        _refresh_declared_dependencies(target, staged, manifest, interactive=False)
        task_dir = target.parent.parent / "runtime" / "cli-tasks"
        if _busy(task_dir):
            raise RuntimeError("cli-router received work while staging; retry once idle")
        record = {"pinned": False, "revision": revision, "source": ORIGIN}
        publish_plugin(staged, target, metadata, {**metadata, PLUGIN: record}, target_digest=before)


def _publish(target: Path, revision: str, *, bootstrap: bool) -> None:
    if bootstrap:
        _bootstrap(target, revision)
        return
    from hermes_cli.plugins_auto_update import unattended_gate
    from hermes_cli.plugins_transaction import update_plugin

    _verify_checkout(target, _head(target))
    surface_gate = unattended_gate(PLUGIN, target)

    def gate(manifest: dict, staged: Path) -> None:
        _verify_checkout(staged, revision)
        surface_gate(manifest, staged)

    update_plugin(target, surface_gate=gate)


def _write_state(path: Path, state: dict) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False, encoding="utf-8") as file:
        draft = Path(file.name)
        try:
            json.dump(state, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
            draft.replace(path)
        finally:
            draft.unlink(missing_ok=True)


def _rollback(target: Path, home: Path, state: dict, state_path: Path) -> None:
    from hermes_cli import plugins_cmd
    from hermes_cli.plugins_transaction import publish_plugin

    pending = state["pending"]
    backup = Path(pending["backup"])
    if backup.parent != state_path.parent or not (backup / "checkout").is_dir():
        raise RuntimeError("update recovery backup is unavailable")
    metadata = plugins_cmd._read_install_metadata()
    old_record = json.loads((backup / "install-record.json").read_text(encoding="utf-8"))
    if not target.exists() or _head(target) != pending["previous"] or metadata.get(PLUGIN) != old_record:
        with tempfile.TemporaryDirectory(dir=target.parent, prefix=".rollback-") as temp:
            restored = Path(temp) / "checkout"
            shutil.copytree(backup / "checkout", restored, symlinks=True)
            publish_plugin(restored, target, metadata, {**metadata, PLUGIN: old_record})
    _reload(home)
    if _head(target) != pending["previous"]:
        raise RuntimeError("rollback checkout verification failed")
    state.pop("pending")
    state["failed"] = pending["revision"]
    _write_state(state_path, state)
    print(f"Activation failed for {pending['revision']}. Restored {pending['previous']}.", file=sys.stderr)


def update(home: Path, *, bootstrap: bool = False, dry_run: bool = False) -> str:
    from hermes_cli import plugins_cmd
    from hermes_cli.plugins_auto_update import plugin_update_lock

    target = home / "plugins" / PLUGIN
    task_dir = home / "runtime" / "cli-tasks"
    state_path = home / "runtime" / "cli-router-updater" / "status.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    revision = _remote_head() if "pending" not in state else state["pending"]["revision"]
    if "pending" not in state:
        if revision == _head(target) == state.get("activated") and not bootstrap:
            _verify_checkout(target, revision)
            return f"Already active at {revision}."
        if revision == state.get("failed"):
            return f"Holding failed revision {revision}. Advance main or clear the failure in {state_path}."
        if not _tests_passed(revision):
            return f"Waiting for the Tests workflow on main at {revision}."
    if dry_run:
        if _busy(task_dir):
            return "Deferred while cli-router tasks are active."
        return f"Would {'recover' if 'pending' in state else 'deploy'} {revision}."

    task_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (task_dir / SUBMISSION_LOCK).open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "Deferred while cli-router is accepting a task or updating."
        if _busy(task_dir):
            return "Deferred while cli-router tasks are active."
        with plugin_update_lock(target):
            # The lock is also shared by manual invocations of this script.
            current_state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            if state != current_state:
                return "Deployment status changed while checking. Will check again on the next run."
            if "pending" in state:
                _rollback(target, home, state, state_path)
                return f"Recovered the interrupted update for {revision}."

            previous = _head(target)
            old_record = plugins_cmd._read_install_metadata()[PLUGIN]
            if not bootstrap and (old_record.get("pinned") is True or old_record.get("revision") != previous):
                raise RuntimeError("installed revision disagrees with tracking metadata; bootstrap is required")
            backup = Path(tempfile.mkdtemp(prefix="bootstrap-" if bootstrap else "backup-", dir=state_path.parent))
            shutil.copytree(target, backup / "checkout", symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
            (backup / "install-record.json").write_text(json.dumps(old_record, indent=2), encoding="utf-8")
            state["pending"] = {"revision": revision, "previous": previous, "backup": str(backup)}
            _write_state(state_path, state)
            try:
                _publish(target, revision, bootstrap=bootstrap)
            except Exception:
                # The Hermes transaction can refuse the candidate before touching the live tree.
                if (target.exists() and _head(target) == previous
                        and plugins_cmd._read_install_metadata().get(PLUGIN) == old_record):
                    state.pop("pending")
                    _write_state(state_path, state)
                    if not bootstrap:
                        shutil.rmtree(backup)
                else:
                    _rollback(target, home, state, state_path)
                raise
            try:
                _verify_checkout(target, revision)
                record = plugins_cmd._read_install_metadata()[PLUGIN]
                if record.get("revision") != revision or record.get("pinned") is True:
                    raise RuntimeError("Hermes did not record the verified main revision as tracking")
                _reload(home)
                _verify_checkout(target, revision)
            except Exception:
                _rollback(target, home, state, state_path)
                raise
            _write_state(state_path, {"activated": revision})
            if not bootstrap:
                shutil.rmtree(backup)
            return f"Activated {revision} in Hermes Code."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap", action="store_true", help="Replace the pinned install once, with a retained backup.")
    parser.add_argument("--dry-run", action="store_true", help="Check the candidate and workload without deploying.")
    parser.add_argument("--hermes-root", type=Path, default=Path.home() / ".hermes")
    args = parser.parse_args()
    root = args.hermes_root.resolve()
    home = root / "profiles" / "code"
    try:
        if not getattr(sys, "_cli_router_hermes_runtime", False):
            launcher = root / "hermes-agent" / ".hermes" / "bin" / "hermes"
            command = subprocess.check_output([str(launcher), "--print-runtime-command"], text=True, timeout=30)
            python = json.loads(command)[0]
            init = ("import runpy, sys; "
                    "sys.path.insert(0, sys.argv.pop(1)); "
                    "sys._hermes_pin_default_home = True; "
                    "sys._cli_router_hermes_runtime = True; "
                    "import hermes_bootstrap; "
                    "runpy.run_path(sys.argv.pop(1), run_name='__main__')")
            env = {key: value for key, value in os.environ.items()
                   if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}}
            env["HERMES_HOME"] = str(home)
            os.execve(python, [python, "-I", "-c", init, str(root / "hermes-agent"), str(SCRIPT), *sys.argv[1:]], env)
        print(update(home, bootstrap=args.bootstrap, dry_run=args.dry_run))
    except Exception as exc:
        print(f"cli-router update failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
