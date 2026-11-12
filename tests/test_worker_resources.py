import sys
import tempfile
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_scope_command_enforces_limits_without_expanding_prompts():
    import worker_resources as resources
    command = resources.scope_command(["/bin/echo", "$HOME", "--"], "a" * 32, 2)
    assert "--scope" in command
    assert "--expand-environment=no" in command
    assert "--property=MemoryMax=1536M" in command
    assert "--property=MemoryHigh=1280M" in command
    assert "--property=MemorySwapMax=256M" in command
    assert "--property=OOMPolicy=kill" in command
    assert "--property=RuntimeMaxSec=1800" in command
    assert "--slice=hermes-workers.slice" in command
    assert command[-3:] == ["/bin/echo", "$HOME", "--"]


def test_different_repositories_run_concurrently_and_third_hits_default_capacity():
    import worker_resources as resources
    with tempfile.TemporaryDirectory() as temp:
        lock = Path(temp) / "slot.lock"
        with patch.object(resources, "memory_block_reason", return_value=None):
            first = resources.acquire(lambda: False, lambda msg: None, lock_path=lock)
            second = resources.acquire(lambda: False, lambda msg: None, lock_path=lock)
            try:
                with pytest.raises(resources.ResourceBlocked, match="capacity reached"):
                    resources.acquire(lambda: False, lambda msg: None, lock_path=lock, timeout=0)
                with pytest.raises(resources.ResourceBlocked) as error:
                    resources.acquire(lambda: True, lambda msg: None, lock_path=lock)
                assert error.value.cancelled
            finally:
                second.close()
                first.close()


def test_telemetry_fails_closed():
    import worker_resources as resources
    with patch.object(Path, "read_text", side_effect=OSError("not readable")):
        assert resources.memory_block_reason() == "host memory telemetry is unavailable"


def test_unavailable_telemetry_denies_admission_with_memory_reason():
    import worker_resources as resources
    with tempfile.TemporaryDirectory() as temp, \
         patch.object(resources, "memory_block_reason", return_value="host memory telemetry is unavailable"):
        with pytest.raises(resources.ResourceBlocked, match="memory pressure: host memory telemetry is unavailable"):
            resources.acquire(lambda: False, lambda _: None, lock_path=Path(temp) / "slot.lock", timeout=0)


def test_worker_cannot_launch_when_resource_guard_blocks():
    import task_manager as tm
    import worker_resources as resources
    with tempfile.TemporaryDirectory() as temp:
        with patch.dict("os.environ", {"AI_MEMORY_AUTH_TOKEN": "mock-token"}), \
             patch.object(tm.ai_memory_bridge, "_parent_env", return_value={"AI_MEMORY_AUTH_TOKEN": "mock-token"}), \
             patch.object(resources, "acquire", side_effect=resources.ResourceBlocked("low RAM")), \
             patch.object(tm.ai_memory_bridge, "verify_runtime"), \
             patch.object(tm, "_installed_cli", return_value="/usr/bin/true"), \
             patch.object(tm.subprocess, "Popen") as start:
            result = tm._run_candidate({"cli": "codex", "model": "test"}, "task", Path(temp), "change", "a" * 32, [])
    assert result["outcome"] == "resource_blocked"
    start.assert_not_called()


def test_worker_launch_holds_slot_and_inherits_repository_lock():
    import task_manager as tm
    import worker_resources as resources
    from unittest.mock import MagicMock
    lease = MagicMock(fd=71, fds=(71, 73))
    process = MagicMock(returncode=0)
    process.communicate.return_value = ("done", "")
    with tempfile.TemporaryDirectory() as temp:
        with patch.dict("os.environ", {"AI_MEMORY_AUTH_TOKEN": "mock-token"}), \
             patch.object(tm.ai_memory_bridge, "_parent_env", return_value={"AI_MEMORY_AUTH_TOKEN": "mock-token"}), \
             patch.object(resources, "acquire", return_value=lease), \
             patch.object(tm.ai_memory_bridge, "verify_runtime"), \
             patch.object(tm.ai_memory_bridge, "find_workstream", return_value="a" * 36), \
             patch.object(tm.ai_memory_bridge, "verify_attempt", return_value=1), \
             patch.object(tm, "_installed_cli", return_value="/usr/bin/true"), \
             patch.dict(tm._LOCK_FDS, {"a" * 32: 72}), \
             patch.object(tm.subprocess, "Popen", return_value=process) as start:
            result = tm._run_candidate({"cli": "codex", "model": "test"}, "task", Path(temp), "change", "a" * 32, [])
    assert result["outcome"] == "needs_verification"
    assert "--scope" in start.call_args.args[0]
    assert start.call_args.kwargs["pass_fds"] == (72, 71, 73)
    lease.close.assert_called_once()


