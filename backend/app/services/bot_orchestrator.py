from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import PRODUCT_TYPE_PERP
from app.core.time_utils import to_millis
from app.models.market import Market
from app.models.market_maker_instance import MarketMakerInstance
from app.models.user import User
from app.services.runtime import AppRuntime
from app.services.process_status import (
    pid_is_running as cached_pid_is_running,
    process_command_line as cached_process_command_line,
)


DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MAKER_RUNTIME_DIR = DEFAULT_PROJECT_ROOT / ".runtime"
MAKER_RUNTIME_DIR_ENV = "MM_RUNTIME_DIR"
MAKER_HEARTBEAT_STALE_SECONDS = 45
MAKER_LOG_ROTATE_BYTES = 10 * 1024 * 1024
MAKER_LOG_KEEP_ARCHIVES = 5


def maker_runtime_dir() -> Path:
    """Maker runtime dir per process identity.

    5174 sandbox makers live in ``<project>/.runtime``.  The 5198 PaperTrading
    exchange runs the same strategy processes for the same symbols, so its
    makers need a separate directory to avoid pid/lock collisions and
    cross-instance health confusion.  Paper mode uses
    ``<paper_data_dir>/.maker_runtime``; strategy children receive the same
    directory through ``MM_RUNTIME_DIR`` for their single-instance locks.
    """
    from app.core.config import settings
    from app.services.persistence_contract import platform_durable_contract

    if platform_durable_contract():
        data_dir = Path(settings.sandbox_data_dir)
        if not data_dir.is_absolute():
            data_dir = DEFAULT_PROJECT_ROOT / data_dir
        return data_dir / ".maker_runtime"
    return DEFAULT_MAKER_RUNTIME_DIR


def maker_instance_paths(symbol: str, runtime_dir: Path | None = None) -> dict[str, Path]:
    safe_symbol = "".join(ch for ch in symbol.upper() if ch.isalnum() or ch in {"_", "-"})
    directory = runtime_dir if runtime_dir is not None else maker_runtime_dir()
    return {
        "pid": directory / f"mm_service_{safe_symbol}.pid",
        "log": directory / f"mm_service_{safe_symbol}.log",
        "lock": directory / f"mm_service_{safe_symbol}.lock",
    }


def read_pid(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.isdigit():
        return None
    return int(text)


def pid_is_running(pid: int | None, *, force: bool = False) -> bool:
    return cached_pid_is_running(pid, force=force)


def process_command_line(pid: int | None, *, force: bool = False) -> str | None:
    return cached_process_command_line(pid, force=force)


def pid_matches_maker_instance(
    pid: int | None,
    symbol: str,
    *,
    project_root: Path = DEFAULT_PROJECT_ROOT,
) -> tuple[bool, str]:
    command = process_command_line(pid)
    if command is None:
        return True, "command_unavailable"
    expected_script = "contract_mm_service.py" if symbol.upper().endswith("-PERP") else "mm_service.py"
    expected_root = str(project_root)
    if expected_script not in command:
        return False, f"expected_script_missing:{expected_script}"
    if expected_root not in command:
        return False, "project_root_mismatch"
    if "--symbol" not in command or symbol.upper() not in command.upper():
        return False, "symbol_arg_mismatch"
    return True, "matched"


def wait_for_pid_exit(
    pid: int | None,
    *,
    timeout_seconds: float = 4.0,
    pid_checker: Callable[[int | None], bool] = pid_is_running,
) -> bool:
    def running() -> bool:
        if pid_checker is pid_is_running:
            return pid_is_running(pid, force=True)
        return pid_checker(pid)

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not running():
            return True
        time.sleep(0.1)
    return not running()


def tail_text(path: Path, *, max_lines: int = 24, max_bytes: int = 256 * 1024) -> list[str]:
    line_limit = max(1, int(max_lines))
    byte_limit = max(4096, int(max_bytes))
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - byte_limit)
            handle.seek(start)
            data = handle.read()
        lines = data.decode("utf-8", errors="replace").splitlines()
        if start > 0 and lines:
            lines = lines[1:]
        return lines[-line_limit:]
    except OSError:
        return []


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def tail_text_from_offset(
    path: Path,
    *,
    start_offset: int,
    max_lines: int = 24,
    max_bytes: int = 256 * 1024,
) -> list[str]:
    line_limit = max(1, int(max_lines))
    byte_limit = max(4096, int(max_bytes))
    try:
        size = path.stat().st_size
        offset = max(0, int(start_offset))
        if offset > size:
            return tail_text(path, max_lines=line_limit, max_bytes=byte_limit)
        start = max(offset, size - byte_limit)
        with path.open("rb") as handle:
            handle.seek(start)
            data = handle.read()
        lines = data.decode("utf-8", errors="replace").splitlines()
        if start > offset and lines:
            lines = lines[1:]
        return lines[-line_limit:]
    except (OSError, TypeError, ValueError):
        return []


