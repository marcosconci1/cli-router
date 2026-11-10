"""Host-wide admission for Code CLI workers; no provider credentials are persisted."""
from __future__ import annotations

import fcntl
import os
import re
import shutil
import time
from pathlib import Path
from typing import Callable

MIN_AVAILABLE_BYTES = 2 * 1024**3
MAX_FULL_PRESSURE_AVG10 = 5.0
QUEUE_TIMEOUT_SECONDS = 300
DEFAULT_MAX_WORKERS = 2
MAX_WORKERS_LIMIT = 8


class ResourceBlocked(RuntimeError):
    def __init__(self, message: str, cancelled: bool = False):
        super().__init__(message)
        self.cancelled = cancelled


class Lease:
    def __init__(self, fd: int, legacy_fd: int):
        self.fd = fd
        self.legacy_fd = legacy_fd

    @property
    def fds(self) -> tuple[int, ...]:
        return tuple(fd for fd in (self.fd, self.legacy_fd) if fd >= 0)

    def close(self) -> None:
        for name in ("fd", "legacy_fd"):
            fd = getattr(self, name)
            if fd >= 0:
                os.close(fd)
                setattr(self, name, -1)


def scope_command(command: list[str], task_id: str, attempt: int) -> list[str]:
    executable = shutil.which("systemd-run")
    if not executable:
        raise ResourceBlocked("systemd-run is unavailable; refusing an unprotected worker")
    if not re.fullmatch(r"[0-9a-f]{32}", task_id) or attempt < 1:
        raise ResourceBlocked("invalid worker resource identity")
    return [executable, "--user", "--scope", "--quiet", "--collect", "--expand-environment=no",
            f"--unit=hermes-cli-{task_id}-{attempt}", "--slice=hermes-workers.slice",
            "--property=MemoryHigh=1280M", "--property=MemoryMax=1536M",
            "--property=MemorySwapMax=256M", "--property=TasksMax=256",
            "--property=OOMPolicy=kill", "--property=RuntimeMaxSec=1800", "--", *command]


def memory_block_reason() -> str | None:
    try:
        fields = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        available = int(fields["MemAvailable"].split()[0]) * 1024
        full = next(line for line in Path("/proc/pressure/memory").read_text().splitlines()
                    if line.startswith("full "))
        pressure = float(dict(word.split("=", 1) for word in full.split()[1:])["avg10"])
        if available < MIN_AVAILABLE_BYTES:
            return "less than 2 GiB available RAM"
        if pressure >= MAX_FULL_PRESSURE_AVG10:
            return "memory stalls exceed the safe launch threshold"
    except (OSError, ValueError, KeyError, StopIteration):
        return "host memory telemetry is unavailable"
    return None


def _open_lock(path: Path) -> int:
    return os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)


def _try_lock(fd: int, operation: int) -> bool:
    try:
        fcntl.flock(fd, operation | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _active_slot_count(directory: Path) -> int:
    active = 0
    for index in range(MAX_WORKERS_LIMIT):
        fd = _open_lock(directory / f"worker-{index}.lock")
        try:
            if not _try_lock(fd, fcntl.LOCK_EX):
                active += 1
        finally:
            os.close(fd)
    return active


def acquire(cancelled: Callable[[], bool], on_wait: Callable[[str], None], *,
            max_workers: int = DEFAULT_MAX_WORKERS, lock_path: Path | None = None,
            timeout: float = QUEUE_TIMEOUT_SECONDS) -> Lease:
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not (
            1 <= max_workers <= MAX_WORKERS_LIMIT):
        raise ResourceBlocked(f"max-workers must be an integer from 1 to {MAX_WORKERS_LIMIT}")
    # lock_path is the old exclusive slot lock. New workers hold it shared for their
    # lifetime, which both permits concurrency and prevents an older exclusive-lock
    # worker from being silently bypassed during a rolling upgrade.
    if lock_path is None:
        directory = Path.home() / ".cache" / "hermes-cli-workers"
        lock_path = directory / "slot.lock"
    directory = lock_path.parent
    deadline = time.monotonic() + timeout
    last_reason = None
    legacy_fd = -1
    slot_fd = -1
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        legacy_fd = _open_lock(lock_path)
        while True:
            if cancelled():
                raise ResourceBlocked("cancelled while waiting for worker resources", cancelled=True)
            if not _try_lock(legacy_fd, fcntl.LOCK_SH):
                reason = "legacy worker holds the shared CLI worker lock"
            else:
                gate_fd = _open_lock(directory / "coordinator.lock")
                try:
                    fcntl.flock(gate_fd, fcntl.LOCK_EX)
                    config_path = directory / "capacity"
                    try:
                        configured = int(config_path.read_text().strip())
                    except FileNotFoundError:
                        configured = None
                    except (OSError, ValueError):
                        configured = -1
                    active = _active_slot_count(directory)
                    if configured not in (None, max_workers):
                        if configured < 1 or configured > MAX_WORKERS_LIMIT:
                            reason = "worker capacity configuration is invalid; refusing admission"
                        elif active:
                            reason = "worker capacity setting differs while workers are active"
                        else:
                            config_path.write_text(f"{max_workers}\n")
                            configured = max_workers
                    elif configured is None:
                        config_path.write_text(f"{max_workers}\n")
                        configured = max_workers
                    if configured == max_workers:
                        slot_fd = -1
                        for index in range(max_workers):
                            candidate_fd = _open_lock(directory / f"worker-{index}.lock")
                            if _try_lock(candidate_fd, fcntl.LOCK_EX):
                                slot_fd = candidate_fd
                                break
                            os.close(candidate_fd)
                        if slot_fd >= 0:
                            reason = memory_block_reason()
                            if reason is None:
                                return Lease(slot_fd, legacy_fd)
                            os.close(slot_fd)
                            slot_fd = -1
                            reason = "memory pressure: " + reason
                        else:
                            reason = "worker capacity reached (max-workers={})".format(max_workers)
                finally:
                    fcntl.flock(gate_fd, fcntl.LOCK_UN)
                    os.close(gate_fd)
                fcntl.flock(legacy_fd, fcntl.LOCK_UN)
            if reason != last_reason:
                on_wait(reason)
                last_reason = reason
            if time.monotonic() >= deadline:
                raise ResourceBlocked("worker resource queue expired: " + reason)
            time.sleep(1)
    except OSError as exc:
        if slot_fd >= 0:
            os.close(slot_fd)
        if legacy_fd >= 0:
            os.close(legacy_fd)
        raise ResourceBlocked("worker admission storage is unavailable") from exc
    except BaseException:
        if slot_fd >= 0:
            os.close(slot_fd)
        if legacy_fd >= 0:
            os.close(legacy_fd)
        raise
