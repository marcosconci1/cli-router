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


class ResourceBlocked(RuntimeError):
    def __init__(self, message: str, cancelled: bool = False):
        super().__init__(message)
        self.cancelled = cancelled


class Lease:
    def __init__(self, fd: int):
        self.fd = fd

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


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


def acquire(cancelled: Callable[[], bool], on_wait: Callable[[str], None], *,
            lock_path: Path | None = None, timeout: float = QUEUE_TIMEOUT_SECONDS) -> Lease:
    # A single lock shared by all Code gateway/CLI processes, not one per repository.
    if lock_path is None:
        directory = Path.home() / ".cache" / "hermes-cli-workers"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = directory / "slot.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + timeout
    last_reason = None
    try:
        while True:
            if cancelled():
                raise ResourceBlocked("cancelled while waiting for worker resources", cancelled=True)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                reason = memory_block_reason()
                if reason is None:
                    return Lease(fd)
                fcntl.flock(fd, fcntl.LOCK_UN)
            except BlockingIOError:
                reason = "another protected CLI worker is running"
            if reason != last_reason:
                on_wait(reason)
                last_reason = reason
            if time.monotonic() >= deadline:
                raise ResourceBlocked("worker resource queue expired: " + reason)
            time.sleep(1)
    except BaseException:
        os.close(fd)
        raise