def maker_instance_run_meta(instance: MarketMakerInstance | None) -> dict:
    metrics = instance.last_metrics_json if instance is not None and isinstance(instance.last_metrics_json, dict) else {}
    meta = metrics.get("_run") if isinstance(metrics.get("_run"), dict) else {}
    return meta if isinstance(meta, dict) else {}


def with_maker_instance_run_meta(metrics: dict | None, meta: dict | None) -> dict:
    merged = dict(metrics or {})
    if meta:
        merged["_run"] = dict(meta)
    return merged


def current_run_log_tail(
    path: Path,
    instance: MarketMakerInstance | None,
    *,
    max_lines: int = 24,
) -> tuple[list[str], dict]:
    meta = maker_instance_run_meta(instance)
    raw_offset = meta.get("log_start_offset")
    try:
        offset = int(raw_offset)
    except (TypeError, ValueError):
        offset = None
    if offset is not None and offset >= 0:
        size = file_size(path)
        if offset <= size:
            return tail_text_from_offset(path, start_offset=offset, max_lines=max_lines), {
                "scope": "current_run",
                "run_id": meta.get("run_id"),
                "log_start_offset": offset,
                "log_size": size,
                "log_started_at": meta.get("log_started_at"),
            }
    return tail_text(path, max_lines=max_lines), {
        "scope": "full_file",
        "run_id": meta.get("run_id"),
        "log_start_offset": offset,
        "log_size": file_size(path),
        "log_started_at": meta.get("log_started_at"),
    }


