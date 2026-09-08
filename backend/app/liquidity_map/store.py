"""Ten-minute disposable observation archive, entirely separate from accounting.

Atomic gzip segments, expiry on start/read/write, global byte/file caps. Only
our own flat, hashed-symbol filenames can be removed. No WAL, VACUUM or DB.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time

RETENTION_MS = 600_000
MAX_BYTES = 128 * 1024 * 1024
MAX_FILES = 40_000
MAX_SEGMENT_BYTES = 512 * 1024
MAX_RAW_BYTES = 4 * 1024 * 1024
SEGMENT = re.compile(r"^([0-9a-f]{16})-(\d{13})\.json\.gz$")


def symbol_key(symbol: str) -> str:
    return hashlib.sha256(symbol.encode()).hexdigest()[:16]


class ObservationStore:
    def __init__(self, root: Path, *, max_bytes: int = MAX_BYTES, max_files: int = MAX_FILES):
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.max_files = max_files
        self.lock = threading.RLock()
        self.files: dict[str, tuple[int, int]] = {}
        self.bytes = 0
        self.expired = 0
        self.capacity_evicted = 0
        self.dropped = 0
        self.errors = 0
        self.last_error = None
        self.root.mkdir(parents=True, exist_ok=True)
        for path in self.root.iterdir():
            if path.name.endswith(".tmp") and SEGMENT.fullmatch(path.name[:-4]) and not path.is_symlink():
                path.unlink(missing_ok=True)
                continue
            match = SEGMENT.fullmatch(path.name)
            if match and path.is_file() and not path.is_symlink():
                stat = path.stat()
                self.files[path.name] = (int(match[2]), max(stat.st_size, stat.st_blocks*512))
        self.bytes = sum(size for _, size in self.files.values())
        self.prune(int(time.time() * 1000))

    def _remove(self, name: str, *, capacity: bool = False):
        _, size = self.files.pop(name)
        (self.root / name).unlink(missing_ok=True)
        self.bytes -= size
        if capacity:
            self.capacity_evicted += 1
        else:
            self.expired += 1

    def prune(self, now_ms: int):
        with self.lock:
            for name, (timestamp, _) in list(self.files.items()):
                if timestamp < now_ms - RETENTION_MS or timestamp > now_ms + 60_000:
                    self._remove(name)
            while self.files and (self.bytes > self.max_bytes or len(self.files) > self.max_files):
                self._remove(min(self.files, key=lambda n: self.files[n][0]), capacity=True)

    def write(self, frame: dict, now_ms: int) -> bool:
        with self.lock:
            self.prune(now_ms)
            if int(frame['t']) < now_ms - RETENTION_MS:
                return False
            raw = json.dumps(frame, separators=(',', ':'), ensure_ascii=False).encode()
            if len(raw) > MAX_RAW_BYTES:
                self.dropped += 1
                return False
            data = gzip.compress(raw, compresslevel=1, mtime=0)
            if len(data) > min(MAX_SEGMENT_BYTES, self.max_bytes):
                self.dropped += 1
                return False
            charge = ((len(data)+4095)//4096)*4096
            if charge > self.max_bytes:
                self.dropped += 1
                return False
            name = f"{symbol_key(frame['symbol'])}-{int(frame['t']):013d}.json.gz"
            old_size = self.files.get(name, (0, 0))[1]
            # Reserve temp-file bytes too: a write never briefly exceeds the cap.
            while self.files and (self.bytes + charge > self.max_bytes or
                                  (name not in self.files and len(self.files) >= self.max_files)):
                oldest = min(self.files, key=lambda n: self.files[n][0])
                self._remove(oldest, capacity=True)
            old_size = self.files.get(name, (0, 0))[1]
            temp = self.root / (name + '.tmp')
            try:
                with temp.open('wb') as handle:
                    handle.write(data)
                os.replace(temp, self.root / name)
            finally:
                temp.unlink(missing_ok=True)
            self.files[name] = (int(frame['t']), charge)
            self.bytes += charge - old_size
            return True

    def read(self, symbol: str, start: int, end: int, now_ms: int) -> list[dict]:
        with self.lock:
            self.prune(now_ms)
            prefix = symbol_key(symbol) + '-'
            names = sorted(name for name, (stamp, _) in self.files.items()
                           if name.startswith(prefix) and max(start, now_ms-RETENTION_MS) <= stamp < end)
            result = []
            total_raw = 0
            for name in names[-601:]:
                try:
                    with gzip.open(self.root / name, 'rb') as handle:
                        raw = handle.read(MAX_RAW_BYTES + 1)
                    if len(raw) > MAX_RAW_BYTES:
                        raise ValueError('oversized observation segment')
                    total_raw += len(raw)
                    if total_raw > 32 * 1024 * 1024:
                        raise RuntimeError("observation query exceeds 32MiB read budget")
                    item = json.loads(raw)
                    if item.get('symbol') == symbol:
                        result.append(item)
                except (OSError, ValueError) as exc:
                    self.errors += 1
                    self.last_error = str(exc)[:160]
            return result

    def status(self) -> dict:
        with self.lock:
            return dict(retentionSeconds=600, maxBytes=self.max_bytes, bytes=self.bytes,
                        files=len(self.files), expired=self.expired,
                        capacityEvicted=self.capacity_evicted, dropped=self.dropped,
                        errors=self.errors, lastError=self.last_error)