def test_worker_launch_failure_releases_lease():
    import task_manager as tm
    import worker_resources as resources
    from unittest.mock import MagicMock
    lease = MagicMock(fd=71, fds=(71, 73))
    with tempfile.TemporaryDirectory() as temp, \
         patch.dict("os.environ", {"AI_MEMORY_AUTH_TOKEN": "mock-token"}), \
         patch.object(tm.ai_memory_bridge, "_parent_env", return_value={"AI_MEMORY_AUTH_TOKEN": "mock-token"}), \
         patch.object(resources, "acquire", return_value=lease), \
         patch.object(tm.ai_memory_bridge, "verify_runtime"), \
         patch.object(tm.ai_memory_bridge, "find_workstream", return_value="a" * 36), \
         patch.object(tm.ai_memory_bridge, "verify_attempt", return_value=1), \
         patch.object(tm, "_installed_cli", return_value="/usr/bin/true"), \
         patch.object(tm.subprocess, "Popen", side_effect=OSError("synthetic start failure")):
        result = tm._run_candidate({"cli": "codex", "model": "test"}, "task", Path(temp),
                                   "change", "a" * 32, [])
    assert result["outcome"] == "resource_blocked"
    lease.close.assert_called_once()


@pytest.mark.parametrize("available_kib,pressure,blocked", [
    (2097151, "0.00", True), (2097152, "0.00", False),
    (3145728, "5.00", True), (3145728, "4.99", False),
])
def test_memory_admission_thresholds(available_kib, pressure, blocked):
    import worker_resources as resources
    with patch.object(Path, "read_text", side_effect=[
        f"MemAvailable: {available_kib} kB\n", f"full avg10={pressure} avg60=0.00 avg300=0.00 total=0\n",
    ]):
        assert (resources.memory_block_reason() is not None) is blocked


def test_scope_unavailable_never_falls_back_to_native_process():
    import worker_resources as resources
    with patch.object(resources.shutil, "which", return_value=None):
        with pytest.raises(resources.ResourceBlocked, match="refusing an unprotected"):
            resources.scope_command(["/bin/true"], "f" * 32, 1)


def test_admission_waits_for_memory_and_releases_global_slot():
    import worker_resources as resources
    with tempfile.TemporaryDirectory() as temp:
        lock = Path(temp) / "slot.lock"
        waits = []
        with patch.object(resources, "memory_block_reason", side_effect=["low RAM", None]), \
             patch.object(resources.time, "sleep"):
            lease = resources.acquire(lambda: False, waits.append, lock_path=lock)
        assert waits == ["memory pressure: low RAM"]
        assert lease.fd >= 0
        lease.close()
        with patch.object(resources, "memory_block_reason", return_value=None):
            second = resources.acquire(lambda: False, waits.append, lock_path=lock, timeout=0)
            second.close()