def heartbeat_status(value: datetime | None, *, now: datetime | None = None) -> str:
    if value is None:
        return "missing"
    current = now or datetime.now(tz=UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    if (current - value) > timedelta(seconds=MAKER_HEARTBEAT_STALE_SECONDS):
        return "stale"
    return "ok"


async def ensure_maker_instance_record(session: AsyncSession, market: Market) -> MarketMakerInstance:
    instance = await session.scalar(select(MarketMakerInstance).where(MarketMakerInstance.market_id == market.id))
    if instance is not None:
        if instance.symbol != market.symbol:
            instance.symbol = market.symbol
        return instance
    instance = MarketMakerInstance(market_id=market.id, symbol=market.symbol, status="stopped")
    session.add(instance)
    await session.flush()
    return instance


def maker_instance_strategy_key(market: Market, fallback: str | None = None) -> str | None:
    if market.product_type == PRODUCT_TYPE_PERP:
        return fallback or "PERP_MM"
    return fallback


class BotOrchestratorService:
    def __init__(
        self,
        runtime: AppRuntime,
        *,
        project_root: Path = DEFAULT_PROJECT_ROOT,
        runtime_dir: Path | None = None,
        popen_factory: Callable = subprocess.Popen,
        pid_checker: Callable[[int | None], bool] = pid_is_running,
        stop_process_func: Callable[[str], dict] | None = None,
    ) -> None:
        self.runtime = runtime
        self.project_root = project_root
        self.runtime_dir = runtime_dir if runtime_dir is not None else maker_runtime_dir()
        self.popen_factory = popen_factory
        self.pid_checker = pid_checker
        self.stop_process_func = stop_process_func

    def paths(self, symbol: str) -> dict[str, Path]:
        return maker_instance_paths(symbol, self.runtime_dir)

    def status(
        self,
        symbol: str,
        *,
        strategy_version: str | None = None,
        instance: MarketMakerInstance | None = None,
        start_readiness: dict | None = None,
    ) -> dict:
        paths = self.paths(symbol)
        pid = read_pid(paths["pid"])
        if pid is None and instance is not None:
            pid = instance.pid
        running = self.pid_checker(pid)
        pid_identity_ok = True
        pid_identity_reason = "not_running"
        if running:
            pid_identity_ok, pid_identity_reason = pid_matches_maker_instance(
                pid,
                symbol,
                project_root=self.project_root,
            )
            if not pid_identity_ok:
                running = False
        runtime_metrics = self.runtime.liquidity_metrics.get(symbol.upper(), {})
        persisted_metrics = instance.last_metrics_json if instance is not None and isinstance(instance.last_metrics_json, dict) else {}
        metrics = runtime_metrics or persisted_metrics
        now = datetime.now(tz=UTC)
        persisted_status = instance.status if instance is not None else "stopped"
        last_heartbeat_at = instance.last_heartbeat_at if instance is not None else None
        hb_status = heartbeat_status(last_heartbeat_at, now=now)
        started_at = instance.started_at if instance is not None else None
        starting_grace = (
            running
            and last_heartbeat_at is None
            and started_at is not None
            and ((now - (started_at.replace(tzinfo=UTC) if started_at.tzinfo is None else started_at)).total_seconds() <= MAKER_HEARTBEAT_STALE_SECONDS)
        )
        if running:
            if persisted_status == "starting" and starting_grace:
                status = "starting"
            elif persisted_status in {"switching", "rollback"} and hb_status != "stale":
                status = persisted_status
            elif hb_status == "stale" or (hb_status == "missing" and not starting_grace):
                status = "stale"
            else:
                status = "running"
        elif pid is not None:
            status = "stale"
        elif persisted_status in {"starting", "running", "switching", "rollback", "stopping"}:
            status = "stale"
        else:
            status = persisted_status or "stopped"
        last_metrics_ts = metrics.get("ts") if isinstance(metrics, dict) else None
        if last_metrics_ts is None and last_heartbeat_at is not None:
            last_metrics_ts = to_millis(last_heartbeat_at)
        log_path = paths["log"]
        log_lines, log_meta = current_run_log_tail(log_path, instance)
        return {
            "symbol": symbol.upper(),
            "status": status,
            "persisted_status": persisted_status,
            "heartbeat_status": hb_status,
            "pid": pid,
            "pid_identity": {"ok": pid_identity_ok, "reason": pid_identity_reason},
            "running": running,
            "strategy_version": strategy_version or (instance.strategy_key if instance is not None else None) or self.runtime.get_liquidity_strategy_selection(symbol.upper()),
            "metrics_present": bool(metrics),
            "last_metrics_ts": last_metrics_ts,
            "last_heartbeat_at": to_millis(last_heartbeat_at) if last_heartbeat_at is not None else None,
            "started_at": to_millis(instance.started_at) if instance is not None and instance.started_at is not None else None,
            "stopped_at": to_millis(instance.stopped_at) if instance is not None and instance.stopped_at is not None else None,
            "last_error": instance.last_error if instance is not None else None,
            "pid_file": str(paths["pid"]),
            "lock_file": str(paths["lock"]),
            "log_path": str(log_path),
            "log_exists": log_path.exists(),
            "last_log_lines": log_lines,
            "log_scope": log_meta["scope"],
            "run_id": log_meta.get("run_id"),
            "log_start_offset": log_meta.get("log_start_offset"),
            "log_size": log_meta.get("log_size"),
            "log_started_at": log_meta.get("log_started_at"),
            "start_readiness": start_readiness,
        }

    def logs(self, symbol: str, instance: MarketMakerInstance | None, *, max_lines: int) -> dict:
        paths = self.paths(symbol)
        line_count = min(500, max(1, int(max_lines)))
        log_path = paths["log"]
        log_lines, log_meta = current_run_log_tail(log_path, instance, max_lines=line_count)
        return {
            "symbol": symbol.upper(),
            "log_path": str(log_path),
            "log_exists": log_path.exists(),
            "max_lines": line_count,
            "lines": log_lines,
            "log_scope": log_meta["scope"],
            "run_id": log_meta.get("run_id"),
            "log_start_offset": log_meta.get("log_start_offset"),
            "log_size": log_meta.get("log_size"),
            "log_started_at": log_meta.get("log_started_at"),
        }

    def start_process(
        self,
        *,
        market: Market,
        admin_user: User,
        instance: MarketMakerInstance,
        strategy_key: str,
        api_base_url: str,
        public_ws_url: str,
        private_ws_url: str,
        start_readiness: dict | None = None,
        fallback_liquidity_canceled: int = 0,
    ) -> dict:
        current = self.status(
            market.symbol,
            strategy_version=strategy_key,
            instance=instance,
            start_readiness=start_readiness,
        )
        if current["running"] and current.get("heartbeat_status") == "ok":
            return current
        if current["running"]:
            stop_result = self.stop_process_func(market.symbol) if self.stop_process_func else self.stop_process(market.symbol)
            if not stop_result.get("exited"):
                raise RuntimeError("existing maker instance is stale but could not be stopped")

        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        paths = self.paths(market.symbol)
        if market.product_type == PRODUCT_TYPE_PERP:
            cmd = [
                sys.executable,
                str(self.project_root / "contract_mm_service.py"),
                "--symbol",
                market.symbol,
                "--base-url",
                api_base_url,
                "--admin-api-key",
                admin_user.api_key,
            ]
        else:
            cmd = [
                sys.executable,
                str(self.project_root / "mm_service.py"),
                "--symbol",
                market.symbol,
                "--require-runtime-bundle",
                "--base-url",
                api_base_url,
                "--public-ws-url",
                public_ws_url,
                "--private-ws-url",
                private_ws_url,
                "--admin-api-key",
                admin_user.api_key,
            ]
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        # 5174 与 5198 会为同一币对同时运行策略进程；把本实例的 maker
        # runtime 目录传给子进程，避免单实例锁与 pid 文件跨实例冲突。
        env[MAKER_RUNTIME_DIR_ENV] = str(self.runtime_dir)
        safe_cmd = ["***" if previous == "--admin-api-key" else value for previous, value in zip(["", *cmd[:-1]], cmd)]
        run_started_at = datetime.now(tz=UTC)
        run_id = f"{market.symbol}-{int(run_started_at.timestamp() * 1000)}"
        self._rotate_maker_log(paths["log"])
        log_start_offset = file_size(paths["log"])
        with paths["log"].open("a", encoding="utf-8") as log:
            log.write(f"\n[{run_started_at.isoformat()}] run_id={run_id} start {' '.join(safe_cmd)}\n")
            log.flush()
            process = self.popen_factory(
                cmd,
                cwd=str(self.project_root),
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        paths["pid"].write_text(str(process.pid), encoding="utf-8")
        now = datetime.now(tz=UTC)
        run_meta = {
            "run_id": run_id,
            "pid": process.pid,
            "log_path": str(paths["log"]),
            "log_start_offset": log_start_offset,
            "log_started_at": to_millis(run_started_at),
        }
        instance.status = "starting"
        instance.pid = process.pid
        instance.strategy_key = strategy_key
        instance.started_by_user_id = admin_user.id
        instance.started_at = now
        instance.stopped_at = None
        instance.last_heartbeat_at = None
        instance.last_error = None
        instance.log_path = str(paths["log"])
        instance.last_metrics_json = with_maker_instance_run_meta(
            {
                "symbol": market.symbol,
                "service_state": "starting",
                "strategy_version": instance.strategy_key,
                "product_type": market.product_type,
                "pid": process.pid,
                "fallback_liquidity_canceled": fallback_liquidity_canceled,
                "ts": to_millis(now),
            },
            run_meta,
        )
        return self.status(
            market.symbol,
            strategy_version=instance.strategy_key,
            instance=instance,
            start_readiness=start_readiness,
        )

    def _rotate_maker_log(self, log_path: Path) -> None:
        """避免演示做市日志无限追加到数 GB；超过阈值时归档并保留最近 N 份。"""
        try:
            if not log_path.exists() or file_size(log_path) <= MAKER_LOG_ROTATE_BYTES:
                return
            archive_dir = log_path.parent / "log_archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S_%f")
            archive_path = archive_dir / f"{log_path.name}.{timestamp}"
            suffix = 1
            while archive_path.exists():
                archive_path = archive_dir / f"{log_path.name}.{timestamp}_{suffix}"
                suffix += 1
            log_path.rename(archive_path)
            matches = sorted(archive_dir.glob(f"{log_path.name}.*"))
            for old in matches[:-MAKER_LOG_KEEP_ARCHIVES]:
                old.unlink(missing_ok=True)
        except OSError:
            # 轮转失败不应阻断做市实例启动；下次启动会再尝试。
            return

    def stop_process(self, symbol: str) -> dict:
        paths = self.paths(symbol)
        pid = read_pid(paths["pid"])
        running = self.pid_checker(pid)
        exited = not running
        if pid and not running:
            paths["pid"].unlink(missing_ok=True)
        if pid and running:
            identity_ok, identity_reason = pid_matches_maker_instance(
                pid,
                symbol,
                project_root=self.project_root,
            )
            if not identity_ok:
                with paths["log"].open("a", encoding="utf-8") as log:
                    log.write(
                        f"\n[{datetime.now(tz=UTC).isoformat()}] stale pid ignored "
                        f"pid={pid} reason={identity_reason}\n"
                    )
                paths["pid"].unlink(missing_ok=True)
                return {
                    "pid": pid,
                    "was_running": False,
                    "exited": True,
                    "stale_pid": True,
                    "reason": identity_reason,
                }
            try:
                os.killpg(pid, signal.SIGTERM)
            except OSError:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            with paths["log"].open("a", encoding="utf-8") as log:
                log.write(f"\n[{datetime.now(tz=UTC).isoformat()}] stop pid={pid}\n")
            exited = wait_for_pid_exit(pid, pid_checker=self.pid_checker)
            if not exited:
                try:
                    os.killpg(pid, signal.SIGTERM)
                except OSError:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except OSError:
                        pass
                exited = wait_for_pid_exit(pid, timeout_seconds=4.0, pid_checker=self.pid_checker)
            if not exited:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
                exited = wait_for_pid_exit(pid, timeout_seconds=2.0, pid_checker=self.pid_checker)
            if exited:
                paths["pid"].unlink(missing_ok=True)
        return {"pid": pid, "was_running": running, "exited": exited}
