from __future__ import annotations

import fcntl
import os
from pathlib import Path
from urllib.parse import unquote


class SQLiteWorkerGuard:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self._handle = None

    @staticmethod
    def configured_worker_count() -> int:
        values = [os.getenv("WEB_CONCURRENCY"), os.getenv("UVICORN_WORKERS")]
        counts = []
        for value in values:
            if value:
                try:
                    counts.append(int(value))
                except ValueError as exc:
                    raise RuntimeError(f"invalid worker count: {value}") from exc
        return max(counts, default=1)

    def acquire(self) -> None:
        if not self.database_url.startswith("sqlite"):
            return
        if self.configured_worker_count() != 1:
            raise RuntimeError("SQLite financial core requires exactly one backend worker")
        marker = "///"
        if marker not in self.database_url or ":memory:" in self.database_url:
            return
        db_path = Path(unquote(self.database_url.split(marker, 1)[1])).resolve()
        lock_path = db_path.with_suffix(db_path.suffix + ".backend.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise RuntimeError(f"another SQLite backend already owns {lock_path}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None
