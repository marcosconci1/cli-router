import sys
import tempfile
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


def test_queue_is_cancellable_and_never_starts_when_full():
    import worker_resources as resources
    with tempfile.TemporaryDirectory() as temp:
        lock = Path(temp) / "slot.lock"
        with patch.object(resources, "memory_block_reason", return_value=None):
            first = resources.acquire(lambda: False, lambda msg: None, lock_path=lock)
            try:
                with pytest.raises(resources.ResourceBlocked, match="queue expired"):
                    resources.acquire(lambda: False, lambda msg: None, lock_path=lock, timeout=0)
                with pytest.raises(resources.ResourceBlocked) as error:
                    resources.acquire(lambda: True, lambda msg: None, lock_path=lock)
                assert error.value.cancelled
            finally:
                first.close()


def test_telemetry_fails_closed():
    import worker_resources as resources
    with patch.object(Path, "read_text", side_effect=OSError("not readable")):
        assert resources.memory_block_reason() == "host memory telemetry is unavailable"


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
    lease = MagicMock(fd=71)
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
    assert start.call_args.kwargs["pass_fds"] == (72, 71)
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
        assert waits == ["low RAM"]
        assert lease.fd >= 0
        lease.close()
        with patch.object(resources, "memory_block_reason", return_value=None):
            second = resources.acquire(lambda: False, waits.append, lock_path=lock, timeout=0)
            second.close()