def test_legacy_exclusive_holder_blocks_new_protocol():
    import fcntl
    import os
    import worker_resources as resources
    with tempfile.TemporaryDirectory() as temp:
        lock = Path(temp) / "slot.lock"
        legacy_fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(legacy_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with patch.object(resources, "memory_block_reason", return_value=None), \
                 pytest.raises(resources.ResourceBlocked, match="legacy worker holds"):
                resources.acquire(lambda: False, lambda _: None, lock_path=lock, timeout=0)
        finally:
            os.close(legacy_fd)


def test_capacity_setting_change_waits_until_existing_group_drains():
    import worker_resources as resources
    with tempfile.TemporaryDirectory() as temp, \
         patch.object(resources, "memory_block_reason", return_value=None):
        lock = Path(temp) / "slot.lock"
        current = resources.acquire(lambda: False, lambda _: None, lock_path=lock, max_workers=2)
        with pytest.raises(resources.ResourceBlocked, match="setting differs"):
            resources.acquire(lambda: False, lambda _: None, lock_path=lock,
                              max_workers=3, timeout=0)
        current.close()
        changed = resources.acquire(lambda: False, lambda _: None, lock_path=lock,
                                    max_workers=3, timeout=0)
        changed.close()
        assert (Path(temp) / "capacity").read_text() == "3\n"


def test_capacity_setting_is_bounded_positive_integer():
    import worker_resources as resources
    with tempfile.TemporaryDirectory() as temp:
        lock = Path(temp) / "slot.lock"
        for value in (True, 0, -1, 1.5, "2", resources.MAX_WORKERS_LIMIT + 1):
            with pytest.raises(resources.ResourceBlocked, match="max-workers"):
                resources.acquire(lambda: False, lambda _: None, lock_path=lock,
                                  max_workers=value, timeout=0)


def test_unavailable_lock_storage_fails_closed_as_resource_block():
    import worker_resources as resources
    with tempfile.TemporaryDirectory() as temp, \
         patch.object(resources, "_open_lock", side_effect=OSError("read-only")), \
         pytest.raises(resources.ResourceBlocked, match="admission storage is unavailable"):
        resources.acquire(lambda: False, lambda _: None,
                          lock_path=Path(temp) / "slot.lock", timeout=0)


def test_process_crash_releases_capacity_lease():
    import worker_resources as resources
    with tempfile.TemporaryDirectory() as temp:
        lock = Path(temp) / "slot.lock"
        script = textwrap.dedent("""\
            import sys, time
            from pathlib import Path
            sys.path.insert(0, sys.argv[1])
            import worker_resources as r
            r.memory_block_reason = lambda: None
            lease = r.acquire(lambda: False, lambda _: None, lock_path=Path(sys.argv[2]), timeout=0)
            print('admitted', flush=True)
            time.sleep(30)
        """)
        child = subprocess.Popen([sys.executable, "-c", script, str(Path(__file__).resolve().parents[1]), str(lock)],
                                 stdout=subprocess.PIPE, text=True)
        assert child.stdout.readline().strip() == "admitted"
        child.kill()
        child.wait(timeout=5)
        with patch.object(resources, "memory_block_reason", return_value=None):
            lease = resources.acquire(lambda: False, lambda _: None, lock_path=lock, timeout=0)
            lease.close()


def test_subprocess_race_never_admits_more_than_default_capacity():
    import worker_resources as resources
    with tempfile.TemporaryDirectory() as temp:
        lock = Path(temp) / "slot.lock"
        script = textwrap.dedent("""\
            import sys, time
            from pathlib import Path
            sys.path.insert(0, sys.argv[1])
            import worker_resources as r
            r.memory_block_reason = lambda: None
            try:
                lease = r.acquire(lambda: False, lambda _: None, lock_path=Path(sys.argv[2]), timeout=0)
            except r.ResourceBlocked as exc:
                print('denied:' + str(exc), flush=True)
                raise SystemExit(0)
            print('admitted', flush=True)
            time.sleep(.3)
            lease.close()
        """)
        children = [subprocess.Popen([sys.executable, "-c", script,
                     str(Path(__file__).resolve().parents[1]), str(lock)],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
        outputs = [child.communicate(timeout=10)[0].strip() for child in children]
        assert sum(line == "admitted" for line in outputs) == resources.DEFAULT_MAX_WORKERS
        assert sum("capacity reached" in line for line in outputs) == 4 - resources.DEFAULT_MAX_WORKERS


def test_configured_worker_limit_reads_direct_and_nested_settings():
    import hermes_adapter
    import task_manager as tm
    configs = ({"max-workers": 3}, {"cli-router": {"settings": {"max-workers": 3}}})
    for values in configs:
        with patch.object(hermes_adapter, "has_context", return_value=True), \
             patch.object(hermes_adapter, "get_config", side_effect=lambda name, default=None:
                          values.get(name, default)):
            assert tm._max_workers() == 3
    with patch.object(hermes_adapter, "has_context", return_value=False):
        assert tm._max_workers() == 2


def test_configured_worker_limit_prefers_plugin_relative_then_legacy():
    import hermes_adapter
    import task_manager as tm

    class Context:
        def __init__(self, values):
            self.values = values
            self.calls = []

        def get_config(self, name, default=None):
            self.calls.append(name)
            return self.values.get(name, default)

    ctx = Context({"max-workers": 5, "cli-router": {"max-workers": 3}})
    with patch.object(tm, "_load_modular_config", return_value={}), \
         patch.object(hermes_adapter, "has_context", return_value=True), \
         patch.object(hermes_adapter, "get_config", side_effect=ctx.get_config):
        assert tm._max_workers() == 5
    assert ctx.calls == ["max-workers"]

    ctx = Context({"cli-router": {"settings": {"max-workers": 3}}})
    with patch.object(tm, "_load_modular_config", return_value={}), \
         patch.object(hermes_adapter, "has_context", return_value=True), \
         patch.object(hermes_adapter, "get_config", side_effect=ctx.get_config):
        assert tm._max_workers() == 3
    assert ctx.calls == ["max-workers", "cli-router"]


@pytest.mark.parametrize("value", [True, 0, 9, 2.5, "3"])
def test_configured_worker_limit_rejects_unsafe_hermes_values(value):
    import hermes_adapter
    import task_manager as tm
    import worker_resources as resources
    with patch.object(tm, "_load_modular_config", return_value={}), \
         patch.object(hermes_adapter, "has_context", return_value=True), \
         patch.object(hermes_adapter, "get_config", return_value=value):
        assert tm._max_workers() == resources.DEFAULT_MAX_WORKERS
