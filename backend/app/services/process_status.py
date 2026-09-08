from __future__ import annotations

from dataclasses import dataclass
import os
import subprocess
import threading
import time


PROCESS_STATUS_CACHE_TTL_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class ProcessStatus:
    pid: int
    running: bool
    command: str | None
    state: str | None
    checked_at: float


_cache: dict[int, ProcessStatus] = {}
_cache_lock = threading.Lock()


def invalidate_process_status(pid: int | None = None) -> None:
    with _cache_lock:
        if pid is None:
            _cache.clear()
        else:
            _cache.pop(int(pid), None)


def inspect_process(
    pid: int | None,
    *,
    force: bool = False,
    ttl_seconds: float = PROCESS_STATUS_CACHE_TTL_SECONDS,
) -> ProcessStatus:
    normalized_pid = int(pid or 0)
    now = time.monotonic()
    if normalized_pid <= 0:
        return ProcessStatus(normalized_pid, False, None, None, now)
    if not force:
        with _cache_lock:
            cached = _cache.get(normalized_pid)
        if cached is not None and now - cached.checked_at < max(0.0, ttl_seconds):
            return cached

    try:
        os.kill(normalized_pid, 0)
    except OSError:
        result = ProcessStatus(normalized_pid, False, None, None, now)
    else:
        state: str | None = None
        command: str | None = None
        try:
            completed = subprocess.run(
                ["ps", "-p", str(normalized_pid), "-o", "stat=", "-o", "command="],
                check=False,
                capture_output=True,
                text=True,
                timeout=1,
            )
            line = completed.stdout.strip()
            if completed.returncode == 0 and line:
                parts = line.split(maxsplit=1)
                state = parts[0]
                command = parts[1] if len(parts) > 1 else None
        except Exception:
            # os.kill(pid, 0) still provides a useful liveness signal if ps is
            # temporarily unavailable. Identity validation remains available
            # as soon as the cached snapshot can include a command line.
            pass
        result = ProcessStatus(
            normalized_pid,
            not bool(state and "Z" in state.upper()),
            command,
            state,
            now,
        )
    with _cache_lock:
        _cache[normalized_pid] = result
    return result


def pid_is_running(pid: int | None, *, force: bool = False) -> bool:
    return inspect_process(pid, force=force).running


def process_command_line(pid: int | None, *, force: bool = False) -> str | None:
    status = inspect_process(pid, force=force)
    return status.command if status.running else None
