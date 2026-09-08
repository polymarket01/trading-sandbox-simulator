from __future__ import annotations
from app.services.maker_plugins import INTERNAL_MAKER_STRATEGIES
from app.services.maker_lifecycle import maker_serialized, switch_states

import asyncio
from contextlib import suppress
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import os
import platform
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.api.deps import get_admin_user
from app.core.config import settings
from app.core.constants import (
    ORDER_STATUS_CANCELED,
    ORDER_TYPE_LIMIT,
    POSITION_MODE_HEDGE,
    PRODUCT_TYPE_PERP,
    PRODUCT_TYPE_SPOT,
    ROLE_ADMIN,
    ROLE_BOT,
    SIDE_BUY,
    SIDE_SELL,
    TIF_GTC,
)
from app.core.decimal_utils import decimal_scale, decimal_to_str, quantize_scale, quantize_step
from app.core.security import generate_api_key, generate_api_secret, hash_password, verify_password
from app.core.time_utils import ensure_utc, to_millis
from app.db.session import get_db_session
from app.models.admin_operation_audit import AdminOperationAudit
from app.models.balance import Balance
from app.models.contract_account import ContractAccount
from app.models.contract_position import ContractPosition
from app.models.contract_user_setting import ContractUserSetting
from app.models.fee_profile import FeeProfile
from app.models.ledger_entry import LedgerEntry
from app.models.market import MARKET_VISIBILITY_LISTED, MARKET_VISIBILITY_TEST, Market
from app.models.market_bot_account import MarketBotAccount
from app.models.market_maker_instance import MarketMakerInstance
from app.models.market_strategy_config import MarketStrategyConfig
from app.models.contract_risk_limit_tier import ContractRiskLimitTier
from app.models.order import Order
from app.models.reset_template import ResetTemplate
from app.models.strategy_template import StrategyTemplate
from app.models.trade import Trade
from app.models.user import User
from app.schemas.api import (
    AdjustBalanceRequest,
    AdminOperationFailureRequest,
    ConfirmExecuteRequest,
    MarketBotCreateRequest,
    MarketBotDefaultsRequest,
    MarketBotFlowDefaultRequest,
    MarketBotUpdateRequest,
    MarketCreateRequest,
    MarketSweepPreviewRequest,
    OrderCreateRequest,
    SeedMarketBookRequest,
    UpdateMarketFeesRequest,
    UpdateMarketRequest,
    UpdateUserFeesRequest,
    UserCreateRequest,
    UserUpdateRequest,
)
from app.services.account_service import AccountService
from app.services.admin_operation_audit import record_admin_operation, serialize_admin_operation
from app.services.bot_orchestrator import BotOrchestratorService, maker_runtime_dir, pid_matches_maker_instance
from app.services.process_status import pid_is_running as cached_pid_is_running
from app.services.bot_coordinator import (
    FLOW_MODE_OFF,
    FLOW_MODE_REAL_IOC_SANDBOX,
    FLOW_MODE_VIRTUAL_VOLUME,
    LOCAL_SANDBOX_CONNECTORS,
    build_symbol_coordinator_state,
    build_symbol_robot_cards,
)
from app.services.contract_ledger import add_contract_ledger_entry, snapshot_contract_account
from app.services.history_retention_service import HistoryRetentionService, history_retention_auto_enabled
from app.services.liquidity_diagnostics_service import LiquidityDiagnosticsService
from app.services.persistence_contract import legacy_sampled_runtime, platform_durable_contract
from app.services.reconciliation_service import ReconciliationService
from app.services.paper_account_service import ensure_user_assets
from app.services.financial_outbox_service import (
    financial_outbox_summary,
    list_financial_outbox_events,
    list_financial_outbox_replay_requests,
    request_financial_outbox_replay,
    serialize_outbox_replay_request,
)
from app.models.accounting import AccountingEntry, AccountingTransaction
from app.models.accounting_proof import AccountingProofCheckpoint
from app.services.accounting_proof_service import (
    create_accounting_proof_checkpoint,
    list_accounting_proofs,
    serialize_accounting_proof,
    verify_accounting_proof_chain,
)
from app.services.accounting_service import create_shadow_reconciliation_adjustment
from app.models.robot_financial_checkpoint import RobotFinancialCheckpoint
from app.services.robot_financial_checkpoint_service import (
    create_robot_financial_checkpoint,
    list_robot_financial_checkpoints,
    serialize_robot_checkpoint,
)

LIQUIDITY_STATE_PERSIST_INTERVAL_SECONDS = 5
from app.services.accounting_gate_service import accounting_gate_readiness
from app.services.accounting_evidence_service import (
    ACCOUNTING_EVIDENCE_RUN_LOCK,
    list_accounting_evidence_runs,
)
from app.services.contract_liquidity_service import contract_liquidity_username, ensure_contract_liquidity_user
from app.services.market_data_service import BOOTSTRAP_SEED_SOURCE
from app.services.seed_book import build_seed_book_plan
from app.services.strategy_config_service import (
    PERP_STRATEGY_KEYS,
    SPOT_STRATEGY_KEYS,
    SUPPORTED_STRATEGY_KEYS,
    default_config_for_strategy,
    ensure_market_strategy_config,
    ensure_strategy_templates,
    is_legacy_aggressive_lite_config,
    merge_config,
    normalize_strategy_key,
    selected_market_strategy_config,
    set_selected_market_strategy,
    strategy_template_map,
)
from app.services.user_uid import next_uid_for_role, uid_rule_status

def default_runtime_config_snapshot():
    if not {"LITE", "PERP_MM"}.intersection(SUPPORTED_STRATEGY_KEYS):
        return {}
    from liquidity_v2.config import default_runtime_config_snapshot as snapshot
    return snapshot()


def default_lite_runtime_config_snapshot():
    return default_config_for_strategy("LITE") if "LITE" in SUPPORTED_STRATEGY_KEYS else {}


router = APIRouter(tags=["admin"])
ACCOUNTING_RECONCILIATION_RUN_LOCK = ACCOUNTING_EVIDENCE_RUN_LOCK
LIVE_ORDER_STATUSES = ["new", "partially_filled"]
TEST_ACCOUNT_ROLES = {"manual_user", "mm_bot"}
DEFAULT_MARKET_BOT_PASSWORD = "mm123"
DEFAULT_MARKET_BOT_MAKER_FEE = Decimal("0")
DEFAULT_MARKET_BOT_TAKER_FEE = Decimal("0")
PROJECT_ROOT = Path(__file__).resolve().parents[3]
MAKER_RUNTIME_DIR = PROJECT_ROOT / ".runtime"
MAKER_HEARTBEAT_STALE_SECONDS = 45
DEFAULT_WEB_PASSWORDS = {
    "spot_manual_user": "manual123",
    "spot_admin": "admin123",
    **{f"spot_mm_{index}": "mm123" for index in range(1, 11)},
    "flow_user_1": "flow123",
    "flow_user_2": "flow123",
}
MARKET_TEMPLATES = [
    {
        "template_id": "btcusdt_perp",
        "label": "BTC/USDT 永续",
        "description": "USDT 本位永续合约最小闭环模板；不创建现货做市机器人。",
        "symbol": "BTCUSDT-PERP",
        "product_type": "PERP",
        "market_type": "mainstream",
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "margin_asset": "USDT",
        "price_tick": "0.01",
        "qty_step": "0.001",
        "min_qty": "0.001",
        "min_notional": "5",
        "reference_price": "62000",
        "max_leverage": "20",
        "default_leverage": "5",
        "maintenance_margin_rate": "0.005",
        "funding_rate": "0",
        "funding_interval_hours": 8,
        "index_price_source": "binance",
        "mark_price_mode": "orderbook",
        "funding_rate_mode": "binance",
        "funding_interest_rate": "0.0001",
        "funding_clamp_rate": "0.0005",
        "funding_cap_rate": "0.02",
        "funding_impact_notional": "25000",
        "default_maker_fee_rate": "0",
        "default_taker_fee_rate": "0",
        "initial_base_balance": "0",
        "initial_quote_balance": "0",
    },
    {
        "template_id": "btc_usdt_mainstream",
        "label": "BTC/USDT 主流",
        "description": "适合测试高价主流币盘口铺单、刷量和深度恢复。",
        "symbol": "BTCUSDT",
        "market_type": "mainstream",
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "price_tick": "0.01",
        "qty_step": "0.0001",
        "min_qty": "0.0001",
        "min_notional": "5",
        "reference_price": "62000",
        "default_maker_fee_rate": "0",
        "default_taker_fee_rate": "0",
        "initial_base_balance": "100000000",
        "initial_quote_balance": "100000000",
    },
    {
        "template_id": "eth_usdt_mainstream",
        "label": "ETH/USDT 主流",
        "description": "适合测试中高价主流币做市和跨币种机器人参数。",
        "symbol": "ETHUSDT",
        "market_type": "mainstream",
        "base_asset": "ETH",
        "quote_asset": "USDT",
        "price_tick": "0.01",
        "qty_step": "0.001",
        "min_qty": "0.001",
        "min_notional": "5",
        "reference_price": "3500",
        "default_maker_fee_rate": "0",
        "default_taker_fee_rate": "0",
        "initial_base_balance": "100000000",
        "initial_quote_balance": "100000000",
    },
    {
        "template_id": "sol_usdt_mainstream",
        "label": "SOL/USDT 主流",
        "description": "适合测试中价币小 tick 盘口和订单刷新频率。",
        "symbol": "SOLUSDT",
        "market_type": "mainstream",
        "base_asset": "SOL",
        "quote_asset": "USDT",
        "price_tick": "0.001",
        "qty_step": "0.01",
        "min_qty": "0.01",
        "min_notional": "5",
        "reference_price": "150",
        "default_maker_fee_rate": "0",
        "default_taker_fee_rate": "0",
        "initial_base_balance": "100000000",
        "initial_quote_balance": "100000000",
    },
    {
        "template_id": "doge_usdt_mainstream",
        "label": "DOGE/USDT 主流",
        "description": "适合测试低价大数量币种的 step、tick 和盘口聚合。",
        "symbol": "DOGEUSDT",
        "market_type": "mainstream",
        "base_asset": "DOGE",
        "quote_asset": "USDT",
        "price_tick": "0.00001",
        "qty_step": "1",
        "min_qty": "1",
        "min_notional": "5",
        "reference_price": "0.16",
        "default_maker_fee_rate": "0",
        "default_taker_fee_rate": "0",
        "initial_base_balance": "100000000",
        "initial_quote_balance": "100000000",
    },
    {
        "template_id": "listed_mid_cap",
        "label": "独立上市中价币",
        "description": "适合测试单机币做市、人工交易和操控漏洞排查。",
        "symbol": "ABCUSDT",
        "market_type": "listed",
        "base_asset": "ABC",
        "quote_asset": "USDT",
        "price_tick": "0.001",
        "qty_step": "0.01",
        "min_qty": "0.01",
        "min_notional": "5",
        "reference_price": "4.2",
        "default_maker_fee_rate": "0",
        "default_taker_fee_rate": "0",
        "initial_base_balance": "100000000",
        "initial_quote_balance": "100000000",
    },
    {
        "template_id": "listed_micro_cap",
        "label": "独立上市低价币",
        "description": "适合测试低价大数量、容易被扫盘或拉盘的场景。",
        "symbol": "XYZUSDT",
        "market_type": "listed",
        "base_asset": "XYZ",
        "quote_asset": "USDT",
        "price_tick": "0.0001",
        "qty_step": "1",
        "min_qty": "1",
        "min_notional": "5",
        "reference_price": "0.25",
        "default_maker_fee_rate": "0",
        "default_taker_fee_rate": "0",
        "initial_base_balance": "100000000",
        "initial_quote_balance": "100000000",
    },
]


def get_service(request: Request):
    return request.app.state.order_service


def _admin_maker_runtime_dir() -> Path:
    """Admin-visible maker runtime dir.

    Tests monkeypatch ``MAKER_RUNTIME_DIR`` to isolate pid/log files; keep
    that override for sandbox mode.  The 5198 PaperTrading instance instead
    uses its own ``<paper_data_dir>/.maker_runtime`` so the same symbol can
    run a strategy process on 5174 and 5198 at the same time.
    """
    if MAKER_RUNTIME_DIR != PROJECT_ROOT / ".runtime":
        return MAKER_RUNTIME_DIR
    return maker_runtime_dir()


def get_bot_orchestrator(request: Request) -> BotOrchestratorService:
    service = getattr(request.app.state, "bot_orchestrator", None)
    if isinstance(service, BotOrchestratorService):
        service.project_root = PROJECT_ROOT
        service.runtime_dir = _admin_maker_runtime_dir()
        service.popen_factory = subprocess.Popen
        service.pid_checker = pid_is_running
        service.stop_process_func = stop_maker_instance_process
        return service
    return BotOrchestratorService(
        request.app.state.runtime,
        project_root=PROJECT_ROOT,
        runtime_dir=_admin_maker_runtime_dir(),
        popen_factory=subprocess.Popen,
        pid_checker=pid_is_running,
        stop_process_func=stop_maker_instance_process,
    )


def normalize_market_number(value, scale: int) -> str:
    return str(quantize_scale(value, scale))


def merge_liquidity_config(base: dict, override: dict) -> dict:
    merged = deepcopy(base) if isinstance(base, dict) else {}
    if not isinstance(override, dict):
        return merged
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_liquidity_config(merged[key], value)
        else:
            merged[key] = value
    return merged


def serialize_market(market: Market) -> dict:
    return {
        "symbol": market.symbol,
        "price_source": market.price_source,
        "price_source_symbol": market.price_source_symbol,
        "product_type": market.product_type,
        "market_type": market.market_type,
        "visibility": market.visibility or (MARKET_VISIBILITY_LISTED if market.is_listed else MARKET_VISIBILITY_TEST),
        "base_asset": market.base_asset,
        "quote_asset": market.quote_asset,
        "margin_asset": market.margin_asset,
        "price_tick": normalize_market_number(market.price_tick, market.price_precision),
        "qty_step": normalize_market_number(market.qty_step, market.qty_precision),
        "min_qty": normalize_market_number(market.min_qty, market.qty_precision),
        "min_notional": str(market.min_notional),
        "max_leverage": str(market.max_leverage),
        "default_leverage": str(market.default_leverage),
        "maintenance_margin_rate": str(market.maintenance_margin_rate),
        "funding_rate": str(market.funding_rate),
        "funding_interval_hours": market.funding_interval_hours,
        "index_price_source": market.index_price_source,
        "mark_price_mode": market.mark_price_mode,
        "funding_rate_mode": market.funding_rate_mode,
        "funding_interest_rate": str(market.funding_interest_rate),
        "funding_clamp_rate": str(market.funding_clamp_rate),
        "funding_cap_rate": str(market.funding_cap_rate),
        "funding_impact_notional": str(market.funding_impact_notional),
        "contract_trading_mode": market.contract_trading_mode,
        "reference_price": decimal_to_str(quantize_scale(market.reference_price, market.price_precision))
        if market.reference_price is not None
        else None,
        "price_precision": market.price_precision,
        "qty_precision": market.qty_precision,
        "is_active": market.is_active,
        "default_maker_fee_rate": str(market.default_maker_fee_rate),
        "default_taker_fee_rate": str(market.default_taker_fee_rate),
    }


def mask_api_secret(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "****"
    return f"{value[:6]}...{value[-4:]}"


def is_masked_api_secret_placeholder(candidate: str | None, current_secret: str | None) -> bool:
    cleaned = (candidate or "").strip()
    if not cleaned:
        return False
    return bool(current_secret) and cleaned == mask_api_secret(current_secret)


def serialize_user(
    user: User,
    balances: list[dict] | None = None,
    fee_profiles: list[dict] | None = None,
    *,
    include_api_secret: bool = False,
) -> dict:
    api_secret = user.api_secret_hash or ""
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "api_key": user.api_key,
        "api_secret": api_secret if include_api_secret else mask_api_secret(api_secret),
        "api_secret_masked": mask_api_secret(api_secret),
        "api_secret_present": bool(api_secret),
        "has_password": user.password_hash is not None,
        "is_active": user.is_active,
        "balances": balances or [],
        "fee_profiles": fee_profiles or [],
    }


def deployment_check(code: str, severity: str, label: str, detail: str, action: str) -> dict:
    return {"code": code, "severity": severity, "label": label, "detail": detail, "action": action}


def summarize_names(names: list[str], limit: int = 8) -> str:
    return ", ".join(names[:limit]) + (" ..." if len(names) > limit else "")


def current_process_rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if platform.system().lower() == "darwin":
        return round(rss / 1024 / 1024, 2)
    return round(rss / 1024, 2)


def sqlite_database_path() -> Path | None:
    url = settings.database_url
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if url.startswith(prefix):
            return Path(url[len(prefix):])
    return None


def file_size_mb(path: Path | None) -> float | None:
    if path is None or not path.exists():
        return None
    return round(path.stat().st_size / 1024 / 1024, 2)


def maker_instance_paths(symbol: str) -> dict[str, Path]:
    safe_symbol = "".join(ch for ch in symbol.upper() if ch.isalnum() or ch in {"_", "-"})
    directory = _admin_maker_runtime_dir()
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


def pid_is_running(pid: int | None) -> bool:
    return cached_pid_is_running(pid)


def wait_for_pid_exit(pid: int | None, *, timeout_seconds: float = 4.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        # Stop/restart must observe the process itself, not the five-second
        # status cache used by read-heavy admin pages. A cached alive result
        # can otherwise consume the whole shutdown budget and return a false
        # 409 after the maker has already exited.
        if not cached_pid_is_running(pid, force=True):
            return True
        time.sleep(0.1)
    return not cached_pid_is_running(pid, force=True)


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


def datetime_from_millis(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return datetime.fromtimestamp(number / 1000, tz=UTC)


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


def maker_instance_status(
    symbol: str,
    request: Request,
    *,
    strategy_version: str | None = None,
    configured_strategy_version: str | None = None,
    instance: MarketMakerInstance | None = None,
    start_readiness: dict | None = None,
) -> dict:
    transition = switch_states(request.app.state.runtime).get(symbol.upper())
    auto_restart_blocked = bool(transition and transition.get("status") in {"running", "failed", "interrupted"})
    ladder = getattr(request.app.state, "contract_ladder_service", None)
    worker = ladder.workers.get(symbol.upper()) if ladder else None
    if configured_strategy_version in INTERNAL_MAKER_STRATEGIES and worker is not None:
        status = worker.status()
        running = bool(worker.config.get("enabled"))
        return {"auto_restart_blocked": auto_restart_blocked, "transition": transition, "symbol": symbol, "running": running, "status": status.get("state") if running else "stopped",
                "strategy_key": configured_strategy_version, "strategy_version": configured_strategy_version,
                "heartbeat_status": "ok" if time.monotonic()-worker.heartbeat < 3 else "stale",
                "pid": os.getpid(), "start_readiness": start_readiness, "last_metrics": status, "last_log_lines": []}
    paths = maker_instance_paths(symbol)
    pid = read_pid(paths["pid"])
    if pid is None and instance is not None:
        pid = instance.pid
    running = pid_is_running(pid)
    pid_identity_ok = True
    pid_identity_reason = "not_running"
    if running:
        pid_identity_ok, pid_identity_reason = pid_matches_maker_instance(
            pid,
            symbol,
            project_root=PROJECT_ROOT,
        )
        if not pid_identity_ok:
            running = False
    runtime_metrics = request.app.state.runtime.liquidity_metrics.get(symbol.upper(), {})
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
    metrics_age_ms: int | None = None
    if last_metrics_ts is not None:
        try:
            metrics_age_ms = max(0, to_millis(now) - int(last_metrics_ts))
        except (TypeError, ValueError):
            metrics_age_ms = None
    metrics_data_plane_status = (
        "missing"
        if last_metrics_ts is None or metrics_age_ms is None
        else (
            "stale"
            if metrics_age_ms > MAKER_HEARTBEAT_STALE_SECONDS * 1000
            else "fresh"
        )
    )
    engine_open_order_count: int | None = None
    try:
        engine_book = request.app.state.runtime.engine.books.get(symbol.upper())
        engine_open_order_count = len(engine_book.orders) if engine_book is not None else 0
    except (AttributeError, TypeError):
        engine_open_order_count = None
    data_plane_status = (
        "empty"
        if running and engine_open_order_count == 0
        else metrics_data_plane_status
    )
    if running and status == "running" and data_plane_status != "fresh":
        # A live heartbeat only proves that the control loop process exists.
        # A stale local-book timestamp means its data plane is not serving the
        # market and must not be presented as healthy.
        status = "degraded"
    log_path = paths["log"]
    log_lines, log_meta = current_run_log_tail(log_path, instance)
    runtime_strategy = (
        (instance.strategy_key if instance is not None else None)
        or strategy_version
        or request.app.state.runtime.get_liquidity_strategy_selection(symbol.upper())
    )
    configured_strategy = configured_strategy_version or strategy_version or runtime_strategy
    runtime_strategy_key = normalize_strategy_key(runtime_strategy) if runtime_strategy else None
    configured_strategy_key = normalize_strategy_key(configured_strategy) if configured_strategy else None
    return {
        "auto_restart_blocked": auto_restart_blocked, "transition": transition,
        "symbol": symbol.upper(),
        "status": status,
        "persisted_status": persisted_status,
        "heartbeat_status": hb_status,
        "pid": pid,
        "pid_identity": {"ok": pid_identity_ok, "reason": pid_identity_reason},
        "running": running,
        "strategy_version": runtime_strategy,
        "configured_strategy_version": configured_strategy,
        "runtime_strategy_mismatch": bool(
            running
            and runtime_strategy_key
            and configured_strategy_key
            and runtime_strategy_key != configured_strategy_key
        ),
        "metrics_present": bool(metrics),
        "last_metrics_ts": last_metrics_ts,
        "metrics_age_ms": metrics_age_ms,
        "data_plane_status": data_plane_status,
        "engine_open_order_count": engine_open_order_count,
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


def maker_instance_strategy_key(market: Market, fallback: str | None = None) -> str | None:
    if market.product_type == PRODUCT_TYPE_PERP:
        return fallback or "PERP_MM"
    return fallback


def api_base_url_from_request(request: Request) -> str:
    return f"{str(request.base_url).rstrip('/')}{settings.api_prefix}"


def ws_url_from_request(request: Request, path: str) -> str:
    scheme = "wss" if request.url.scheme == "https" else "ws"
    return f"{scheme}://{request.url.netloc}{path}"


async def enabled_market_maker_accounts(session: AsyncSession, market: Market) -> list[tuple[MarketBotAccount, User]]:
    rows = await session.execute(
        select(MarketBotAccount, User)
        .join(User, User.id == MarketBotAccount.user_id)
        .where(
            MarketBotAccount.market_id == market.id,
            MarketBotAccount.role == "maker",
            MarketBotAccount.is_enabled.is_(True),
            User.is_active.is_(True),
        )
        .order_by(MarketBotAccount.id.asc())
    )
    return [(bot, user) for bot, user in rows.all()]


def build_sweep_preview(market: Market, snapshot: dict, payload: MarketSweepPreviewRequest) -> dict:
    levels = snapshot.get("asks" if payload.side == SIDE_BUY else "bids", [])
    bids = snapshot.get("bids", [])
    asks = snapshot.get("asks", [])
    best_bid = Decimal(str(bids[0][0])) if bids else Decimal("0")
    best_ask = Decimal(str(asks[0][0])) if asks else Decimal("0")
    reference_price = (best_bid + best_ask) / Decimal("2") if best_bid > 0 and best_ask > 0 else best_bid or best_ask
    best_opposite = best_ask if payload.side == SIDE_BUY else best_bid
    quantity_mode = payload.quantity is not None
    requested_quantity = Decimal(payload.quantity or 0)
    requested_quote = Decimal(payload.quote_amount or 0)
    filled_quantity = Decimal("0")
    quote_used = Decimal("0")
    worst_price = Decimal("0")
    levels_used = 0

    for price_text, quantity_text in levels[:payload.depth]:
        price = Decimal(str(price_text))
        level_quantity = Decimal(str(quantity_text))
        if price <= 0 or level_quantity <= 0:
            continue
        if quantity_mode:
            remaining_quantity = requested_quantity - filled_quantity
            if remaining_quantity <= 0:
                break
            take_quantity = min(level_quantity, remaining_quantity)
        else:
            remaining_quote = requested_quote - quote_used
            if remaining_quote <= 0:
                break
            take_quantity = min(level_quantity, remaining_quote / price)
        if take_quantity <= 0:
            break
        filled_quantity += take_quantity
        quote_used += take_quantity * price
        worst_price = price
        levels_used += 1

    remaining_quantity = max(Decimal("0"), requested_quantity - filled_quantity) if quantity_mode else Decimal("0")
    remaining_quote = max(Decimal("0"), requested_quote - quote_used) if not quantity_mode else Decimal("0")
    avg_price = quote_used / filled_quantity if filled_quantity > 0 else Decimal("0")
    slippage_bps = Decimal("0")
    if avg_price > 0 and reference_price > 0:
        if payload.side == SIDE_BUY:
            slippage_bps = (avg_price - reference_price) / reference_price * Decimal("10000")
        else:
            slippage_bps = (reference_price - avg_price) / reference_price * Decimal("10000")
    move_pct = Decimal("0")
    if worst_price > 0 and best_opposite > 0:
        if payload.side == SIDE_BUY:
            move_pct = (worst_price - best_opposite) / best_opposite * Decimal("100")
        else:
            move_pct = (best_opposite - worst_price) / best_opposite * Decimal("100")
    filled = remaining_quantity <= 0 if quantity_mode else remaining_quote <= Decimal("0.00000001")
    status = "empty" if filled_quantity <= 0 else "filled" if filled else "partial"
    if status == "empty":
        severity = "critical"
        summary = "对手盘为空，假想扫盘无法成交。"
    elif status == "partial":
        severity = "critical"
        summary = "当前盘口深度不足，假想扫盘只能部分成交。"
    elif abs(slippage_bps) >= Decimal("200") or move_pct >= Decimal("2"):
        severity = "critical"
        summary = "小额扫盘会造成明显价格冲击，需要补深度或放宽测试预期。"
    elif abs(slippage_bps) >= Decimal("50") or move_pct >= Decimal("0.5"):
        severity = "warn"
        summary = "假想扫盘存在可见滑点，适合进一步人工检验。"
    else:
        severity = "ok"
        summary = "假想扫盘冲击较小。"
    return {
        "symbol": market.symbol,
        "side": payload.side,
        "input_mode": "quantity" if quantity_mode else "quote_amount",
        "requested_quantity": decimal_to_str(quantize_scale(requested_quantity, market.qty_precision)) if quantity_mode else None,
        "requested_quote_amount": decimal_to_str(requested_quote) if not quantity_mode else None,
        "filled_quantity": decimal_to_str(quantize_scale(filled_quantity, market.qty_precision)),
        "quote_amount": decimal_to_str(quote_used),
        "remaining_quantity": decimal_to_str(quantize_scale(remaining_quantity, market.qty_precision)),
        "remaining_quote_amount": decimal_to_str(remaining_quote),
        "avg_price": decimal_to_str(quantize_scale(avg_price, market.price_precision)) if avg_price else "0",
        "worst_price": decimal_to_str(quantize_scale(worst_price, market.price_precision)) if worst_price else "0",
        "reference_price": decimal_to_str(quantize_scale(reference_price, market.price_precision)) if reference_price else "0",
        "slippage_bps": decimal_to_str(slippage_bps),
        "move_pct": decimal_to_str(move_pct),
        "levels_used": levels_used,
        "status": status,
        "severity": severity,
        "summary": summary,
        "depth": payload.depth,
    }


async def ensure_user_asset_template(
    session: AsyncSession,
    user: User,
    asset: str,
    amount: Decimal,
    *,
    note: str,
    now: datetime,
    update_existing_template: bool = False,
) -> tuple[bool, bool]:
    normalized_asset = asset.upper().strip()
    if not normalized_asset:
        return False, False
    if amount < Decimal("0"):
        raise HTTPException(status_code=400, detail=f"{normalized_asset} initial balance cannot be negative")

    balance_created = False
    template_created = False
    existing_balance = await session.scalar(
        select(Balance).where(Balance.user_id == user.id, Balance.asset == normalized_asset)
    )
    if existing_balance is None:
        await AccountService().get_balance(session, user.id, normalized_asset)
        balance_created = True
        if amount != Decimal("0"):
            await AccountService().apply_change(
                session, user.id, normalized_asset,
                available_delta=amount, frozen_delta=Decimal("0"),
                change_type="deposit_reset", amount=amount,
                note=note, created_at=now,
            )

    existing_template = await session.scalar(
        select(ResetTemplate).where(
            ResetTemplate.name == "default",
            ResetTemplate.user_id == user.id,
            ResetTemplate.asset == normalized_asset,
        )
    )
    if existing_template is None:
        session.add(ResetTemplate(name="default", user_id=user.id, asset=normalized_asset, amount=amount))
        template_created = True
    elif update_existing_template:
        existing_template.amount = amount
    return balance_created, template_created


async def ensure_contract_bot_account(
    session: AsyncSession,
    user: User,
    *,
    market: Market,
    margin_asset: str,
    wallet_balance: Decimal,
    note: str,
    now: datetime,
) -> tuple[bool, bool]:
    asset = margin_asset.upper().strip() or "USDT"
    if wallet_balance < Decimal("0"):
        raise HTTPException(status_code=400, detail=f"{asset} contract wallet cannot be negative")
    account = await session.scalar(
        select(ContractAccount).where(
            ContractAccount.user_id == user.id,
            ContractAccount.margin_asset == asset,
        )
    )
    if account is None:
        account = ContractAccount(
            user_id=user.id,
            margin_asset=asset,
            wallet_balance=wallet_balance,
            available_margin=wallet_balance,
            used_margin=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            realized_pnl=Decimal("0"),
            total_fees=Decimal("0"),
            updated_at=now,
        )
        session.add(account)
        await session.flush()
        await add_contract_ledger_entry(
            session,
            account,
            change_type="account_init",
            amount=wallet_balance,
            before={
                "wallet": Decimal("0"),
                "available": Decimal("0"),
                "used_margin": Decimal("0"),
                "unrealized_pnl": Decimal("0"),
                "realized_pnl": Decimal("0"),
                "total_fees": Decimal("0"),
            },
            note=note,
            created_at=now,
            skip_if_unchanged=False,
        )
        await ensure_contract_account_position_mode(session, user.id, market)
        return True, True

    if Decimal(account.wallet_balance) <= Decimal("0") and wallet_balance > Decimal("0"):
        before = snapshot_contract_account(account)
        account.wallet_balance = wallet_balance
        account.available_margin = wallet_balance
        account.used_margin = Decimal("0")
        account.updated_at = now
        await add_contract_ledger_entry(
            session,
            account,
            change_type="admin_adjust",
            amount=wallet_balance,
            before=before,
            note=note,
            created_at=now,
        )
        await ensure_contract_account_position_mode(session, user.id, market)
        return False, True
    await ensure_contract_account_position_mode(session, user.id, market)
    return False, False


async def ensure_contract_account_position_mode(session: AsyncSession, user_id: int, market: Market) -> ContractUserSetting:
    setting = await session.scalar(
        select(ContractUserSetting).where(
            ContractUserSetting.user_id == user_id,
            ContractUserSetting.market_id == market.id,
        )
    )
    if setting is None:
        setting = ContractUserSetting(
            user_id=user_id,
            market_id=market.id,
            leverage=market.default_leverage,
            margin_mode="isolated",
            position_mode=POSITION_MODE_HEDGE,
        )
        session.add(setting)
        await session.flush()
    else:
        setting.position_mode = POSITION_MODE_HEDGE
    return setting


async def ensure_fee_profile(
    session: AsyncSession,
    user_id: int,
    market_id: int,
    maker_fee_rate: Decimal,
    taker_fee_rate: Decimal,
) -> FeeProfile:
    profile = await session.scalar(
        select(FeeProfile).where(FeeProfile.user_id == user_id, FeeProfile.market_id == market_id)
    )
    if profile is None:
        profile = FeeProfile(
            user_id=user_id,
            market_id=market_id,
            maker_fee_rate=maker_fee_rate,
            taker_fee_rate=taker_fee_rate,
        )
        session.add(profile)
    else:
        profile.maker_fee_rate = maker_fee_rate
        profile.taker_fee_rate = taker_fee_rate
    return profile


def normalize_bot_text(value: str | None, fallback: str, max_length: int = 64) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        cleaned = fallback
    return cleaned[:max_length]


def default_market_bot_username(symbol: str, index: int) -> str:
    return f"{symbol.lower()}_mm_{index}"


def default_market_flow_username(symbol: str) -> str:
    return f"{symbol.lower()}_flow_1"


async def unique_market_bot_username(session: AsyncSession, symbol: str, preferred: str | None, index: int) -> str:
    base = normalize_bot_text(preferred, default_market_bot_username(symbol, index), 48)
    candidate = base
    suffix = 1
    while await session.scalar(select(User.id).where(User.username == candidate)) is not None:
        suffix += 1
        candidate = normalize_bot_text(None, f"{base}_{suffix}", 64)
    return candidate


async def ensure_unique_api_key(session: AsyncSession, api_key: str | None, *, exclude_user_id: int | None = None) -> str | None:
    cleaned = normalize_bot_text(api_key, "", 128)
    if not cleaned:
        return None
    query = select(User.id).where(User.api_key == cleaned)
    if exclude_user_id is not None:
        query = query.where(User.id != exclude_user_id)
    existing = await session.scalar(query)
    if existing is not None:
        raise HTTPException(status_code=409, detail="api_key already exists")
    return cleaned


async def unique_market_bot_label(session: AsyncSession, market: Market, preferred: str | None, index: int) -> str:
    base = normalize_bot_text(preferred, f"{market.symbol}-MM-{index}", 56)
    candidate = base
    suffix = 1
    while (
        await session.scalar(
            select(MarketBotAccount.id).where(
                MarketBotAccount.market_id == market.id,
                MarketBotAccount.bot_label == candidate,
            )
        )
        is not None
    ):
        suffix += 1
        candidate = normalize_bot_text(None, f"{base}-{suffix}", 64)
    return candidate


def resolve_bot_reference_price(market: Market, reference_price: Decimal | None) -> Decimal:
    resolved = reference_price or market.reference_price
    if resolved is None or resolved <= Decimal("0"):
        raise HTTPException(status_code=400, detail="reference_price is required and must be positive")
    return Decimal(resolved)


def resolve_bot_base_amount(
    market: Market,
    reference_price: Decimal,
    initial_base_notional: Decimal,
    initial_base_amount: Decimal | None,
) -> Decimal:
    if initial_base_amount is not None:
        amount = Decimal(initial_base_amount)
    else:
        if initial_base_notional <= Decimal("0"):
            amount = Decimal("0")
        else:
            amount = Decimal(initial_base_notional) / reference_price
    return quantize_step(amount, Decimal(market.qty_step))


async def serialize_market_bot_account(
    session: AsyncSession,
    market: Market,
    bot: MarketBotAccount,
    *,
    include_api_secret: bool = False,
    request: Request | None = None,
) -> dict:
    user = await session.scalar(select(User).where(User.id == bot.user_id))
    if user is None:
        raise HTTPException(status_code=500, detail="market bot user missing")
    balance_rows = await session.execute(select(Balance).where(Balance.user_id == user.id))
    balances = {balance.asset: balance for balance in balance_rows.scalars()}
    base_balance = balances.get(market.base_asset)
    quote_balance = balances.get(market.quote_asset)
    contract_account = None
    margin_asset = (market.margin_asset or market.quote_asset).upper()
    if market.product_type == PRODUCT_TYPE_PERP:
        contract_account = await session.scalar(
            select(ContractAccount).where(
                ContractAccount.user_id == user.id,
                ContractAccount.margin_asset == margin_asset,
            )
        )
    api_secret = user.api_secret_hash or ""
    return {
        "id": bot.id,
        "market_id": bot.market_id,
        "symbol": market.symbol,
        "uid": user.id,
        "username": user.username,
        "api_key": user.api_key,
        "api_secret": api_secret if include_api_secret else mask_api_secret(api_secret),
        "api_secret_masked": mask_api_secret(api_secret),
        "api_secret_present": bool(api_secret),
        "bot_label": bot.bot_label,
        "role": bot.role,
        "strategy_role": bot.strategy_role,
        "initial_quote_amount": decimal_to_str(bot.initial_quote_amount),
        "initial_base_amount": decimal_to_str(bot.initial_base_amount),
        "initial_base_notional": decimal_to_str(bot.initial_base_notional),
        "reference_price": decimal_to_str(bot.reference_price),
        "is_enabled": bot.is_enabled,
        "base_balance": {
            "asset": market.base_asset,
            "available": decimal_to_str(base_balance.available) if base_balance else "0",
            "frozen": decimal_to_str(base_balance.frozen) if base_balance else "0",
        },
        "quote_balance": {
            "asset": market.quote_asset,
            "available": decimal_to_str(quote_balance.available) if quote_balance else "0",
            "frozen": decimal_to_str(quote_balance.frozen) if quote_balance else "0",
        },
        "contract_account": {
            "margin_asset": margin_asset,
            "wallet_balance": decimal_to_str(contract_account.wallet_balance) if contract_account else "0",
            "available_margin": decimal_to_str(contract_account.available_margin) if contract_account else "0",
            "used_margin": decimal_to_str(contract_account.used_margin) if contract_account else "0",
            "unrealized_pnl": decimal_to_str(contract_account.unrealized_pnl) if contract_account else "0",
            "realized_pnl": decimal_to_str(contract_account.realized_pnl) if contract_account else "0",
            "total_fees": decimal_to_str(contract_account.total_fees) if contract_account else "0",
        } if market.product_type == PRODUCT_TYPE_PERP else None,
        "created_at": to_millis(bot.created_at) if bot.created_at else None,
        "updated_at": to_millis(bot.updated_at) if bot.updated_at else None,
    }


def serialize_market_bot_account_snapshot(
    market: Market,
    bot: MarketBotAccount,
    user: User,
    *,
    balances_by_user: dict[int, list[Balance]],
    contract_accounts_by_user_asset: dict[tuple[int, str], ContractAccount],
    include_api_secret: bool = False,
) -> dict:
    balances = {balance.asset: balance for balance in balances_by_user.get(user.id, [])}
    base_balance = balances.get(market.base_asset)
    quote_balance = balances.get(market.quote_asset)
    margin_asset = (market.margin_asset or market.quote_asset).upper()
    contract_account = contract_accounts_by_user_asset.get((user.id, margin_asset)) if market.product_type == PRODUCT_TYPE_PERP else None
    api_secret = user.api_secret_hash or ""
    return {
        "id": bot.id,
        "market_id": bot.market_id,
        "symbol": market.symbol,
        "uid": user.id,
        "username": user.username,
        "api_key": user.api_key,
        "api_secret": api_secret if include_api_secret else mask_api_secret(api_secret),
        "api_secret_masked": mask_api_secret(api_secret),
        "api_secret_present": bool(api_secret),
        "bot_label": bot.bot_label,
        "role": bot.role,
        "strategy_role": bot.strategy_role,
        "initial_quote_amount": decimal_to_str(bot.initial_quote_amount),
        "initial_base_amount": decimal_to_str(bot.initial_base_amount),
        "initial_base_notional": decimal_to_str(bot.initial_base_notional),
        "reference_price": decimal_to_str(bot.reference_price),
        "is_enabled": bot.is_enabled,
        "base_balance": {
            "asset": market.base_asset,
            "available": decimal_to_str(base_balance.available) if base_balance else "0",
            "frozen": decimal_to_str(base_balance.frozen) if base_balance else "0",
        },
        "quote_balance": {
            "asset": market.quote_asset,
            "available": decimal_to_str(quote_balance.available) if quote_balance else "0",
            "frozen": decimal_to_str(quote_balance.frozen) if quote_balance else "0",
        },
        "contract_account": {
            "margin_asset": margin_asset,
            "wallet_balance": decimal_to_str(contract_account.wallet_balance) if contract_account else "0",
            "available_margin": decimal_to_str(contract_account.available_margin) if contract_account else "0",
            "used_margin": decimal_to_str(contract_account.used_margin) if contract_account else "0",
            "unrealized_pnl": decimal_to_str(contract_account.unrealized_pnl) if contract_account else "0",
            "realized_pnl": decimal_to_str(contract_account.realized_pnl) if contract_account else "0",
            "total_fees": decimal_to_str(contract_account.total_fees) if contract_account else "0",
        } if market.product_type == PRODUCT_TYPE_PERP else None,
        "created_at": to_millis(bot.created_at) if bot.created_at else None,
        "updated_at": to_millis(bot.updated_at) if bot.updated_at else None,
    }


def serialize_strategy_template(template: StrategyTemplate) -> dict:
    from app.services.maker_plugins import get_plugin
    return {
        "products": get_plugin(template.strategy_key).manifest["products"],
        "id": template.id,
        "strategy_key": template.strategy_key,
        "display_name": template.display_name,
        "scope": template.scope,
        "config_schema": template.config_schema_json or {},
        "default_config": template.default_config_json or {},
        "is_active": template.is_active,
        "created_at": to_millis(template.created_at) if template.created_at else None,
        "updated_at": to_millis(template.updated_at) if template.updated_at else None,
    }


def serialize_market_strategy_config(
    config: MarketStrategyConfig,
    template: StrategyTemplate | None,
    *,
    effective_config: dict | None = None,
) -> dict:
    return {
        "id": config.id,
        "market_id": config.market_id,
        "strategy_key": config.strategy_key,
        "display_name": template.display_name if template else config.strategy_key,
        "config": config.config_json or {},
        "effective_config": effective_config if effective_config is not None else config.config_json or {},
        "schema": template.config_schema_json if template else {},
        "is_enabled": config.is_enabled,
        "updated_by_user_id": config.updated_by_user_id,
        "created_at": to_millis(config.created_at) if config.created_at else None,
        "updated_at": to_millis(config.updated_at) if config.updated_at else None,
    }


async def ensure_strategy_config_for_key(
    session: AsyncSession,
    market: Market,
    strategy_key: str,
) -> MarketStrategyConfig:
    return await ensure_market_strategy_config(session, market, normalize_strategy_key(strategy_key))


def parse_strategy_key_or_400(value: object) -> str:
    raw = str(value or "LITE").upper()
    if raw not in SUPPORTED_STRATEGY_KEYS:
        raise HTTPException(status_code=400, detail="unsupported strategy version")
    return raw


def validate_strategy_key_for_market(market: Market, key: str) -> str:
    if market.product_type == PRODUCT_TYPE_PERP:
        if key not in PERP_STRATEGY_KEYS:
            raise HTTPException(status_code=400, detail="strategy is only supported for SPOT markets")
        return key
    if key not in SPOT_STRATEGY_KEYS:
        raise HTTPException(status_code=400, detail="strategy is only supported for PERP markets")
    return key


def legacy_strategy_overrides(request: Request, symbol: str, strategy_key: str) -> dict:
    runtime = request.app.state.runtime
    key = normalize_strategy_key(strategy_key)
    if key in PERP_STRATEGY_KEYS:
        return {}
    if key == "LITE":
        overrides = runtime.get_liquidity_lite_runtime_overrides(symbol)
        if is_legacy_aggressive_lite_config(overrides):
            runtime.set_liquidity_lite_runtime_overrides(symbol, {})
            return {}
        return overrides
    return {}


def legacy_live_strategy_config(request: Request, symbol: str, strategy_key: str) -> dict:
    metrics = request.app.state.runtime.liquidity_metrics.get(symbol, {})
    key = normalize_strategy_key(strategy_key)
    if key in PERP_STRATEGY_KEYS:
        return {}
    if key == "LITE":
        return metrics.get("lite_runtime_config", {}) or {}
    return {}


async def effective_strategy_config(
    session: AsyncSession,
    request: Request,
    market: Market,
    strategy_key: str,
) -> tuple[MarketStrategyConfig, StrategyTemplate | None, dict]:
    key = normalize_strategy_key(strategy_key)
    templates = await strategy_template_map(session)
    template = templates.get(key)
    config = await ensure_strategy_config_for_key(session, market, key)
    default_config = default_config_for_strategy(key)
    effective = merge_config(default_config, config.config_json or {})
    effective = merge_config(effective, legacy_strategy_overrides(request, market.symbol, key))
    return config, template, effective


async def current_strategy_config(
    session: AsyncSession,
    request: Request,
    market: Market,
) -> tuple[MarketStrategyConfig, StrategyTemplate | None, dict]:
    fallback = request.app.state.runtime.get_liquidity_strategy_selection(market.symbol)
    selected = await selected_market_strategy_config(session, market, fallback_strategy_key=fallback)
    return await effective_strategy_config(session, request, market, selected.strategy_key)


def recent_trade_source_counts(request: Request, symbol: str, limit: int = 200) -> dict[str, int]:
    counts: dict[str, int] = {}
    try:
        items = request.app.state.runtime.market_data.recent_trade_items(symbol, limit)
    except Exception:
        return counts
    for item in items:
        source_counts = item.get("source_counts") if isinstance(item, dict) else None
        if isinstance(source_counts, dict):
            for source, count in source_counts.items():
                try:
                    numeric = int(count or 0)
                except (TypeError, ValueError):
                    numeric = 0
                if numeric > 0:
                    counts[str(source)] = counts.get(str(source), 0) + numeric
            continue
        source = str(item.get("source") or "unknown") if isinstance(item, dict) else "unknown"
        counts[source] = counts.get(source, 0) + 1
    return counts


def user_system_data_condition(user_model) -> Any:
    username = func.lower(user_model.username)
    return or_(user_model.role == ROLE_ADMIN, username.like("contract_liq_%"))


def user_robot_data_condition(user_model) -> Any:
    username = func.lower(user_model.username)
    return and_(
        ~user_system_data_condition(user_model),
        or_(user_model.role == ROLE_BOT, username.like("%_mm_%"), username.like("%_flow_%")),
    )


def user_customer_data_condition(user_model) -> Any:
    return and_(~user_system_data_condition(user_model), ~user_robot_data_condition(user_model))


def normalize_admin_data_domain(value: str | None) -> str:
    normalized = (value or "all").strip().lower()
    if normalized in {"", "all"}:
        return "all"
    if normalized in {"customer", "robot", "system"}:
        return normalized
    raise HTTPException(status_code=400, detail="unsupported data_domain")


def admin_audit_query_meta(
    *,
    product_type: str | None,
    symbol: str | None,
    user_id: int | None,
    status: str | None,
    data_domain: str,
    limit: int,
    result_count: int,
) -> dict[str, Any]:
    normalized_product_type = (product_type or "").upper().strip()
    normalized_symbol = symbol.upper().strip() if symbol else "all"
    normalized_status = (status or "").strip().lower() if status is not None else None
    if normalized_status in {"", "all"}:
        normalized_status = "all"
    if normalized_status == "open":
        normalized_status = "live"
    return {
        "query_scope": "recent_slice",
        "count_scope": "loaded_result_only",
        "product_type": normalized_product_type if normalized_product_type in {PRODUCT_TYPE_SPOT, PRODUCT_TYPE_PERP} else "all",
        "symbol": normalized_symbol,
        "user_id": user_id,
        "status": normalized_status,
        "data_domain": data_domain,
        "limit": limit,
        "result_count": result_count,
        "has_more": result_count >= limit,
    }


def trade_source_robot_condition() -> Any:
    source = func.lower(Trade.source)
    return or_(
        source.like("%flow%"),
        source == "bot",
        source.like("%mm%"),
        source.like("%robot%"),
    )


def trade_source_system_condition() -> Any:
    source = func.lower(Trade.source)
    return or_(source == "bootstrap_seed", source.like("%seed%"), source.like("%fallback%"))


def trade_source_customer_condition() -> Any:
    return func.lower(Trade.source).in_(["user", "match"])


def admin_order_data_domain_condition(domain: str, user_model) -> Any | None:
    if domain == "customer":
        return user_customer_data_condition(user_model)
    if domain == "robot":
        return user_robot_data_condition(user_model)
    if domain == "system":
        return or_(user_system_data_condition(user_model), user_model.id.is_(None))
    return None


def admin_trade_data_domain_condition(domain: str, taker_user_model, maker_user_model) -> Any | None:
    customer_condition = or_(
        user_customer_data_condition(taker_user_model),
        user_customer_data_condition(maker_user_model),
    )
    robot_base_condition = or_(
        trade_source_robot_condition(),
        user_robot_data_condition(taker_user_model),
        user_robot_data_condition(maker_user_model),
    )
    if domain == "customer":
        return customer_condition
    if domain == "robot":
        return and_(~customer_condition, robot_base_condition)
    if domain == "system":
        return and_(~customer_condition, ~robot_base_condition)
    return None


async def scalar_count(session: AsyncSession, stmt) -> int:
    return int(await session.scalar(stmt) or 0)


async def build_data_retention_domain_summary(
    session: AsyncSession,
    *,
    order_total: int,
    open_order_total: int,
    trade_total: int,
) -> dict:
    non_live_order_predicate = Order.status.not_in(LIVE_ORDER_STATUSES)
    customer_order_retention = await scalar_count(
        session,
        select(func.count())
        .select_from(Order)
        .join(User, User.id == Order.user_id)
        .where(non_live_order_predicate, user_customer_data_condition(User)),
    )
    robot_order_retention = await scalar_count(
        session,
        select(func.count())
        .select_from(Order)
        .join(User, User.id == Order.user_id)
        .where(non_live_order_predicate, user_robot_data_condition(User)),
    )
    retention_eligible_orders = max(int(order_total) - int(open_order_total), 0)
    system_order_retention = max(retention_eligible_orders - customer_order_retention - robot_order_retention, 0)
    order_prune_candidate = robot_order_retention + system_order_retention

    taker_user = aliased(User)
    maker_user = aliased(User)
    customer_trade_count = await scalar_count(
        session,
        select(func.count())
        .select_from(Trade)
        .join(taker_user, taker_user.id == Trade.taker_user_id)
        .join(maker_user, maker_user.id == Trade.maker_user_id)
        .where(or_(user_customer_data_condition(taker_user), user_customer_data_condition(maker_user))),
    )
    robot_trade_count = await scalar_count(
        session,
        select(func.count())
        .select_from(Trade)
        .join(taker_user, taker_user.id == Trade.taker_user_id)
        .join(maker_user, maker_user.id == Trade.maker_user_id)
        .where(and_(user_robot_data_condition(taker_user), user_robot_data_condition(maker_user))),
    )
    system_trade_count = max(int(trade_total) - customer_trade_count - robot_trade_count, 0)
    trade_prune_candidate = robot_trade_count + system_trade_count

    market_rows = list(
        (
            await session.execute(
                select(Market.id, Market.symbol, Market.product_type).order_by(Market.symbol.asc())
            )
        ).all()
    )
    market_meta = {
        int(market_id): {"symbol": symbol, "product_type": product_type}
        for market_id, symbol, product_type in market_rows
    }
    order_rows = await session.execute(
        select(
            Order.market_id,
            func.count().label("retention_eligible"),
            func.sum(case((user_customer_data_condition(User), 1), else_=0)).label("customer_count"),
            func.sum(case((user_robot_data_condition(User), 1), else_=0)).label("robot_count"),
        )
        .select_from(Order)
        .join(User, User.id == Order.user_id)
        .where(non_live_order_predicate)
        .group_by(Order.market_id)
    )
    order_counts_by_market: dict[int, dict[str, int]] = {}
    for market_id, retention_count, customer_count, robot_count in order_rows.all():
        retention_count_int = int(retention_count or 0)
        customer_count_int = int(customer_count or 0)
        robot_count_int = int(robot_count or 0)
        order_counts_by_market[int(market_id)] = {
            "retention_eligible": retention_count_int,
            "customer_retention_eligible": customer_count_int,
            "robot_retention_eligible": robot_count_int,
            "system_retention_eligible": max(retention_count_int - customer_count_int - robot_count_int, 0),
        }
        order_counts_by_market[int(market_id)]["prune_candidate"] = (
            order_counts_by_market[int(market_id)]["robot_retention_eligible"]
            + order_counts_by_market[int(market_id)]["system_retention_eligible"]
        )

    market_taker_user = aliased(User)
    market_maker_user = aliased(User)
    trade_rows = await session.execute(
        select(
            Trade.market_id,
            func.count().label("trade_total"),
            func.sum(
                case(
                    (
                        or_(
                            user_customer_data_condition(market_taker_user),
                            user_customer_data_condition(market_maker_user),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("customer_count"),
            func.sum(
                case(
                    (
                        and_(
                            user_robot_data_condition(market_taker_user),
                            user_robot_data_condition(market_maker_user),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("robot_count"),
        )
        .select_from(Trade)
        .join(market_taker_user, market_taker_user.id == Trade.taker_user_id)
        .join(market_maker_user, market_maker_user.id == Trade.maker_user_id)
        .group_by(Trade.market_id)
    )
    trade_counts_by_market: dict[int, dict[str, int]] = {}
    for market_id, market_trade_total, market_customer_trades, market_robot_trades in trade_rows.all():
        trade_total_int = int(market_trade_total or 0)
        customer_trade_int = int(market_customer_trades or 0)
        robot_trade_int = int(market_robot_trades or 0)
        trade_counts_by_market[int(market_id)] = {
            "total": trade_total_int,
            "customer_involved": customer_trade_int,
            "robot_only": robot_trade_int,
            "system_or_unknown": max(trade_total_int - customer_trade_int - robot_trade_int, 0),
        }
        trade_counts_by_market[int(market_id)]["prune_candidate"] = (
            trade_counts_by_market[int(market_id)]["robot_only"]
            + trade_counts_by_market[int(market_id)]["system_or_unknown"]
        )

    robot_source_taker_user = aliased(User)
    robot_source_maker_user = aliased(User)
    robot_source_rows = await session.execute(
        select(
            Trade.market_id,
            Trade.source,
            func.count().label("trade_count"),
            func.sum(Trade.quote_amount).label("quote_amount"),
            func.sum(Trade.maker_fee).label("maker_fee"),
            func.sum(Trade.taker_fee).label("taker_fee"),
        )
        .select_from(Trade)
        .join(robot_source_taker_user, robot_source_taker_user.id == Trade.taker_user_id)
        .join(robot_source_maker_user, robot_source_maker_user.id == Trade.maker_user_id)
        .where(
            and_(
                user_robot_data_condition(robot_source_taker_user),
                user_robot_data_condition(robot_source_maker_user),
            )
        )
        .group_by(Trade.market_id, Trade.source)
    )
    robot_sources_by_market: dict[int, list[dict]] = {}
    robot_source_items: list[dict] = []
    for market_id, source, count, quote_amount, maker_fee, taker_fee in robot_source_rows.all():
        market_id_int = int(market_id)
        meta = market_meta.get(market_id_int, {})
        item = {
            "symbol": meta.get("symbol"),
            "product_type": meta.get("product_type"),
            "source": source or "unknown",
            "trade_count": int(count or 0),
            "quote_amount": decimal_to_str(quote_amount or Decimal("0")),
            "maker_fee": decimal_to_str(maker_fee or Decimal("0")),
            "taker_fee": decimal_to_str(taker_fee or Decimal("0")),
        }
        robot_sources_by_market.setdefault(market_id_int, []).append(
            {key: value for key, value in item.items() if key not in {"symbol", "product_type"}}
        )
        robot_source_items.append(item)

    for items in robot_sources_by_market.values():
        items.sort(key=lambda item: (int(item["trade_count"]), item["source"]), reverse=True)

    market_items: list[dict] = []
    for market_id, symbol, product_type in market_rows:
        market_id_int = int(market_id)
        order_counts = order_counts_by_market.get(
            market_id_int,
            {
                "retention_eligible": 0,
                "customer_retention_eligible": 0,
                "robot_retention_eligible": 0,
                "system_retention_eligible": 0,
                "prune_candidate": 0,
            },
        )
        trade_counts = trade_counts_by_market.get(
            market_id_int,
            {
                "total": 0,
                "customer_involved": 0,
                "robot_only": 0,
                "system_or_unknown": 0,
                "prune_candidate": 0,
            },
        )
        market_items.append(
            {
                "symbol": symbol,
                "product_type": product_type,
                "orders": order_counts,
                "trades": trade_counts,
                "robot_trade_sources": robot_sources_by_market.get(market_id_int, [])[:5],
                "pressure_score": order_counts["robot_retention_eligible"] + trade_counts["robot_only"],
            }
        )

    robot_source_items.sort(key=lambda item: (int(item["trade_count"]), item["source"]), reverse=True)

    return {
        "orders": {
            "total": int(order_total),
            "live": int(open_order_total),
            "retention_eligible": retention_eligible_orders,
            "customer_retention_eligible": customer_order_retention,
            "robot_retention_eligible": robot_order_retention,
            "system_retention_eligible": system_order_retention,
            "prune_candidate": order_prune_candidate,
        },
        "trades": {
            "total": int(trade_total),
            "customer_involved": customer_trade_count,
            "robot_only": robot_trade_count,
            "system_or_unknown": system_trade_count,
            "prune_candidate": trade_prune_candidate,
        },
        "markets": sorted(
            market_items,
            key=lambda item: (int(item["pressure_score"]), int(item["trades"]["total"]), item["symbol"]),
            reverse=True,
        ),
        "robot_trade_sources": robot_source_items[:20],
        "policy": {
            "customer_records": "full_preserve",
            "robot_records": "aggregation_or_short_retention_candidate",
            "system_records": "control_evidence_or_unknown_source",
            "source_of_truth_unchanged": True,
        },
    }


def coordinator_orderbook_depth(config: dict | None) -> int:
    if not isinstance(config, dict):
        return 20
    candidates = [
        config.get("flow_min_book_levels_per_side"),
        config.get("levels_per_side"),
        config.get("levels"),
    ]
    for value in candidates:
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            continue
        if numeric > 0:
            return max(20, numeric)
    return 20


async def build_strategy_runtime_bundle(session: AsyncSession, request: Request, market: Market) -> dict:
    config, template, effective = await current_strategy_config(session, request, market)
    rows = await session.execute(
        select(MarketBotAccount)
        .where(MarketBotAccount.market_id == market.id, MarketBotAccount.is_enabled.is_(True))
        .order_by(MarketBotAccount.id.asc())
    )
    bots = [
        await serialize_market_bot_account(
            session,
            market,
            bot,
            include_api_secret=True,
            request=request,
        )
        for bot in rows.scalars()
    ]
    if platform_durable_contract():
        # 已退役的旧绑定只保留历史关联，不能重新进入策略执行身份。
        bots = [bot for bot in bots if str(bot.get("strategy_role") or "") != "paper_default"]
    makers = [bot for bot in bots if bot.get("role") == "maker" and (
        (bot.get("strategy_role") == "CONTRACT_LADDER") == (config.strategy_key in INTERNAL_MAKER_STRATEGIES))]
    flows = [bot for bot in bots if bot.get("role") == "flow"]
    warnings: list[str] = []
    if not makers:
        warnings.append("当前币对没有启用的 maker 机器人，策略不能形成做市盘口。")
    if market.product_type == PRODUCT_TYPE_SPOT and config.strategy_key == "LITE" and len(makers) < 2:
        warnings.append("Lite 建议至少 2 个 maker；单账户可运行但无法形成多账户分层。")
    runtime = request.app.state.runtime
    orderbook, orderbook_seq, orderbook_ts = await runtime.orderbook_snapshot(
        market.symbol,
        coordinator_orderbook_depth(effective),
    )
    instance = await session.scalar(select(MarketMakerInstance).where(MarketMakerInstance.market_id == market.id))
    instance_snapshot = maker_instance_status(
        market.symbol,
        request,
        configured_strategy_version=maker_instance_strategy_key(market, config.strategy_key),
        instance=instance,
    )
    metrics = deepcopy(getattr(runtime, "liquidity_metrics", {}).get(market.symbol, {}) or {})
    contract_snapshots = getattr(runtime, "contract_price_snapshots", {})
    coordinator = build_symbol_coordinator_state(
        market=market,
        strategy_config=effective,
        liquidity_metrics=metrics,
        orderbook=orderbook,
        instance_status=instance_snapshot,
        bots=bots,
        contract_price_snapshot=contract_snapshots.get(market.symbol.upper(), {}) if isinstance(contract_snapshots, dict) else {},
        source_counts=recent_trade_source_counts(request, market.symbol),
        persistence_metrics=(
            getattr(getattr(runtime, "persistence_writer", None), "metrics_snapshot", lambda: {})()
        ),
    )
    independent = getattr(request.app.state, "independent_flow", None)
    if independent is not None:
        doc = independent.configs.get(market.symbol, {})
        fc = doc.get("config", {})
        metric = independent.metrics.get(market.symbol, {})
        healthy = independent.process is not None and independent.process.returncode is None and time.monotonic()-independent.heartbeat < 5
        enabled = bool(fc.get("enabled"))
        healthy = healthy and market.symbol not in independent.blocked
        coordinator["flow"].update(mode=fc.get("mode", "off"), running=enabled and healthy,
            status="running" if enabled and healthy else "paused" if enabled else "stopped",
            allowed=enabled and healthy, flow_allowed=enabled and healthy,
            pause_reason=metric.get("reason") or ("FLOW 独立进程未就绪" if enabled and not healthy else None),
            effective_execution_mode=fc.get("mode", "off"), financial_effect=fc.get("mode")=="real_ioc_sandbox",
            source_policy="independent_flow", attempts=metric.get("attempts",0))
    robot_cards = build_symbol_robot_cards(
        market=market,
        instance_status=instance_snapshot,
        bots=bots,
        coordinator=coordinator,
        log_lines=instance_snapshot.get("last_log_lines") if isinstance(instance_snapshot.get("last_log_lines"), list) else [],
    )
    compat = {}
    legacy_key = "PERP_MM" if market.product_type == PRODUCT_TYPE_PERP else "LITE"
    if legacy_key in SUPPORTED_STRATEGY_KEYS:
        compat["perp_config" if legacy_key == "PERP_MM" else "lite_config"] = (await effective_strategy_config(session, request, market, legacy_key))[2]
    return {
        "symbol": market.symbol,
        "market": serialize_market(market),
        "strategy": serialize_market_strategy_config(config, template, effective_config=effective),
        "bots": bots,
        "accounts": {
            "makers": makers,
            "flows": flows,
        },
        "coordinator": coordinator,
        "orderbook_invariants": {
            **runtime.market_data.orderbook_invariant_snapshot(market.symbol),
            **runtime.orderbook_reconcile_snapshot(market.symbol),
            "snapshot_seq": orderbook_seq,
            "snapshot_ts": orderbook_ts,
        },
        "robot_cards": robot_cards,
        "warnings": warnings,
        "compat": compat,
    }


def build_strategy_apply_status(
    market: Market,
    instance_status: dict,
    *,
    previous_strategy_key: str | None,
    next_strategy_key: str,
    config_changed: bool,
) -> dict:
    running = bool(instance_status.get("running")) and instance_status.get("status") not in {"stale", "stopped", "error"}
    previous = previous_strategy_key or next_strategy_key
    if not running:
        state = "instance_not_running"
        label = "实例未运行"
        detail = "策略已保存；下次启动做市实例时会读取这套参数。"
    elif market.product_type == PRODUCT_TYPE_PERP:
        state = "waiting_next_cycle"
        label = "等待下一轮生效"
        detail = f"运行中的 {next_strategy_key} 会约 1 秒轮询 runtime bundle；深度、层数、单笔上限等参数会在下一轮报价规划中生效。"
    elif previous != next_strategy_key:
        state = "waiting_strategy_switch"
        label = "等待热切换"
        detail = f"运行中的 mm_service 会在轮询 runtime bundle 后切换到 {next_strategy_key}，并清理旧策略挂单。"
    elif config_changed:
        state = "waiting_next_cycle"
        label = "等待下一轮生效"
        detail = "运行中的 Lite/PERP_MM 会在约 1 秒内拉取新参数，并在下一轮报价规划中生效。"
    else:
        state = "already_current"
        label = "当前已是该策略"
        detail = "策略选择未变化；运行实例继续使用当前配置。"
    return {
        "state": state,
        "label": label,
        "detail": detail,
        "running": running,
        "previous_strategy_key": previous,
        "strategy_key": next_strategy_key,
        "instance_status": instance_status.get("status"),
    }


def _decimal_from_bundle_value(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except Exception:
        return Decimal("0")


def readiness_item(code: str, label: str, detail: str) -> dict:
    return {"code": code, "label": label, "detail": detail}


def build_maker_instance_start_readiness(market: Market, bundle: dict) -> dict:
    accounts = bundle.get("accounts", {}) if isinstance(bundle.get("accounts"), dict) else {}
    bots = bundle.get("bots", []) if isinstance(bundle.get("bots"), list) else []
    makers = accounts.get("makers", []) if isinstance(accounts.get("makers"), list) else []
    flows = accounts.get("flows", []) if isinstance(accounts.get("flows"), list) else []
    strategy = bundle.get("strategy", {}) if isinstance(bundle.get("strategy"), dict) else {}
    strategy_key = normalize_strategy_key(strategy.get("strategy_key") or "LITE")
    displayed_strategy_key = maker_instance_strategy_key(market, strategy_key) or strategy_key

    blockers: list[dict] = []
    warnings: list[dict] = []
    if not market.is_active:
        blockers.append(readiness_item("market_inactive", "市场未启用", "该币对当前 is_active=false，启动做市实例前需要先恢复交易。"))
    if not makers:
        blockers.append(readiness_item("no_enabled_maker", "执行身份尚未初始化", "点击启动时会自动分配执行身份及所需凭据，再完成预检；无需手动创建机器人。"))

    internal_ladder = market.product_type == PRODUCT_TYPE_PERP and displayed_strategy_key in INTERNAL_MAKER_STRATEGIES
    for bot in makers:
        if internal_ladder:
            if bot.get("strategy_role") != "CONTRACT_LADDER":
                blockers.append(readiness_item("invalid_internal_binding", "内部铺单身份不匹配", "内部铺单必须绑定本市场的专用执行身份。"))
            continue
        username = bot.get("username") or bot.get("bot_label") or f"uid={bot.get('uid')}"
        if not bot.get("api_key") or not (bot.get("api_secret_present") or bot.get("api_secret")):
            blockers.append(readiness_item("missing_api_credentials", "机器人 API 凭据不完整", f"{username} 缺少 api_key 或 api_secret。"))
        if _decimal_from_bundle_value(bot.get("initial_quote_amount")) <= Decimal("0"):
            blockers.append(readiness_item("missing_initial_quote", "初始 Quote 快照异常", f"{username} initial_quote_amount 必须大于 0。"))
        if market.product_type == PRODUCT_TYPE_SPOT and _decimal_from_bundle_value(bot.get("initial_base_amount")) <= Decimal("0"):
            blockers.append(readiness_item("missing_initial_base", "初始 Base 快照异常", f"{username} initial_base_amount 必须大于 0。"))
        if _decimal_from_bundle_value(bot.get("reference_price")) <= Decimal("0"):
            blockers.append(readiness_item("missing_reference_price", "参考价格异常", f"{username} reference_price 必须大于 0。"))
        uid = bot.get("uid")
        if uid is not None:
            uid_ok, uid_detail = uid_rule_status(int(uid), "mm_bot")
            if not uid_ok:
                warnings.append(readiness_item("legacy_bot_uid", "机器人 UID 不符合新规则", f"{username} {uid_detail}；旧账号可继续运行，新建机器人会自动使用 9 开头。"))
        if market.product_type == PRODUCT_TYPE_PERP:
            contract_account = bot.get("contract_account") if isinstance(bot.get("contract_account"), dict) else {}
            wallet = _decimal_from_bundle_value(contract_account.get("wallet_balance"))
            available = _decimal_from_bundle_value(contract_account.get("available_margin"))
            if wallet <= Decimal("0"):
                blockers.append(readiness_item("missing_contract_wallet", "合约钱包为空", f"{username} 合约钱包余额必须大于 0，PERP 做市不能依赖现货 BTC/USDT 余额。"))
            elif available <= Decimal("0"):
                warnings.append(readiness_item("empty_contract_margin", "可用保证金为 0", f"{username} 当前可用保证金为 0，新挂单可能被风控拒绝。"))
        else:
            quote_balance = bot.get("quote_balance") if isinstance(bot.get("quote_balance"), dict) else {}
            base_balance = bot.get("base_balance") if isinstance(bot.get("base_balance"), dict) else {}
            if _decimal_from_bundle_value(quote_balance.get("available")) + _decimal_from_bundle_value(quote_balance.get("frozen")) <= Decimal("0"):
                warnings.append(readiness_item("empty_quote_balance", "Quote 当前余额为 0", f"{username} 当前 {market.quote_asset} 余额为 0，买盘可能无法形成。"))
            if _decimal_from_bundle_value(base_balance.get("available")) + _decimal_from_bundle_value(base_balance.get("frozen")) <= Decimal("0"):
                warnings.append(readiness_item("empty_base_balance", "Base 当前余额为 0", f"{username} 当前 {market.base_asset} 余额为 0，卖盘可能无法形成。"))

    if market.product_type == PRODUCT_TYPE_PERP and not internal_ladder and len(makers) < 2:
        warnings.append(readiness_item("perp_low_maker_count", "PERP maker 数偏少", "合约做市建议至少 2 个 maker 账户，便于买卖侧隔离和 ADL/保证金观察。"))
    if market.product_type == PRODUCT_TYPE_SPOT and strategy_key == "LITE" and len(makers) < 2:
        warnings.append(readiness_item("lite_low_maker_count", "Lite maker 数偏少", "Lite 建议至少 2 个 maker；单账户可运行但无法形成多账户分层。"))

    for warning in bundle.get("warnings", []) if isinstance(bundle.get("warnings"), list) else []:
        if isinstance(warning, str) and warning:
            warnings.append(readiness_item("runtime_bundle_warning", "Runtime bundle 提示", warning))

    return {
        "ok": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "summary": {
            "strategy_key": displayed_strategy_key,
            "product_type": market.product_type,
            "enabled_bots": len(bots),
            "enabled_makers": len(makers),
            "enabled_flows": len(flows),
            "market_active": bool(market.is_active),
        },
    }


async def create_market_bot_account(
    session: AsyncSession,
    market: Market,
    payload: MarketBotCreateRequest,
    *,
    index: int,
) -> MarketBotAccount:
    bind_user_id = payload.uid
    if bind_user_id is not None:
        from app.services.strategy_accounts import validate_uid
        await validate_uid(session, bind_user_id, market.id, payload.role)
    bot_label = await unique_market_bot_label(session, market, payload.bot_label, index)
    reference_price = resolve_bot_reference_price(market, payload.reference_price)
    initial_base_amount = resolve_bot_base_amount(
        market,
        reference_price,
        payload.initial_base_notional,
        payload.initial_base_amount,
    )
    if bind_user_id is not None:
        user = await session.scalar(select(User).where(User.id == bind_user_id))
        if user is None:
            raise HTTPException(status_code=404, detail="uid user not found")
        if user.role != "mm_bot":
            raise HTTPException(status_code=400, detail="uid user must be mm_bot")
        if not user.is_active:
            raise HTTPException(status_code=400, detail="uid user is inactive")
        existing_binding = await session.scalar(
            select(MarketBotAccount.id).where(
                MarketBotAccount.market_id == market.id,
                MarketBotAccount.user_id == user.id,
            )
        )
        if existing_binding is not None:
            raise HTTPException(status_code=409, detail="uid is already bound to this market")
        override_api_key = await ensure_unique_api_key(session, payload.api_key, exclude_user_id=user.id)
        if override_api_key is not None:
            user.api_key = override_api_key
        elif not user.api_key:
            user.api_key = generate_api_key(user.username.replace("_", "-")[:18] or "bot")
        if payload.api_secret is not None and payload.api_secret.strip():
            user.api_secret_hash = payload.api_secret.strip()[:255]
        elif not user.api_secret_hash:
            user.api_secret_hash = generate_api_secret()
        if payload.password is not None and payload.password.strip():
            user.password_hash = hash_password(payload.password)
    else:
        username = await unique_market_bot_username(session, market.symbol, payload.username, index)
        api_key = await ensure_unique_api_key(session, payload.api_key)
        user = User(
            id=await next_uid_for_role(session, "mm_bot"),
            username=username,
            role="mm_bot",
            api_key=api_key or generate_api_key(username.replace("_", "-")[:18] or "bot"),
            api_secret_hash=normalize_bot_text(payload.api_secret, "", 255) or generate_api_secret(),
            password_hash=hash_password(payload.password or DEFAULT_MARKET_BOT_PASSWORD),
            is_active=True,
        )
        session.add(user)
        await session.flush()

    now = datetime.now(tz=UTC)
    if market.product_type == PRODUCT_TYPE_PERP:
        await ensure_contract_bot_account(
            session,
            user,
            market=market,
            margin_asset=market.margin_asset or market.quote_asset,
            wallet_balance=payload.initial_quote_amount,
            note=f"contract_market_bot_create:{market.symbol}",
            now=now,
        )
    else:
        await ensure_user_asset_template(
            session,
            user,
            market.quote_asset,
            payload.initial_quote_amount,
            note=f"market_bot_create:{market.symbol}",
            now=now,
            update_existing_template=True,
        )
        await ensure_user_asset_template(
            session,
            user,
            market.base_asset,
            initial_base_amount,
            note=f"market_bot_create:{market.symbol}",
            now=now,
            update_existing_template=True,
        )
    await ensure_fee_profile(session, user.id, market.id, DEFAULT_MARKET_BOT_MAKER_FEE, DEFAULT_MARKET_BOT_TAKER_FEE)

    bot = MarketBotAccount(
        market_id=market.id,
        user_id=user.id,
        bot_label=bot_label,
        role=payload.role,
        strategy_role=normalize_bot_text(payload.strategy_role, payload.role, 32),
        initial_quote_amount=payload.initial_quote_amount,
        initial_base_amount=initial_base_amount,
        initial_base_notional=payload.initial_base_notional,
        reference_price=reference_price,
        is_enabled=payload.is_enabled,
    )
    session.add(bot)
    await session.flush()
    return bot


async def create_default_market_bots(
    session: AsyncSession,
    market: Market,
    *,
    bot_count: int,
    initial_quote_amount: Decimal,
    initial_base_notional: Decimal,
    reference_price: Decimal | None,
) -> list[MarketBotAccount]:
    existing_count = await session.scalar(
        select(func.count()).select_from(MarketBotAccount).where(MarketBotAccount.market_id == market.id)
    )
    created: list[MarketBotAccount] = []
    start_index = int(existing_count or 0) + 1
    for index in range(start_index, bot_count + 1):
        payload = MarketBotCreateRequest(
            username=default_market_bot_username(market.symbol, index),
            bot_label=f"{market.symbol}-MM-{index}",
            role="maker",
            strategy_role="maker",
            initial_quote_amount=initial_quote_amount,
            initial_base_notional=initial_base_notional,
            reference_price=reference_price,
            is_enabled=True,
        )
        created.append(await create_market_bot_account(session, market, payload, index=index))
    return created


async def ensure_default_market_flow_bot(
    session: AsyncSession,
    market: Market,
    *,
    initial_quote_amount: Decimal,
    initial_base_notional: Decimal,
    reference_price: Decimal | None,
    is_enabled: bool,
) -> tuple[MarketBotAccount, bool]:
    existing = await session.scalar(
        select(MarketBotAccount)
        .where(MarketBotAccount.market_id == market.id, MarketBotAccount.role == "flow")
        .order_by(MarketBotAccount.id.asc())
    )
    if existing is not None:
        if is_enabled and not existing.is_enabled:
            existing.is_enabled = True
        return existing, False

    existing_count = int(
        await session.scalar(
            select(func.count()).select_from(MarketBotAccount).where(MarketBotAccount.market_id == market.id)
        )
        or 0
    )
    payload = MarketBotCreateRequest(
        username=default_market_flow_username(market.symbol),
        bot_label=f"{market.symbol}-FLOW-1",
        role="flow",
        strategy_role="flow",
        initial_quote_amount=initial_quote_amount,
        initial_base_notional=initial_base_notional,
        reference_price=reference_price,
        is_enabled=is_enabled,
    )
    return await create_market_bot_account(session, market, payload, index=existing_count + 1), True


async def sync_market_bot_reset_templates(
    session: AsyncSession,
    market: Market,
    bot: MarketBotAccount,
    *,
    note: str,
    now: datetime,
) -> None:
    user = await session.scalar(select(User).where(User.id == bot.user_id))
    if user is None:
        raise HTTPException(status_code=500, detail="market bot user missing")
    await ensure_user_asset_template(
        session,
        user,
        market.quote_asset,
        Decimal(bot.initial_quote_amount),
        note=note,
        now=now,
        update_existing_template=True,
    )
    await ensure_user_asset_template(
        session,
        user,
        market.base_asset,
        Decimal(bot.initial_base_amount),
        note=note,
        now=now,
        update_existing_template=True,
    )


def summarize_level_owner(rows: list[tuple[str, str]]) -> dict:
    if not rows:
        return {"type": "none", "label": "--", "usernames": []}
    usernames = [username for username, _ in rows]
    roles = {role for _, role in rows}
    if roles == {"mm_bot"}:
        owner_type = "bot"
        label = "机器人"
    elif roles == {"manual_user"}:
        owner_type = "user"
        label = "用户"
    else:
        owner_type = "mixed"
        label = "混合"
    if usernames:
        label = f"{label} · {usernames[0]}" if len(usernames) == 1 else f"{label} · {usernames[0]} +{len(usernames) - 1}"
    return {"type": owner_type, "label": label, "usernames": usernames}


async def fetch_level_owner_summary(
    session: AsyncSession,
    market_id: int,
    side: str,
    price_value: str | None,
) -> dict:
    if not price_value:
        return {"type": "none", "label": "--", "usernames": []}
    rows = await session.execute(
        select(User.username, User.role)
        .join(Order, Order.user_id == User.id)
        .where(
            Order.market_id == market_id,
            Order.status.in_(LIVE_ORDER_STATUSES),
            Order.side == side,
            Order.price == Decimal(str(price_value)),
        )
        .order_by(User.username.asc())
    )
    return summarize_level_owner(list(rows.all()))


def summarize_orderbook_snapshot(snapshot: dict) -> dict:
    bids = snapshot.get("bids", [])
    asks = snapshot.get("asks", [])
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    spread_quote = None
    crossed = False
    if best_bid is not None and best_ask is not None:
        spread_quote = decimal_to_str(Decimal(str(best_ask)) - Decimal(str(best_bid)))
        crossed = Decimal(str(best_bid)) >= Decimal(str(best_ask))
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_quote": spread_quote,
        "crossed": crossed,
        "bid_level_count": len(bids),
        "ask_level_count": len(asks),
        "top_bids": bids[:10],
        "top_asks": asks[:10],
    }


def build_liquidity_diagnosis(
    metrics: dict,
    book_summary: dict,
    bid_owner: dict,
    ask_owner: dict,
) -> list[str]:
    issues: list[str] = []
    if book_summary["bid_level_count"] == 0:
        issues.append("本地买盘为空")
    if book_summary["ask_level_count"] == 0:
        issues.append("本地卖盘为空")
    if book_summary["crossed"]:
        issues.append("本地盘口已交叉")

    our_best_bid = metrics.get("our_best_bid")
    our_best_ask = metrics.get("our_best_ask")
    if our_best_bid and our_best_ask and Decimal(str(our_best_bid)) >= Decimal(str(our_best_ask)):
        issues.append("我方顶层报价已交叉")

    health = metrics.get("health", {}) or {}
    spread_bps = health.get("spread_bps")
    if spread_bps is not None and Decimal(str(spread_bps)) > Decimal("3"):
        issues.append(f"当前点差偏大: {spread_bps} bps")

    local_book_stale_ms = health.get("local_book_stale_ms")
    if isinstance(local_book_stale_ms, (int, float)) and local_book_stale_ms > 1500:
        issues.append(f"本地盘口延迟偏高: {local_book_stale_ms} ms")

    external_stale_ms = health.get("external_stale_ms")
    if isinstance(external_stale_ms, (int, float)) and external_stale_ms > 1500:
        issues.append(f"外部行情延迟偏高: {external_stale_ms} ms")

    vulnerable_side = metrics.get("vulnerable_side")
    if vulnerable_side == "buy" and ask_owner.get("type") == "user":
        issues.append("当前不利侧在卖盘，但市场卖一由用户单占据")
    if vulnerable_side == "sell" and bid_owner.get("type") == "user":
        issues.append("当前不利侧在买盘，但市场买一由用户单占据")

    if not issues:
        issues.append("未发现显著结构性异常，请结合近期行为和日志继续分析")
    return issues


def surveillance_check(code: str, severity: str, label: str, detail: str) -> dict:
    return {"code": code, "severity": severity, "label": label, "detail": detail}


async def build_market_surveillance_item(request: Request, session: AsyncSession, market: Market) -> dict:
    runtime = request.app.state.runtime
    snapshot, _, _ = await runtime.orderbook_snapshot(market.symbol, 100)
    book_summary = summarize_orderbook_snapshot(snapshot)
    stats = runtime.market_data.compute_stats(market.symbol, snapshot)
    bid_owner = await fetch_level_owner_summary(session, market.id, "buy", book_summary["best_bid"])
    ask_owner = await fetch_level_owner_summary(session, market.id, "sell", book_summary["best_ask"])

    open_order_rows = await session.execute(
        select(Order.side, User.username, User.role, Order.price, Order.remaining_quantity)
        .join(User, User.id == Order.user_id)
        .where(Order.market_id == market.id, Order.status.in_(LIVE_ORDER_STATUSES))
        .order_by(Order.side.asc(), User.username.asc())
    )
    role_summary: dict[tuple[str, str], dict] = {}
    user_summary: dict[str, dict] = {}
    total_open_orders = 0
    total_open_notional = Decimal("0")
    for side, username, role, price, remaining_quantity in open_order_rows.all():
        price_value = Decimal(price or 0)
        quantity_value = Decimal(remaining_quantity or 0)
        notional = price_value * quantity_value
        total_open_orders += 1
        total_open_notional += notional

        role_key = (side, role)
        role_item = role_summary.setdefault(
            role_key,
            {"side": side, "role": role, "open_order_count": 0, "remaining_quantity": Decimal("0"), "notional": Decimal("0")},
        )
        role_item["open_order_count"] += 1
        role_item["remaining_quantity"] += quantity_value
        role_item["notional"] += notional

        user_item = user_summary.setdefault(
            username,
            {"username": username, "role": role, "open_order_count": 0, "remaining_quantity": Decimal("0"), "notional": Decimal("0")},
        )
        user_item["open_order_count"] += 1
        user_item["remaining_quantity"] += quantity_value
        user_item["notional"] += notional

    since = datetime.now(tz=UTC) - timedelta(hours=24)
    trade_count_24h = await session.scalar(
        select(func.count())
        .select_from(Trade)
        .where(Trade.market_id == market.id, Trade.executed_at >= since, Trade.source != BOOTSTRAP_SEED_SOURCE)
    )
    quote_volume_24h = await session.scalar(
        select(func.coalesce(func.sum(Trade.quote_amount), 0))
        .where(Trade.market_id == market.id, Trade.executed_at >= since, Trade.source != BOOTSTRAP_SEED_SOURCE)
    )

    checks: list[dict] = []
    if not market.is_active:
        checks.append(surveillance_check("market_paused", "critical", "市场暂停", "该市场当前拒绝新订单。"))
    if book_summary["bid_level_count"] == 0:
        checks.append(surveillance_check("empty_bid", "critical", "买盘为空", "没有可成交买盘，卖出市价单会无法成交。"))
    if book_summary["ask_level_count"] == 0:
        checks.append(surveillance_check("empty_ask", "critical", "卖盘为空", "没有可成交卖盘，买入市价单会无法成交。"))
    if book_summary["crossed"]:
        checks.append(surveillance_check("crossed_book", "critical", "盘口交叉", "买一大于或等于卖一，撮合或撤改单需要排查。"))
    spread_pct = Decimal(str(stats.get("spread_pct") or "0"))
    if spread_pct > Decimal("1"):
        checks.append(surveillance_check("wide_spread", "warn", "点差偏宽", f"当前点差约 {decimal_to_str(spread_pct)}%。"))
    if bid_owner.get("type") in {"user", "mixed"}:
        checks.append(surveillance_check("manual_bid_top", "warn", "买一含人工单", f"买一归属：{bid_owner.get('label') or '--'}。"))
    if ask_owner.get("type") in {"user", "mixed"}:
        checks.append(surveillance_check("manual_ask_top", "warn", "卖一含人工单", f"卖一归属：{ask_owner.get('label') or '--'}。"))
    if total_open_orders == 0:
        checks.append(surveillance_check("no_open_orders", "warn", "无活跃挂单", "当前市场没有任何活跃挂单。"))
    if Decimal(str(quote_volume_24h or "0")) <= Decimal("0"):
        checks.append(surveillance_check("no_24h_turnover", "warn", "24H 无成交额", "最近 24 小时没有成交额，K 线和成交检验样本不足。"))
    if not checks:
        checks.append(surveillance_check("structure_ok", "ok", "结构正常", "未发现显著盘口结构异常。"))

    if any(item["severity"] == "critical" for item in checks):
        status = "critical"
    elif any(item["severity"] == "warn" for item in checks):
        status = "warn"
    else:
        status = "ok"

    def serialize_decimal_summary(item: dict) -> dict:
        return {
            **{key: value for key, value in item.items() if key not in {"remaining_quantity", "notional"}},
            "remaining_quantity": decimal_to_str(item["remaining_quantity"]),
            "notional": decimal_to_str(item["notional"]),
        }

    top_users = sorted(user_summary.values(), key=lambda item: item["notional"], reverse=True)[:5]
    return {
        "symbol": market.symbol,
        "market_type": market.market_type,
        "is_active": market.is_active,
        "status": status,
        "ts": to_millis(datetime.now(tz=UTC)),
        "top": {"bid": bid_owner, "ask": ask_owner},
        "metrics": {
            "best_bid": book_summary["best_bid"],
            "best_ask": book_summary["best_ask"],
            "spread_quote": book_summary["spread_quote"],
            "spread_pct": stats["spread_pct"],
            "depth_amount_0_5pct": stats["depth_amount_0_5pct"],
            "depth_amount_2pct": stats["depth_amount_2pct"],
            "book_imbalance": stats["book_imbalance"],
            "bid_level_count": book_summary["bid_level_count"],
            "ask_level_count": book_summary["ask_level_count"],
            "open_order_count": total_open_orders,
            "open_order_notional": decimal_to_str(total_open_notional),
            "trade_count_24h": int(trade_count_24h or 0),
            "quote_volume_24h": decimal_to_str(Decimal(str(quote_volume_24h or "0"))),
        },
        "open_orders_by_role": [
            serialize_decimal_summary(item)
            for item in sorted(role_summary.values(), key=lambda value: (value["side"], value["role"]))
        ],
        "top_open_order_users": [serialize_decimal_summary(item) for item in top_users],
        "checks": checks,
    }


@router.get("/admin/users")
async def list_users(
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    users = await session.execute(select(User).order_by(User.id.asc()))
    balances = await session.execute(select(Balance))
    grouped: dict[int, list[dict]] = {}
    for item in balances.scalars():
        grouped.setdefault(item.user_id, []).append(
            {"asset": item.asset, "available": str(item.available), "frozen": str(item.frozen)}
        )
    fee_rows = await session.execute(
        select(FeeProfile, Market.symbol)
        .join(Market, Market.id == FeeProfile.market_id)
        .order_by(Market.symbol.asc())
    )
    fee_grouped: dict[int, list[dict]] = {}
    for profile, symbol in fee_rows.all():
        fee_grouped.setdefault(profile.user_id, []).append(
            {
                "symbol": symbol,
                "maker_fee_rate": str(profile.maker_fee_rate),
                "taker_fee_rate": str(profile.taker_fee_rate),
            }
        )
    return {
        "items": [
            serialize_user(user, grouped.get(user.id, []), fee_grouped.get(user.id, []))
            for user in users.scalars()
        ]
    }


@router.get("/admin/deployment-checklist")
async def get_deployment_checklist(
    request: Request,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    rows = await session.execute(select(User).order_by(User.id.asc()))
    users = list(rows.scalars())
    active_users = [user for user in users if user.is_active]
    active_admin_count = sum(1 for user in active_users if user.role == "admin")
    default_password_users = [
        user.username
        for user in active_users
        if user.username in DEFAULT_WEB_PASSWORDS and verify_password(DEFAULT_WEB_PASSWORDS[user.username], user.password_hash)
    ]
    demo_api_users = [
        user.username
        for user in active_users
        if (user.api_key or "").endswith("-demo-key") or (user.api_secret_hash or "").endswith("-demo-secret")
    ]
    market_rows = await session.execute(select(Market).where(Market.is_active.is_(True)).order_by(Market.symbol.asc()))
    active_markets = list(market_rows.scalars())
    empty_book_symbols: list[str] = []
    no_trade_symbols: list[str] = []
    runtime = request.app.state.runtime
    for market in active_markets:
        snapshot, _, _ = await runtime.orderbook_snapshot(market.symbol, 1)
        if not snapshot["bids"] or not snapshot["asks"]:
            empty_book_symbols.append(market.symbol)
        trade_count = await session.scalar(select(func.count()).select_from(Trade).where(Trade.market_id == market.id))
        if not trade_count:
            no_trade_symbols.append(market.symbol)
    checks: list[dict] = []

    checks.append(
        deployment_check(
            "active_admin",
            "ok" if active_admin_count >= 1 else "critical",
            "活跃管理员",
            f"当前活跃管理员数量：{active_admin_count}",
            "至少保留 1 个管理员账户，否则无法继续后台管理。",
        )
    )
    checks.append(
        deployment_check(
            "default_passwords",
            "critical" if default_password_users else "ok",
            "默认网页登录密码",
            "仍使用默认密码：" + ", ".join(default_password_users[:8]) + (" ..." if len(default_password_users) > 8 else "")
            if default_password_users
            else "未发现活跃预置账户仍使用默认网页登录密码。",
            "公网部署前重置管理员、人工账户和机器人账户网页登录密码。",
        )
    )
    checks.append(
        deployment_check(
            "demo_api_keys",
            "critical" if demo_api_users else "ok",
            "Demo API Key / Secret",
            "仍使用 demo API 凭据：" + ", ".join(demo_api_users[:8]) + (" ..." if len(demo_api_users) > 8 else "")
            if demo_api_users
            else "未发现活跃账户仍使用 demo API key/secret。",
            "公网部署前轮换 API key，或新建正式测试账户后停用 demo 账户。",
        )
    )
    is_sqlite = settings.database_url.startswith("sqlite")
    checks.append(
        deployment_check(
            "database_mode",
            "warn" if is_sqlite else "ok",
            "数据库模式",
            "当前使用 SQLite，适合本地演示和短期联调。" if is_sqlite else "当前不是 SQLite 单文件模式。",
            "云主机长期运行建议切 PostgreSQL；短期内部测试可继续 SQLite 并做好备份。",
        )
    )
    wildcard_cors = "*" in settings.cors_origins
    checks.append(
        deployment_check(
            "cors_origins",
            "warn" if wildcard_cors else "ok",
            "CORS 配置",
            "当前 CORS 允许所有来源。" if wildcard_cors else "当前 CORS 已限制来源。",
            "公网部署时建议按域名/IP 收紧；内部临时联调可保留通配。",
        )
    )
    checks.append(
        deployment_check(
            "private_rate_limit",
            "ok" if settings.private_order_rate_limit_enabled else "warn",
            "私有交易写入限流",
            (
                f"已启用：下单 {settings.order_submit_rate_per_second:g}/s burst {settings.order_submit_burst}，"
                f"撤单 {settings.order_cancel_rate_per_second:g}/s burst {settings.order_cancel_burst}。"
            )
            if settings.private_order_rate_limit_enabled
            else "当前未启用私有交易写入限流。",
            "4c6g 单机部署建议保持启用，机器人按 /account/connectivity 返回值自适应。",
        )
    )
    open_order_limits_enabled = (
        settings.max_open_orders_per_user_market > 0
        and settings.max_open_orders_per_user_total > 0
    )
    checks.append(
        deployment_check(
            "account_open_order_limits",
            "ok" if open_order_limits_enabled else "warn",
            "账户挂单上限",
            (
                f"已启用：单账户单市场 {settings.max_open_orders_per_user_market} 单，"
                f"单账户全市场 {settings.max_open_orders_per_user_total} 单。"
            )
            if open_order_limits_enabled
            else "当前未完整启用账户挂单上限。",
            "4c6g 单机部署建议保留挂单上限，避免外部机器人异常循环堆积订单。",
        )
    )
    checks.append(
        deployment_check(
            "active_markets",
            "ok" if active_markets else "critical",
            "活跃市场",
            f"当前活跃市场数量：{len(active_markets)}",
            "至少保留 1 个活跃市场；默认主流/独立币对可用于机器人和人工联调。",
        )
    )
    book_severity = "critical" if active_markets and len(empty_book_symbols) == len(active_markets) else "warn" if empty_book_symbols else "ok"
    checks.append(
        deployment_check(
            "market_orderbooks",
            book_severity,
            "活跃市场盘口",
            f"以下活跃市场缺少买盘或卖盘：{summarize_names(empty_book_symbols)}"
            if empty_book_symbols
            else "活跃市场均有双边盘口。",
            "上线给外部测试前，应先运行做市机器人或手工挂单，让目标市场至少有双边盘口。",
        )
    )
    checks.append(
        deployment_check(
            "market_trade_history",
            "warn" if no_trade_symbols else "ok",
            "市场成交历史",
            f"以下活跃市场暂无成交历史：{summarize_names(no_trade_symbols)}"
            if no_trade_symbols
            else "活跃市场均有成交历史。",
            "新建市场可先用机器人或人工小额对敲生成基础成交，再交给操作手检验。",
        )
    )
    critical = sum(1 for item in checks if item["severity"] == "critical")
    warn = sum(1 for item in checks if item["severity"] == "warn")
    status = "critical" if critical else "warn" if warn else "ok"
    return {
        "status": status,
        "summary": {"critical": critical, "warn": warn, "ok": sum(1 for item in checks if item["severity"] == "ok")},
        "checks": checks,
    }


@router.get("/admin/system-status")
async def get_system_status(
    request: Request,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    runtime = request.app.state.runtime
    now = datetime.now(tz=UTC)
    uptime_seconds = int((now - runtime.started_at).total_seconds())
    db_path = sqlite_database_path()
    db_mode = "sqlite" if settings.database_url.startswith("sqlite") else "external"
    order_total = await session.scalar(select(func.count()).select_from(Order))
    open_order_total = await session.scalar(
        select(func.count()).select_from(Order).where(Order.status.in_(LIVE_ORDER_STATUSES))
    )
    trade_total = await session.scalar(select(func.count()).select_from(Trade))
    ledger_total = await session.scalar(select(func.count()).select_from(LedgerEntry))
    market_total = await session.scalar(select(func.count()).select_from(Market))
    active_market_total = await session.scalar(select(func.count()).select_from(Market).where(Market.is_active.is_(True)))
    user_total = await session.scalar(select(func.count()).select_from(User))
    active_user_total = await session.scalar(select(func.count()).select_from(User).where(User.is_active.is_(True)))
    balance_total = await session.scalar(select(func.count()).select_from(Balance))
    market_rows_for_invariants = await session.execute(select(Market).order_by(Market.symbol.asc()))
    orderbook_invariants = []
    spot_order_service = request.app.state.order_service
    for market in market_rows_for_invariants.scalars():
        symbol = market.symbol
        async with runtime.market_locks[symbol]:
            db_rows = await session.execute(
                select(Order.order_id).where(
                    Order.market_id == market.id,
                    Order.type == ORDER_TYPE_LIMIT,
                    Order.tif == TIF_GTC,
                    Order.status.in_(LIVE_ORDER_STATUSES),
                    Order.remaining_quantity > Decimal("0"),
                )
            )
            db_live_ids = set(db_rows.scalars())
            book = runtime.engine.books.get(symbol)
            engine_ids = set(book.orders) if book is not None else set()
        engine_only = sorted(engine_ids - db_live_ids)
        db_only = sorted(db_live_ids - engine_ids)
        liquidity_metrics = runtime.liquidity_metrics.get(symbol, {}) or {}
        flow_ioc = liquidity_metrics.get("flow_ioc") if isinstance(liquidity_metrics.get("flow_ioc"), dict) else {}
        fast_mirror_invariant = None
        if (
            (settings.persistence_mode == "memory")
            and market.product_type == PRODUCT_TYPE_SPOT
        ):
            fast_mirror_invariant = await spot_order_service.fast_orderbook_invariant(symbol)
        orderbook_invariants.append(
            {
                "symbol": symbol,
                "product_type": market.product_type,
                "engine_open_order_count": len(engine_ids),
                "db_live_open_order_count": len(db_live_ids),
                "engine_only_count": len(engine_only),
                "db_only_count": len(db_only),
                "engine_only_sample": engine_only[:20],
                "db_only_sample": db_only[:20],
                "fast_mirror": fast_mirror_invariant,
                "sampled_runtime_authority": False,
                "sampled_engine_only_count": 0,
                "sampled_non_robot_engine_only_count": 0,
                "orderbook": runtime.market_data.orderbook_invariant_snapshot(symbol),
                "reconcile": runtime.orderbook_reconcile_snapshot(symbol),
                "flow": {
                    "action_queue_size": liquidity_metrics.get("action_queue_size"),
                    "active_action_size": liquidity_metrics.get("active_action_size"),
                    "action_load_size": liquidity_metrics.get("action_load_size"),
                    "in_flight": flow_ioc.get("in_flight"),
                    "skipped_queue": flow_ioc.get("skipped_queue"),
                    "last_status": flow_ioc.get("last_status"),
                },
            }
        )
    book_markets = []
    for symbol in sorted(runtime.engine.books):
        book = runtime.engine.books[symbol]
        snapshot, _, _ = await runtime.orderbook_snapshot(symbol, 100)
        book_markets.append(
            {
                "symbol": symbol,
                "open_orders": len(book.orders),
                "bid_levels": len(snapshot["bids"]),
                "ask_levels": len(snapshot["asks"]),
            }
        )
    public_ws_sockets = {socket for sockets in runtime.ws.public_subscriptions.values() for socket in sockets}
    private_ws_sockets = set(runtime.ws.authenticated_users)
    rss_mb = current_process_rss_mb()
    sqlite_size_mb = file_size_mb(db_path)
    warnings: list[str] = []
    runner_parent_watch = getattr(request.app.state, "runner_parent_watch", {"enabled": False})
    if (
        isinstance(runner_parent_watch, dict)
        and runner_parent_watch.get("enabled")
        and runner_parent_watch.get("status") == "parent_lost"
    ):
        warnings.append("sandbox runner 父进程已丢失；做市实例已停止，后端正在退出。")
    if rss_mb > 2048:
        warnings.append("进程峰值 RSS 已超过 2GB，4c6g 上需要降低机器人频率或重启观察。")
    if sqlite_size_mb is not None and sqlite_size_mb > 1024:
        warnings.append("SQLite 文件已超过 1GB，建议备份并评估切 PostgreSQL。")
    if int(open_order_total or 0) > 20000:
        warnings.append("当前活跃挂单超过 20000，单机撮合和前端监控压力会明显上升。")
    if int(trade_total or 0) > 1_000_000 and db_mode == "sqlite":
        warnings.append("SQLite 成交记录超过 100 万，建议归档历史或切 PostgreSQL。")
    if any(item["engine_only_count"] or item["db_only_count"] for item in orderbook_invariants):
        warnings.append("订单簿内存 open orders 与数据库 live orders 存在差异，请查看 orderbook_invariants。")
    if any(
        item.get("fast_mirror")
        and (
            item["fast_mirror"].get("engine_only_count")
            or item["fast_mirror"].get("fast_mirror_only_count")
            or item["fast_mirror"].get("content_mismatch_count")
        )
        for item in orderbook_invariants
    ):
        warnings.append("现货撮合簿与机器人实时镜像不一致，请查看 fast_mirror 的 only/content mismatch 指标。")
    if any(item["orderbook"]["same_seq_diff_count"] or item["orderbook"]["seq_rollback_count"] for item in orderbook_invariants):
        warnings.append("盘口版本 invariant 曾记录 same-seq-diff 或 seq rollback，请查看 orderbook_invariants。")
    exchange_diagnostics = runtime.exchange_diagnostics()
    if exchange_diagnostics.get("status") in {"HALTED", "DEGRADED"}:
        warnings.append("ExchangeCore 或关键持久化 sink 当前不健康，禁止自动 watcher 重启。")
    ws_metrics = runtime.ws.metrics_snapshot()
    if int(ws_metrics.get("send_timeouts") or 0) or int(ws_metrics.get("send_failures") or 0):
        warnings.append("WebSocket 发送存在 timeout/failure，慢连接已被丢弃。")
    retention_last_result = getattr(request.app.state, "history_retention_last_result", None)
    retention_last_error = getattr(request.app.state, "history_retention_last_error", None)
    data_retention_domains = getattr(request.app.state, "data_retention_domains", None)
    return {
        "status": "warn" if warnings else "ok",
        "ts": to_millis(now),
        "uptime_seconds": uptime_seconds,
        "process": {
            "pid": os.getpid(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "rss_mb": rss_mb,
        },
        "runner_parent_watch": runner_parent_watch,
        "database": {
            "mode": db_mode,
            "path": str(db_path) if db_path is not None else None,
            "size_mb": sqlite_size_mb,
        },
        "history_retention": {
            "enabled": settings.history_retention_enabled,
            "auto_enabled": history_retention_auto_enabled(),
            "sqlite_only": settings.history_retention_sqlite_only,
            "interval_seconds": settings.history_retention_interval_seconds,
            "config": {
                "order_keep_per_market": settings.history_retention_order_keep_per_market,
                "trade_keep_per_market": settings.history_retention_trade_keep_per_market,
                "kline_keep_per_market_interval": settings.history_retention_kline_keep_per_market_interval,
                "ledger_keep_per_user": settings.history_retention_ledger_keep_per_user,
                "contract_ledger_keep_per_user": settings.history_retention_contract_ledger_keep_per_user,
                "batch_size": settings.history_retention_batch_size,
                "customer_records_preserved": True,
                "financial_pruning_enabled": settings.history_retention_financial_pruning_enabled,
            },
            "last_result": retention_last_result,
            "last_error": retention_last_error,
        },
        "data_retention_domains": data_retention_domains,
        "counts": {
            "users": int(user_total or 0),
            "active_users": int(active_user_total or 0),
            "markets": int(market_total or 0),
            "active_markets": int(active_market_total or 0),
            "orders": int(order_total or 0),
            "open_orders": int(open_order_total or 0),
            "trades": int(trade_total or 0),
            "ledger_entries": int(ledger_total or 0),
            "balances": int(balance_total or 0),
            "book_markets": len(book_markets),
        },
        "websocket": {
            "public_connections": len(public_ws_sockets),
            "private_connections": len(private_ws_sockets),
            "public_subscription_keys": sum(1 for sockets in runtime.ws.public_subscriptions.values() if sockets),
            "private_subscription_keys": sum(1 for sockets in runtime.ws.private_subscriptions.values() if sockets),
            "metrics": ws_metrics,
        },
        "exchange": exchange_diagnostics,
        "robot_flow": {
            "persistence_switch_enabled": bool(settings.robot_flow_persistence_enabled),
            "effective_execution_mode": (
                "durable"
                if settings.persistence_mode != "memory"
                or settings.robot_flow_persistence_enabled
                else "synthetic_ephemeral"
            ),
            "actual_fill_policy": "durable",
            "synthetic_persistence": "ephemeral_tape+display_kline",
            "synthetic_financial_effect": False,
            "synthetic_kline_enabled": True,
            "synthetic_kline_canonical": False,
            "synthetic_kline_intervals_persisted": ["1m", "5m"],
            "metrics": {
                "events": int(
                    ((exchange_diagnostics.get("persistence_writer") or {}).get(
                        "robot_flow_synthetic_events"
                    ))
                    or 0
                ),
                "fills": int(
                    ((exchange_diagnostics.get("persistence_writer") or {}).get(
                        "robot_flow_synthetic_fills"
                    ))
                    or 0
                ),
            },
        },
        "snapshot": {
            "loaded": getattr(request.app.state, "exchange_snapshot_loaded", None),
            "last": getattr(request.app.state, "exchange_snapshot_last", None),
            "last_error": getattr(request.app.state, "exchange_snapshot_last_error", None),
            "commands_replayed": getattr(request.app.state, "exchange_commands_replayed", 0),
        },
        "rate_limits": {
            "enabled": settings.private_order_rate_limit_enabled,
            "order_submit_rate_per_second": settings.order_submit_rate_per_second,
            "order_submit_burst": settings.order_submit_burst,
            "order_cancel_rate_per_second": settings.order_cancel_rate_per_second,
            "order_cancel_burst": settings.order_cancel_burst,
        },
        "risk_limits": {
            "max_open_orders_per_user_market": settings.max_open_orders_per_user_market,
            "max_open_orders_per_user_total": settings.max_open_orders_per_user_total,
        },
        "books": book_markets,
        "orderbook_invariants": orderbook_invariants,
        "warnings": warnings,
    }


@router.post("/admin/markets/{symbol}/orderbook/rebuild")
async def rebuild_market_orderbook_from_db(
    symbol: str,
    payload: ConfirmExecuteRequest,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    if not payload.confirm_execute:
        raise HTTPException(status_code=400, detail="confirm_execute is required to rebuild orderbook")
    normalized_symbol = symbol.upper().strip()
    market = await session.scalar(select(Market).where(Market.symbol == normalized_symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")

    runtime = request.app.state.runtime
    async with runtime.market_locks[market.symbol]:
        db_rows = await session.execute(
            select(Order.order_id).where(
                Order.market_id == market.id,
                Order.product_type == market.product_type,
                Order.type == ORDER_TYPE_LIMIT,
                Order.tif == TIF_GTC,
                Order.status.in_(LIVE_ORDER_STATUSES),
                Order.remaining_quantity > Decimal("0"),
            )
        )
        db_live_ids = set(db_rows.scalars())
        book = runtime.engine.books.get(market.symbol)
        before_engine_ids = set(book.orders) if book is not None else set()

        if market.product_type == PRODUCT_TYPE_PERP:
            reconcile = await request.app.state.contract_service.rebuild_engine_book_from_db(
                session,
                market.id,
                reason="admin_rebuild_orderbook_from_db",
            )
        else:
            reconcile = await request.app.state.order_service._rebuild_engine_book_from_db(
                session,
                market.id,
                reason="admin_rebuild_orderbook_from_db",
            )

        after_book = runtime.engine.books.get(market.symbol)
        after_engine_ids = set(after_book.orders) if after_book is not None else set()

    response = {
        "ok": True,
        "symbol": market.symbol,
        "product_type": market.product_type,
        "before": {
            "engine_open_order_count": len(before_engine_ids),
            "db_live_open_order_count": len(db_live_ids),
            "engine_only_count": len(before_engine_ids - db_live_ids),
            "db_only_count": len(db_live_ids - before_engine_ids),
            "engine_only_sample": sorted(before_engine_ids - db_live_ids)[:20],
            "db_only_sample": sorted(db_live_ids - before_engine_ids)[:20],
        },
        "after": {
            "engine_open_order_count": len(after_engine_ids),
            "db_live_open_order_count": len(db_live_ids),
            "engine_only_count": len(after_engine_ids - db_live_ids),
            "db_only_count": len(db_live_ids - after_engine_ids),
            "engine_only_sample": sorted(after_engine_ids - db_live_ids)[:20],
            "db_only_sample": sorted(db_live_ids - after_engine_ids)[:20],
            "orderbook": runtime.market_data.orderbook_invariant_snapshot(market.symbol),
        },
        "reconcile": reconcile,
    }
    await record_admin_operation(
        session,
        actor=admin_user,
        request=request,
        domain="system",
        operation_type="orderbook_rebuild",
        target_type="market",
        target_id=market.id,
        target_symbol=market.symbol,
        status="success",
        summary=f"从数据库重建 {market.symbol} 内存订单簿",
        result={
            "product_type": market.product_type,
            "before": response["before"],
            "after": response["after"],
            "reconcile": reconcile,
        },
    )
    await session.commit()
    return response


@router.post("/admin/system/history-retention/run")
async def run_history_retention(
    request: Request,
    payload: dict | None = None,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    body = payload or {}
    dry_run = bool(body.get("dry_run", False))
    confirm_execute = bool(body.get("confirm_execute", False))
    if not dry_run and not confirm_execute:
        raise HTTPException(status_code=400, detail="confirm_execute is required to run history retention")
    service = HistoryRetentionService()
    result = await service.prune(session, dry_run=dry_run)
    response = {
        "ok": True,
        "auto_enabled": history_retention_auto_enabled(),
        **result,
    }
    if not dry_run:
        await record_admin_operation(
            session,
            actor=admin_user,
            request=request,
            domain="system",
            operation_type="history_retention_run",
            target_type="history_retention",
            status="success",
            summary="手动执行历史保留清理",
            result=response,
        )
        await session.commit()
    return response


@router.get("/admin/accounting/reconciliation/latest")
async def get_latest_reconciliation(
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    result = await ReconciliationService().latest(session)
    return {"item": result}


@router.post("/admin/accounting/reconciliation/run")
async def run_accounting_reconciliation(
    payload: dict | None = None,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    body = payload or {}
    scope = str(body.get("scope") or "full").strip().lower()
    allowed = {"full", "spot", "robot", "orders", "contract", "funding", "liquidation", "accounting", "outbox"}
    if scope not in allowed:
        raise HTTPException(status_code=400, detail="unsupported reconciliation scope")
    # This writes only the immutable reconciliation evidence; it never mutates
    # balances, positions, orders, trades, funding, insurance or ADL records.
    if ACCOUNTING_RECONCILIATION_RUN_LOCK.locked():
        raise HTTPException(status_code=409, detail="reconciliation is already running")
    async with ACCOUNTING_RECONCILIATION_RUN_LOCK:
        return await ReconciliationService().run(session, scope=scope)


@router.get("/admin/accounting/reconciliation/{run_id}/differences")
async def get_reconciliation_differences(
    run_id: str,
    domain: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    page = await ReconciliationService().differences(session, run_id, domain=domain, limit=limit, offset=offset)
    return {**page, "run_id": run_id, "domain": domain}


@router.get("/admin/accounting/shadow-summary")
async def get_shadow_accounting_summary(
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    transactions = int(await session.scalar(select(func.count()).select_from(AccountingTransaction)) or 0)
    entries = int(await session.scalar(select(func.count()).select_from(AccountingEntry)) or 0)
    reversals = int(
        await session.scalar(
            select(func.count()).select_from(AccountingTransaction).where(
                AccountingTransaction.reversal_of_transaction_id.is_not(None)
            )
        ) or 0
    )
    return {
        "mode": "shadow",
        "source_of_truth": False,
        "transactions": transactions,
        "entries": entries,
        "reversals": reversals,
        "immutable_committed_records": True,
        "automatic_repair": False,
    }


@router.post("/admin/accounting/shadow-adjustments")
async def create_shadow_accounting_adjustment(
    payload: dict,
    request: Request,
    actor: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Create an explicit shadow-only recovery linked to failed evidence.

    This endpoint never changes balances or contract accounts. It is isolated
    behind confirmation, an immutable failed reconciliation run, idempotency,
    a named actor and the normal admin-operation audit trail.
    """
    if payload.get("confirm_execute") is not True:
        raise HTTPException(status_code=400, detail="confirm_execute is required for a shadow adjustment")
    if ACCOUNTING_RECONCILIATION_RUN_LOCK.locked():
        raise HTTPException(status_code=409, detail="accounting proof, adjustment, or reconciliation is already running")
    async with ACCOUNTING_RECONCILIATION_RUN_LOCK:
        try:
            if settings.database_url.startswith("sqlite"):
                await session.execute(text("BEGIN IMMEDIATE"))
            transaction, created = await create_shadow_reconciliation_adjustment(
                session,
                reconciliation_run_id=str(payload.get("reconciliation_run_id") or ""),
                idempotency_key=str(payload.get("idempotency_key") or ""),
                reason=str(payload.get("reason") or ""),
                actor_username=actor.username,
            )
            if created:
                await record_admin_operation(
                    session,
                    actor=actor,
                    request=request,
                    domain="accounting",
                    operation_type="shadow_reconciliation_adjustment",
                    target_type="accounting_transaction",
                    target_id=transaction.transaction_id,
                    status="success",
                    summary="显式创建影子复式恢复调整",
                    result={
                        "reconciliation_run_id": transaction.source_batch_id,
                        "idempotency_key": transaction.idempotency_key,
                        "reason": transaction.metadata_json.get("reason"),
                        "adjustment_count": transaction.metadata_json.get("adjustment_count"),
                        "business_balance_mutated": False,
                        "automatic_repair": False,
                        "reversal_supported": True,
                    },
                )
            await session.commit()
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception:
            await session.rollback()
            raise
    return {
        "created": created,
        "item": {
            "transaction_id": transaction.transaction_id,
            "event_type": transaction.event_type,
            "idempotency_key": transaction.idempotency_key,
            "reconciliation_run_id": transaction.source_batch_id,
            "schema_version": transaction.schema_version,
            "metadata": transaction.metadata_json,
        },
    }


@router.get("/admin/accounting/outbox-summary")
async def get_financial_outbox_summary(
    request: Request,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    result = await financial_outbox_summary(session)
    result["runtime"] = {
        "metrics": dict(getattr(request.app.state.runtime, "financial_outbox_metrics", {})),
        "last_result": getattr(request.app.state, "financial_outbox_last_result", None),
        "last_error": getattr(request.app.state, "financial_outbox_last_error", None),
    }
    return result


@router.get("/admin/accounting/outbox-events")
async def get_financial_outbox_events(
    status: str | None = None,
    domain: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    allowed_statuses = {None, "pending", "processing", "delivered", "dead"}
    if status not in allowed_statuses:
        raise HTTPException(status_code=400, detail="unsupported outbox status")
    return await list_financial_outbox_events(
        session, status=status, domain=domain, limit=limit, offset=offset,
    )


@router.get("/admin/accounting/outbox-replay-requests")
async def get_financial_outbox_replay_requests(
    event_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    return await list_financial_outbox_replay_requests(
        session, event_id=event_id, limit=limit, offset=offset,
    )


@router.post("/admin/accounting/outbox-events/{event_id}/replay")
async def replay_dead_financial_outbox_event(
    event_id: str,
    payload: dict,
    request: Request,
    actor: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    if payload.get("confirm_execute") is not True:
        raise HTTPException(status_code=400, detail="confirm_execute is required to replay a dead financial event")
    try:
        expected_attempts = int(payload.get("expected_attempts") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="expected_attempts must be an integer") from exc
    try:
        replay_request, created = await request_financial_outbox_replay(
            session,
            event_id=event_id,
            expected_attempts=expected_attempts,
            idempotency_key=str(payload.get("idempotency_key") or ""),
            reason=str(payload.get("reason") or ""),
            reconciliation_run_id=str(payload.get("reconciliation_run_id") or ""),
            actor=actor,
        )
        if created:
            await record_admin_operation(
                session,
                actor=actor,
                request=request,
                domain="accounting",
                operation_type="financial_outbox_replay",
                target_type="financial_outbox_event",
                target_id=event_id,
                status="success",
                summary="受控重放 dead 财务 Outbox 事件",
                result={
                    "replay_id": replay_request.replay_id,
                    "expected_attempts": expected_attempts,
                    "reconciliation_run_id": replay_request.reconciliation_run_id,
                    "reason": replay_request.reason,
                    "automatic_balance_repair": False,
                },
            )
        await session.commit()
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"created": created, "item": serialize_outbox_replay_request(replay_request)}


@router.get("/admin/accounting/proof-checkpoints")
async def get_accounting_proof_checkpoints(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    include_snapshots: bool = False,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    page = await list_accounting_proofs(
        session, limit=limit, offset=offset, include_snapshots=include_snapshots,
    )
    page["verification"] = await verify_accounting_proof_chain(session)
    return page


@router.post("/admin/accounting/proof-checkpoints")
async def create_accounting_proof(
    payload: dict,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    idempotency_key = str(payload.get("idempotency_key") or "").strip()
    if len(idempotency_key) < 8 or len(idempotency_key) > 191:
        raise HTTPException(status_code=400, detail="idempotency_key must be 8-191 characters")
    if ACCOUNTING_RECONCILIATION_RUN_LOCK.locked():
        raise HTTPException(status_code=409, detail="accounting proof or reconciliation is already running")
    async with ACCOUNTING_RECONCILIATION_RUN_LOCK:
        existing = await session.scalar(
            select(AccountingProofCheckpoint).where(
                AccountingProofCheckpoint.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            return {"created": False, "item": serialize_accounting_proof(existing)}
        await session.rollback()
        try:
            # SQLite BEGIN IMMEDIATE establishes one stable snapshot and blocks
            # financial writers only for this on-demand proof window. The
            # reconciliation run and checkpoint then commit atomically.
            if settings.database_url.startswith("sqlite"):
                await session.execute(text("BEGIN IMMEDIATE"))
            summary = await ReconciliationService().run(
                session, scope="full", commit=False,
            )
            checkpoint, created = await create_accounting_proof_checkpoint(
                session,
                reconciliation_summary=summary,
                idempotency_key=idempotency_key,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    return {"created": created, "item": serialize_accounting_proof(checkpoint)}


@router.get("/admin/accounting/robot-checkpoints")
async def get_robot_financial_checkpoints(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    return await list_robot_financial_checkpoints(session, limit=limit, offset=offset)


@router.get("/admin/accounting/gate-readiness")
async def get_accounting_gate_readiness(
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    return await accounting_gate_readiness(session)


@router.get("/admin/accounting/evidence-runs")
async def get_accounting_evidence_runs(
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    return await list_accounting_evidence_runs(session, limit=limit, offset=offset)


@router.post("/admin/accounting/robot-checkpoints")
async def create_robot_financial_checkpoint_api(
    payload: dict,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    idempotency_key = str(payload.get("idempotency_key") or "").strip()
    if len(idempotency_key) < 8 or len(idempotency_key) > 191:
        raise HTTPException(status_code=400, detail="idempotency_key must be 8-191 characters")
    if ACCOUNTING_RECONCILIATION_RUN_LOCK.locked():
        raise HTTPException(status_code=409, detail="accounting proof, robot checkpoint, or reconciliation is already running")
    async with ACCOUNTING_RECONCILIATION_RUN_LOCK:
        existing = await session.scalar(
            select(RobotFinancialCheckpoint).where(
                RobotFinancialCheckpoint.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            return {"created": False, "item": serialize_robot_checkpoint(existing)}
        await session.rollback()
        try:
            if settings.database_url.startswith("sqlite"):
                await session.execute(text("BEGIN IMMEDIATE"))
            summary = await ReconciliationService().run(session, scope="full", commit=False)
            accounting_proof, _ = await create_accounting_proof_checkpoint(
                session,
                reconciliation_summary=summary,
                idempotency_key=f"{idempotency_key}:accounting-proof",
            )
            checkpoint, created = await create_robot_financial_checkpoint(
                session,
                reconciliation_summary=summary,
                accounting_proof=accounting_proof,
                idempotency_key=idempotency_key,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    return {"created": created, "item": serialize_robot_checkpoint(checkpoint)}


@router.get("/admin/operations")
async def list_admin_operations(
    domain: str | None = None,
    operation_type: str | None = None,
    target_symbol: str | None = None,
    actor_user_id: int | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    stmt = select(AdminOperationAudit)
    if domain:
        stmt = stmt.where(AdminOperationAudit.domain == domain.strip().lower())
    if operation_type:
        stmt = stmt.where(AdminOperationAudit.operation_type == operation_type.strip().lower())
    if target_symbol:
        stmt = stmt.where(AdminOperationAudit.target_symbol == target_symbol.strip().upper())
    if actor_user_id is not None:
        stmt = stmt.where(AdminOperationAudit.actor_user_id == actor_user_id)
    if status:
        stmt = stmt.where(AdminOperationAudit.status == status.strip().lower())
    rows = await session.execute(stmt.order_by(AdminOperationAudit.created_at.desc()).limit(limit))
    return {"items": [serialize_admin_operation(item) for item in rows.scalars()]}


@router.post("/admin/operations/failures")
async def record_admin_operation_failure(
    payload: AdminOperationFailureRequest,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    domain = (payload.domain or "system").strip().lower()[:32]
    operation_type = (payload.operation_type or "frontend_operation_failure").strip().lower()[:64]
    target_type = (payload.target_type or "operation").strip().lower()[:32]
    summary = payload.summary.strip() or "后台操作失败"
    operation = await record_admin_operation(
        session,
        actor=admin_user,
        request=request,
        domain=domain,
        operation_type=operation_type,
        target_type=target_type,
        target_id=payload.target_id,
        target_symbol=payload.target_symbol,
        status="failed",
        summary=summary,
        result={
            "error": payload.error,
            "context": payload.context,
            "source": "admin_page_client",
        },
    )
    await session.commit()
    return {"item": serialize_admin_operation(operation)}


@router.get("/admin/users/{user_id}")
async def get_user_detail(
    user_id: int,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    balances = await session.execute(select(Balance).where(Balance.user_id == user_id).order_by(Balance.asset.asc()))
    fee_rows = await session.execute(
        select(FeeProfile, Market.symbol)
        .join(Market, Market.id == FeeProfile.market_id)
        .where(FeeProfile.user_id == user_id)
        .order_by(Market.symbol.asc())
    )
    return serialize_user(
        user,
        [{"asset": item.asset, "available": str(item.available), "frozen": str(item.frozen)} for item in balances.scalars()],
        [
            {
                "symbol": symbol,
                "maker_fee_rate": str(profile.maker_fee_rate),
                "taker_fee_rate": str(profile.taker_fee_rate),
            }
            for profile, symbol in fee_rows.all()
        ],
    )


@router.get("/admin/users/{user_id}/activity")
async def get_user_activity(
    user_id: int,
    request: Request,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
    limit: int = Query(default=20, ge=1, le=100),
):
    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    service = get_service(request)
    open_order_rows = await session.execute(
        select(Order, Market.symbol)
        .join(Market, Market.id == Order.market_id)
        .where(Order.user_id == user_id, Order.status.in_(LIVE_ORDER_STATUSES))
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    recent_order_rows = await session.execute(
        select(Order, Market.symbol)
        .join(Market, Market.id == Order.market_id)
        .where(Order.user_id == user_id)
        .order_by(Order.updated_at.desc())
        .limit(limit)
    )
    trade_rows = await session.execute(
        select(Trade, Market.symbol)
        .join(Market, Market.id == Trade.market_id)
        .where(or_(Trade.taker_user_id == user_id, Trade.maker_user_id == user_id))
        .order_by(Trade.executed_at.desc())
        .limit(limit)
    )
    open_count = await session.scalar(
        select(func.count()).select_from(Order).where(Order.user_id == user_id, Order.status.in_(LIVE_ORDER_STATUSES))
    )
    open_orders = list(open_order_rows.all())
    recent_orders = list(recent_order_rows.all())
    trade_items = list(trade_rows.all())

    trades = []
    for trade, symbol in trade_items:
        is_taker = trade.taker_user_id == user_id
        side = trade.taker_side if is_taker else ("sell" if trade.taker_side == "buy" else "buy")
        trades.append(
            {
                "trade_id": trade.trade_id,
                "symbol": symbol,
                "side": side,
                "price": decimal_to_str(trade.price),
                "quantity": decimal_to_str(trade.quantity),
                "quote_amount": decimal_to_str(quantize_scale(trade.quote_amount, 8)),
                "liquidity_role": "taker" if is_taker else "maker",
                "fee": decimal_to_str(quantize_scale(trade.taker_fee if is_taker else trade.maker_fee, 8)),
                "fee_asset": trade.fee_asset_taker if is_taker else trade.fee_asset_maker,
                "ts": to_millis(trade.executed_at),
            }
        )
    recent_ledger = await service.serialize_ledger_entries(session, user_id, None, limit)

    return {
        "user": serialize_user(user),
        "summary": {
            "open_order_count": int(open_count or 0),
            "recent_order_count": len(recent_orders),
            "recent_trade_count": len(trades),
            "recent_ledger_count": len(recent_ledger),
        },
        "open_orders": [
            await service.serialize_order(session, order, symbol)
            for order, symbol in open_orders
        ],
        "recent_orders": [
            await service.serialize_order(session, order, symbol)
            for order, symbol in recent_orders
        ],
        "recent_trades": trades,
        "recent_ledger": recent_ledger,
    }


@router.get("/admin/orders")
async def admin_orders(
    request: Request,
    product_type: str | None = None,
    symbol: str | None = None,
    user_id: int | None = None,
    status: str | None = None,
    data_domain: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    order_service = get_service(request)
    contract_service = request.app.state.contract_service
    stmt = (
        select(Order, Market, User)
        .join(Market, Market.id == Order.market_id)
        .join(User, User.id == Order.user_id)
    )
    normalized_product_type = (product_type or "").upper().strip()
    if normalized_product_type in {PRODUCT_TYPE_SPOT, PRODUCT_TYPE_PERP}:
        stmt = stmt.where(Order.product_type == normalized_product_type)
    elif normalized_product_type and normalized_product_type != "ALL":
        raise HTTPException(status_code=400, detail="unsupported product_type")
    if symbol:
        stmt = stmt.where(Market.symbol == symbol.upper().strip())
    if user_id is not None:
        stmt = stmt.where(Order.user_id == user_id)
    normalized_status = (status or "").strip().lower()
    if normalized_status in {"open", "live"}:
        stmt = stmt.where(Order.status.in_(LIVE_ORDER_STATUSES))
    elif normalized_status and normalized_status != "all":
        stmt = stmt.where(Order.status == normalized_status)
    normalized_data_domain = normalize_admin_data_domain(data_domain)
    domain_condition = admin_order_data_domain_condition(normalized_data_domain, User)
    if domain_condition is not None:
        stmt = stmt.where(domain_condition)

    rows = await session.execute(stmt.order_by(Order.created_at.desc()).limit(limit))
    items = []
    for order, market, user in rows.all():
        serializer = contract_service if order.product_type == PRODUCT_TYPE_PERP else order_service
        order_payload = (
            await serializer.serialize_order(session, order, market.symbol, market=market)
            if order.product_type == PRODUCT_TYPE_PERP
            else await serializer.serialize_order(session, order, market.symbol)
        )
        items.append({
            "user": {"id": user.id, "username": user.username, "role": user.role},
            "order": order_payload,
        })
    return {
        "items": items,
        "query": admin_audit_query_meta(
            product_type=normalized_product_type,
            symbol=symbol,
            user_id=user_id,
            status=normalized_status,
            data_domain=normalized_data_domain,
            limit=limit,
            result_count=len(items),
        ),
    }


@router.get("/admin/trades")
async def admin_trades(
    request: Request,
    product_type: str | None = None,
    symbol: str | None = None,
    user_id: int | None = None,
    data_domain: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    order_service = get_service(request)
    contract_service = request.app.state.contract_service
    taker_user = aliased(User)
    maker_user = aliased(User)
    stmt = (
        select(Trade, Market, taker_user, maker_user)
        .join(Market, Market.id == Trade.market_id)
        .join(taker_user, taker_user.id == Trade.taker_user_id)
        .join(maker_user, maker_user.id == Trade.maker_user_id)
    )
    normalized_product_type = (product_type or "").upper().strip()
    if normalized_product_type in {PRODUCT_TYPE_SPOT, PRODUCT_TYPE_PERP}:
        stmt = stmt.where(Trade.product_type == normalized_product_type)
    elif normalized_product_type and normalized_product_type != "ALL":
        raise HTTPException(status_code=400, detail="unsupported product_type")
    if symbol:
        stmt = stmt.where(Market.symbol == symbol.upper().strip())
    if user_id is not None:
        stmt = stmt.where(or_(Trade.taker_user_id == user_id, Trade.maker_user_id == user_id))
    normalized_data_domain = normalize_admin_data_domain(data_domain)
    domain_condition = admin_trade_data_domain_condition(normalized_data_domain, taker_user, maker_user)
    if domain_condition is not None:
        stmt = stmt.where(domain_condition)

    rows = await session.execute(stmt.order_by(Trade.executed_at.desc()).limit(limit))
    items = []
    for trade, market, taker, maker in rows.all():
        serializer = contract_service if trade.product_type == PRODUCT_TYPE_PERP else order_service
        items.append({
            "taker_user": {"id": taker.id, "username": taker.username, "role": taker.role},
            "maker_user": {"id": maker.id, "username": maker.username, "role": maker.role},
            "trade": await serializer.serialize_trade(trade, market.symbol, market),
        })
    return {
        "items": items,
        "query": admin_audit_query_meta(
            product_type=normalized_product_type,
            symbol=symbol,
            user_id=user_id,
            status=None,
            data_domain=normalized_data_domain,
            limit=limit,
            result_count=len(items),
        ),
    }


@router.get("/admin/users/{user_id}/ledger")
async def get_user_ledger(
    user_id: int,
    request: Request,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
    asset: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    normalized_asset = asset.upper() if asset else None
    return {"items": await get_service(request).serialize_ledger_entries(session, user_id, normalized_asset, limit)}


@router.post("/admin/users")
async def create_user(
    payload: UserCreateRequest,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    username = payload.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="username is required")
    exists = await session.scalar(select(User).where(User.username == username))
    if exists is not None:
        raise HTTPException(status_code=409, detail="username already exists")

    user = User(
        id=await next_uid_for_role(session, payload.role),
        username=username,
        role=payload.role,
        api_key=generate_api_key(username.replace("_", "-")[:18] or "user"),
        api_secret_hash=generate_api_secret(),
        password_hash=hash_password(payload.password),
        is_active=payload.is_active,
    )
    session.add(user)
    await session.flush()

    now = datetime.now(tz=UTC)
    zero = Decimal("0")
    initial_balances = {
        str(asset).upper().strip(): Decimal(amount)
        for asset, amount in (payload.initial_balances or {}).items()
        if str(asset).upper().strip()
    }
    # Admin-created accounts use the same default as registration: spot USDT
    # is 100M by default, while the shared account initializer below creates
    # the separate 100M perp wallet.  An explicit admin-entered spot amount is
    # still respected for test fixtures that intentionally need another value.
    initial_balances.setdefault("USDT", settings.paper_exchange_default_spot_usdt)
    for asset, amount in initial_balances.items():
        normalized_asset = asset.upper().strip()
        if not normalized_asset:
            continue
        if amount < zero:
            raise HTTPException(status_code=400, detail=f"{normalized_asset} initial balance cannot be negative")
        await AccountService().get_balance(session, user.id, normalized_asset)
        session.add(ResetTemplate(name="default", user_id=user.id, asset=normalized_asset, amount=amount))
        if amount != zero:
            await AccountService().apply_change(
                session, user.id, normalized_asset,
                available_delta=amount, frozen_delta=zero,
                change_type="deposit_reset", amount=amount,
                note="admin_create_user", created_at=now,
            )

    market_rows = await session.execute(select(Market).where(Market.is_active.is_(True)))
    active_markets = list(market_rows.scalars())
    await ensure_user_assets(
        session,
        user,
        active_markets,
        reason="admin_create_user",
        preserve_balances=True,
    )

    if payload.maker_fee_rate is not None and payload.taker_fee_rate is not None:
        for market in active_markets:
            session.add(
                FeeProfile(
                    user_id=user.id,
                    market_id=market.id,
                    maker_fee_rate=payload.maker_fee_rate,
                    taker_fee_rate=payload.taker_fee_rate,
                )
            )

    await session.commit()
    return {
        "ok": True,
        "user": {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "api_key": user.api_key,
            "api_secret": user.api_secret_hash,
            "is_active": user.is_active,
        },
    }


@router.put("/admin/users/{user_id}")
async def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    changes = payload.model_dump(exclude_none=True)
    if "role" in changes:
        user.role = changes["role"]
    if "is_active" in changes:
        user.is_active = changes["is_active"]
    if "password" in changes:
        password = str(changes["password"])
        if not password:
            raise HTTPException(status_code=400, detail="password cannot be empty")
        user.password_hash = hash_password(password)
    await session.commit()
    return {"ok": True, "user": serialize_user(user)}


@router.post("/admin/users/{user_id}/rotate-api-key")
async def rotate_user_api_key(
    user_id: int,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    prefix = user.username.replace("_", "-")[:18] or "user"
    user.api_key = generate_api_key(prefix)
    user.api_secret_hash = generate_api_secret()
    await record_admin_operation(
        session,
        actor=admin_user,
        request=request,
        domain="account",
        operation_type="rotate_api_key",
        target_type="user",
        target_id=user.id,
        status="success",
        summary=f"轮换用户 {user.username} API Key",
        result={"user_id": user.id, "username": user.username},
    )
    await session.commit()
    return {"ok": True, "user": serialize_user(user, include_api_secret=admin_user.id == user.id)}


@router.get("/admin/markets")
async def list_markets(
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    rows = await session.execute(select(Market).order_by(Market.symbol.asc()))
    return {"items": [serialize_market(market) for market in rows.scalars()]}


@router.get("/admin/market-templates")
async def list_market_templates(
    _: User = Depends(get_admin_user),
):
    return {"items": MARKET_TEMPLATES}


@router.get("/admin/market-surveillance")
async def get_market_surveillance(
    request: Request,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
    symbol: str | None = None,
):
    stmt = select(Market).order_by(Market.symbol.asc())
    if symbol:
        stmt = stmt.where(Market.symbol == symbol.upper())
    rows = await session.execute(stmt)
    markets = list(rows.scalars())
    if symbol and not markets:
        raise HTTPException(status_code=404, detail="market not found")
    return {"items": [await build_market_surveillance_item(request, session, market) for market in markets]}


@router.get("/admin/strategy-templates")
async def list_strategy_templates(
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    templates = await ensure_strategy_templates(session)
    await session.commit()
    return {"items": [serialize_strategy_template(templates[key]) for key in SUPPORTED_STRATEGY_KEYS if key in templates]}


@router.post("/admin/markets")
async def create_market(
    payload: MarketCreateRequest,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    symbol = payload.symbol.upper().strip()
    product_type = payload.product_type.upper().strip()
    base_asset = payload.base_asset.upper().strip()
    quote_asset = payload.quote_asset.upper().strip()
    margin_asset = (payload.margin_asset or quote_asset).upper().strip() if product_type == "PERP" else None
    if not symbol or not base_asset or not quote_asset:
        raise HTTPException(status_code=400, detail="symbol, base_asset and quote_asset are required")
    if product_type == "SPOT" and base_asset == quote_asset:
        raise HTTPException(status_code=400, detail="base_asset and quote_asset cannot be the same")
    exists = await session.scalar(select(Market).where(Market.symbol == symbol))
    if exists is not None:
        raise HTTPException(status_code=409, detail="market already exists")

    choices = PERP_STRATEGY_KEYS if product_type == "PERP" else SPOT_STRATEGY_KEYS
    preferred = "SIMPLE_BBO" if product_type == "PERP" else "LITE"
    strategy_key = payload.default_maker_strategy or (preferred if preferred in choices else next(iter(choices), "NONE"))
    if strategy_key != "NONE" and strategy_key not in choices:
        raise HTTPException(422, "默认铺单策略不支持此产品")
    source_symbol = (payload.price_source_symbol or symbol.removesuffix('-PERP')).upper()
    market = Market(
        symbol=symbol,
        price_source="binance", price_source_symbol=source_symbol,
        product_type=product_type,
        market_type=payload.market_type,
        visibility=MARKET_VISIBILITY_TEST,
        base_asset=base_asset,
        quote_asset=quote_asset,
        margin_asset=margin_asset,
        price_tick=payload.price_tick,
        qty_step=payload.qty_step,
        min_qty=payload.min_qty,
        min_notional=payload.min_notional,
        max_leverage=payload.max_leverage,
        default_leverage=payload.default_leverage,
        maintenance_margin_rate=payload.maintenance_margin_rate,
        funding_rate=payload.funding_rate,
        funding_interval_hours=payload.funding_interval_hours,
        index_price_source=payload.index_price_source,
        mark_price_mode=payload.mark_price_mode,
        funding_rate_mode=payload.funding_rate_mode,
        funding_interest_rate=payload.funding_interest_rate,
        funding_clamp_rate=payload.funding_clamp_rate,
        funding_cap_rate=payload.funding_cap_rate,
        funding_impact_notional=payload.funding_impact_notional,
        contract_trading_mode=payload.contract_trading_mode if product_type == "PERP" else "normal",
        reference_price=payload.reference_price,
        price_precision=payload.price_precision if payload.price_precision is not None else decimal_scale(payload.price_tick),
        qty_precision=payload.qty_precision
        if payload.qty_precision is not None
        else max(decimal_scale(payload.qty_step), decimal_scale(payload.min_qty)),
        is_active=payload.is_active,
        default_maker_fee_rate=payload.default_maker_fee_rate,
        default_taker_fee_rate=payload.default_taker_fee_rate,
    )
    session.add(market)
    await session.flush()
    if product_type == "PERP":
        session.add(
            ContractRiskLimitTier(
                market_id=market.id,
                tier=1,
                notional_floor=Decimal("0"),
                notional_cap=None,
                max_leverage=market.max_leverage,
                maintenance_margin_rate=market.maintenance_margin_rate,
                maintenance_amount=Decimal("0"),
            )
        )
    templates = await ensure_strategy_templates(session)
    if strategy_key in INTERNAL_MAKER_STRATEGIES:
        from app.services.maker_plugins import internal_default
        config = internal_default(strategy_key, source_symbol)
        if strategy_key == 'SIMPLE_BBO':
            config.update(templates[strategy_key].default_config_json)
        await set_selected_market_strategy(session, market, strategy_key, config_json={'desired_version': 1, 'config': config})
    else:
        await set_selected_market_strategy(session, market, strategy_key)

    created_bots: list[MarketBotAccount] = []
    balance_count = 0
    template_count = 0
    fee_count = 0
    if strategy_key == 'NONE':
        pass
    elif product_type == 'PERP' or payload.default_maker_strategy is not None or not payload.create_default_bots or payload.default_bot_count <= 0:
        from app.services.strategy_accounts import allocate_accounts
        uids = await allocate_accounts(session, market, 'maker', strategy_key)
        created_bots = list((await session.scalars(select(MarketBotAccount).where(MarketBotAccount.market_id == market.id, MarketBotAccount.user_id.in_(uids)))).all())
        if strategy_key not in INTERNAL_MAKER_STRATEGIES:
            balance_count = len(created_bots) * (2 if product_type == 'SPOT' else 0)
            template_count = balance_count
            fee_count = len(created_bots)
    elif product_type == "SPOT" and payload.create_default_bots and payload.default_bot_count > 0:
        created_bots = await create_default_market_bots(
            session,
            market,
            bot_count=payload.default_bot_count,
            initial_quote_amount=payload.default_bot_initial_quote_amount,
            initial_base_notional=payload.default_bot_initial_base_notional,
            reference_price=payload.reference_price,
        )
        balance_count = len(created_bots) * 2
        template_count = len(created_bots) * 2
        fee_count = len(created_bots)

    await session.commit()
    return {
        "ok": True,
        "market": serialize_market(market),
        "default_maker_strategy": strategy_key,
        "maker_enabled": False,
        "initialized": {
            "market_bots": len(created_bots),
            "balances": balance_count,
            "reset_templates": template_count,
            "fee_profiles": fee_count,
        },
    }


@router.get("/admin/markets/{symbol}/bots")
async def list_market_bots(
    symbol: str,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    rows = await session.execute(
        select(MarketBotAccount)
        .where(MarketBotAccount.market_id == market.id)
        .order_by(MarketBotAccount.id.asc())
    )
    items = [await serialize_market_bot_account(session, market, bot) for bot in rows.scalars()]
    return {"symbol": market.symbol, "items": items}


@router.post("/admin/markets/{symbol}/bots")
async def create_market_bot(
    symbol: str,
    payload: MarketBotCreateRequest,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    index = int(
        await session.scalar(
            select(func.count()).select_from(MarketBotAccount).where(MarketBotAccount.market_id == market.id)
        )
        or 0
    ) + 1
    bot = await create_market_bot_account(session, market, payload, index=index)
    await session.commit()
    return {"ok": True, "bot": await serialize_market_bot_account(session, market, bot)}


@router.post("/admin/markets/{symbol}/bots/defaults")
async def create_market_default_bots(
    symbol: str,
    payload: MarketBotDefaultsRequest,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    reference_price = payload.reference_price or market.reference_price
    if reference_price is None:
        raise HTTPException(status_code=400, detail="reference_price is required")
    bots = await create_default_market_bots(
        session,
        market,
        bot_count=payload.bot_count,
        initial_quote_amount=payload.initial_quote_amount,
        initial_base_notional=payload.initial_base_notional,
        reference_price=reference_price,
    )
    await session.commit()
    return {
        "ok": True,
        "created": len(bots),
        "items": [await serialize_market_bot_account(session, market, bot) for bot in bots],
    }


@router.post("/admin/markets/{symbol}/bots/default-flow")
async def create_market_default_flow_bot(
    symbol: str,
    payload: MarketBotFlowDefaultRequest,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    reference_price = payload.reference_price or market.reference_price
    if reference_price is None:
        raise HTTPException(status_code=400, detail="reference_price is required")
    bot, created = await ensure_default_market_flow_bot(
        session,
        market,
        initial_quote_amount=payload.initial_quote_amount,
        initial_base_notional=Decimal("0") if market.product_type == PRODUCT_TYPE_PERP else payload.initial_base_notional,
        reference_price=reference_price,
        is_enabled=payload.is_enabled,
    )
    await session.commit()
    return {
        "ok": True,
        "created": created,
        "bot": await serialize_market_bot_account(session, market, bot),
    }


async def set_market_flow_control(
    session: AsyncSession,
    request: Request,
    market: Market,
    *,
    enabled: bool,
    mode: str,
    connector: str = "local_sandbox",
) -> dict:
    independent = getattr(request.app.state, "independent_flow", None)
    if independent is not None:
        from app.services.independent_flow_service import FlowConfig
        document = independent.configs.get(market.symbol, {"version": 0, "config": FlowConfig().model_dump(mode="json")})
        config = dict(document["config"])
        config.update(enabled=enabled, mode="real_ioc_sandbox" if mode == "real_ioc" else mode if mode != "off" else config["mode"])
        if enabled and config["mode"] == "real_ioc_sandbox" and not config.get("uid"):
            config["uid"] = await session.scalar(select(MarketBotAccount.user_id).where(MarketBotAccount.market_id == market.id, MarketBotAccount.role == "flow", MarketBotAccount.is_enabled.is_(True)))
        try:
            saved = await independent.save(market.symbol, config, document["version"])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"ok": True, "symbol": market.symbol, "enabled": enabled, "mode": config["mode"], "flow": saved}
    normalized_mode = str(mode or FLOW_MODE_REAL_IOC_SANDBOX).strip().lower()
    if not enabled:
        normalized_mode = FLOW_MODE_OFF
    if normalized_mode == "real_ioc":
        normalized_mode = FLOW_MODE_REAL_IOC_SANDBOX
    if normalized_mode not in {FLOW_MODE_OFF, FLOW_MODE_REAL_IOC_SANDBOX, FLOW_MODE_VIRTUAL_VOLUME}:
        raise HTTPException(status_code=400, detail="unsupported flow mode")
    normalized_connector = str(connector or "local_sandbox").strip().lower()
    if enabled and normalized_connector not in LOCAL_SANDBOX_CONNECTORS:
        raise HTTPException(status_code=400, detail="active FLOW is disabled for external exchange connectors")

    selected, _, effective = await current_strategy_config(session, request, market)
    config_json = deepcopy(selected.config_json or {})
    current_control = deepcopy(effective.get("flow_control") if isinstance(effective.get("flow_control"), dict) else {})
    current_control.update({
        "enabled": bool(enabled),
        "mode": normalized_mode if normalized_mode != FLOW_MODE_OFF else FLOW_MODE_REAL_IOC_SANDBOX,
        "connector": normalized_connector,
    })
    config_json["flow_control"] = current_control
    real_ioc_enabled = bool(enabled and normalized_mode == FLOW_MODE_REAL_IOC_SANDBOX)
    if selected.strategy_key == "LITE":
        config_json["flow_enabled"] = real_ioc_enabled
    elif selected.strategy_key == "PERP_MM":
        config_json["flow_enabled"] = real_ioc_enabled
    selected.config_json = config_json
    runtime = request.app.state.runtime
    if selected.strategy_key == "LITE":
        live_overrides = runtime.get_liquidity_lite_runtime_overrides(market.symbol)
        live_overrides["flow_control"] = current_control
        live_overrides["flow_enabled"] = real_ioc_enabled
        runtime.set_liquidity_lite_runtime_overrides(market.symbol, live_overrides)
    elif selected.strategy_key == "PERP_MM":
        live_overrides = runtime.get_liquidity_runtime_overrides(market.symbol)
        live_overrides["flow_control"] = current_control
        live_overrides["flow_enabled"] = real_ioc_enabled
        runtime.set_liquidity_runtime_overrides(market.symbol, live_overrides)
    await session.flush()
    await session.commit()
    bundle = await build_strategy_runtime_bundle(session, request, market)
    return {
        "ok": True,
        "symbol": market.symbol,
        "mode": normalized_mode,
        "enabled": bool(enabled),
        "coordinator": bundle["coordinator"],
        "robot_cards": bundle["robot_cards"],
        "strategy": bundle["strategy"],
    }


@router.post("/admin/markets/{symbol}/flow/start")
async def start_market_flow_robot(
    symbol: str,
    payload: dict,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    if not payload.get("confirm_execute"):
        raise HTTPException(status_code=400, detail="confirm_execute is required to start FLOW")
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    result = await set_market_flow_control(
        session,
        request,
        market,
        enabled=True,
        mode=str(payload.get("mode") or FLOW_MODE_REAL_IOC_SANDBOX),
        connector=str(payload.get("connector") or "local_sandbox"),
    )
    await record_admin_operation(
        session,
        actor=admin_user,
        request=request,
        domain="bot",
        operation_type="flow_control_start",
        target_type="market",
        target_id=market.id,
        target_symbol=market.symbol,
        status="success",
        summary=f"启动 {market.symbol} FLOW",
        result={
            "symbol": result.get("symbol"),
            "enabled": result.get("enabled"),
            "mode": result.get("mode"),
            "source_policy": ((result.get("coordinator") or {}).get("flow") or {}).get("source_policy"),
            "flow_status": ((result.get("coordinator") or {}).get("flow") or {}).get("status"),
        },
    )
    await session.commit()
    return result


@router.post("/admin/markets/{symbol}/flow/pause")
async def pause_market_flow_robot(
    symbol: str,
    payload: dict,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    if not payload.get("confirm_execute"):
        raise HTTPException(status_code=400, detail="confirm_execute is required to pause FLOW")
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    result = await set_market_flow_control(
        session,
        request,
        market,
        enabled=False,
        mode=FLOW_MODE_OFF,
        connector=str(payload.get("connector") or "local_sandbox"),
    )
    await record_admin_operation(
        session,
        actor=admin_user,
        request=request,
        domain="bot",
        operation_type="flow_control_pause",
        target_type="market",
        target_id=market.id,
        target_symbol=market.symbol,
        status="success",
        summary=f"暂停 {market.symbol} FLOW",
        result={
            "symbol": result.get("symbol"),
            "enabled": result.get("enabled"),
            "mode": result.get("mode"),
            "source_policy": ((result.get("coordinator") or {}).get("flow") or {}).get("source_policy"),
            "flow_status": ((result.get("coordinator") or {}).get("flow") or {}).get("status"),
        },
    )
    await session.commit()
    return result


@router.put("/admin/markets/{symbol}/bots/{bot_id}")
async def update_market_bot(
    symbol: str,
    bot_id: int,
    payload: MarketBotUpdateRequest,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    bot = await session.scalar(
        select(MarketBotAccount).where(
            MarketBotAccount.id == bot_id,
            MarketBotAccount.market_id == market.id,
        )
    )
    if bot is None:
        raise HTTPException(status_code=404, detail="market bot not found")

    changes = payload.model_dump(exclude_none=True)
    if "bot_label" in changes:
        existing = await session.scalar(
            select(MarketBotAccount.id).where(
                MarketBotAccount.market_id == market.id,
                MarketBotAccount.bot_label == changes["bot_label"],
                MarketBotAccount.id != bot.id,
            )
        )
        if existing is not None:
            raise HTTPException(status_code=409, detail="bot_label already exists for this market")
        bot.bot_label = normalize_bot_text(changes["bot_label"], bot.bot_label, 64)
    user = await session.scalar(select(User).where(User.id == bot.user_id))
    if user is None:
        raise HTTPException(status_code=500, detail="market bot user missing")
    if "username" in changes:
        username = normalize_bot_text(changes["username"], user.username, 64)
        existing = await session.scalar(select(User.id).where(User.username == username, User.id != user.id))
        if existing is not None:
            raise HTTPException(status_code=409, detail="username already exists")
        user.username = username
    if "api_key" in changes:
        api_key = await ensure_unique_api_key(session, changes["api_key"], exclude_user_id=user.id)
        if api_key is not None:
            user.api_key = api_key
    if "api_secret" in changes:
        api_secret = normalize_bot_text(changes["api_secret"], "", 255)
        if api_secret and not is_masked_api_secret_placeholder(api_secret, user.api_secret_hash):
            user.api_secret_hash = api_secret
    if "password" in changes:
        password = changes["password"]
        if password and password.strip():
            user.password_hash = hash_password(password)
    if changes.get("is_enabled", bot.is_enabled) or "role" in changes:
        from app.services.strategy_accounts import validate_uid
        await validate_uid(session, bot.user_id, market.id, changes.get("role", bot.role), exclude_id=bot.id)
    if "role" in changes:
        bot.role = changes["role"]
    if "strategy_role" in changes:
        bot.strategy_role = normalize_bot_text(changes["strategy_role"], bot.role, 32)
    if "initial_quote_amount" in changes:
        bot.initial_quote_amount = changes["initial_quote_amount"]
    if "initial_base_notional" in changes:
        bot.initial_base_notional = changes["initial_base_notional"]
    if "reference_price" in changes:
        bot.reference_price = changes["reference_price"]
    if "initial_base_amount" in changes:
        bot.initial_base_amount = quantize_step(changes["initial_base_amount"], Decimal(market.qty_step))
    elif "initial_base_notional" in changes or "reference_price" in changes:
        bot.initial_base_amount = resolve_bot_base_amount(
            market,
            Decimal(bot.reference_price),
            Decimal(bot.initial_base_notional),
            None,
        )
    if {"initial_quote_amount", "initial_base_notional", "initial_base_amount", "reference_price"} & set(changes):
        if market.product_type == PRODUCT_TYPE_PERP:
            await ensure_contract_bot_account(
                session,
                user,
                market=market,
                margin_asset=market.margin_asset or market.quote_asset,
                wallet_balance=Decimal(bot.initial_quote_amount),
                note=f"contract_market_bot_update:{market.symbol}",
                now=datetime.now(tz=UTC),
            )
        else:
            await sync_market_bot_reset_templates(
                session,
                market,
                bot,
                note=f"market_bot_update:{market.symbol}",
                now=datetime.now(tz=UTC),
            )
    if "is_enabled" in changes:
        bot.is_enabled = changes["is_enabled"]

    await session.commit()
    await session.refresh(bot)
    return {"ok": True, "bot": await serialize_market_bot_account(session, market, bot)}


@router.get("/admin/markets/{symbol}/strategy")
async def get_market_strategy(
    symbol: str,
    request: Request,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    templates = await strategy_template_map(session)
    selected, template, effective = await current_strategy_config(session, request, market)
    rows = await session.execute(
        select(MarketStrategyConfig)
        .where(MarketStrategyConfig.market_id == market.id)
        .order_by(MarketStrategyConfig.strategy_key.asc())
    )
    configs = []
    for item in rows.scalars():
        if item.strategy_key not in SUPPORTED_STRATEGY_KEYS:
            continue
        item_template = templates.get(item.strategy_key)
        item_effective = (await effective_strategy_config(session, request, market, item.strategy_key))[2]
        configs.append(serialize_market_strategy_config(item, item_template, effective_config=item_effective))
    await session.commit()
    return {
        "symbol": market.symbol,
        "selected": serialize_market_strategy_config(selected, template, effective_config=effective),
        "items": configs,
        "templates": [serialize_strategy_template(templates[key]) for key in SUPPORTED_STRATEGY_KEYS if key in templates],
    }


@router.put("/admin/markets/{symbol}/strategy")
@maker_serialized
async def update_market_strategy(
    symbol: str,
    payload: dict,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    key = parse_strategy_key_or_400(payload.get("strategy_key") or payload.get("strategy_version"))
    key = validate_strategy_key_for_market(market, key)
    config_json = payload.get("config")
    if config_json is None:
        config_json = payload.get("config_json")
    if config_json is not None and not isinstance(config_json, dict):
        raise HTTPException(status_code=400, detail="config must be an object")
    await ensure_strategy_templates(session)
    previous_selected = await selected_market_strategy_config(
        session,
        market,
        fallback_strategy_key=request.app.state.runtime.get_liquidity_strategy_selection(market.symbol),
    )
    previous_strategy_key = maker_instance_strategy_key(market, previous_selected.strategy_key) or previous_selected.strategy_key
    if key in INTERNAL_MAKER_STRATEGIES or previous_selected.strategy_key in INTERNAL_MAKER_STRATEGIES:
        raise HTTPException(409, "内部铺单策略切换请使用铺单策略页面；参数使用各自的版本化配置页")
    if not maker_instance_status(market.symbol, request).get('running'):
        from app.services.strategy_accounts import allocate_accounts
        await allocate_accounts(session, market, 'maker', key)

    selected = await set_selected_market_strategy(
        session,
        market,
        key,
        config_json=config_json,
        updated_by_user_id=admin_user.id,
    )
    request.app.state.runtime.set_liquidity_strategy_selection(market.symbol, key)
    if config_json is not None:
        if key == "LITE":
            request.app.state.runtime.set_liquidity_lite_runtime_overrides(market.symbol, config_json)
        else:
            request.app.state.runtime.set_liquidity_runtime_overrides(market.symbol, config_json)
    await session.commit()
    selected, template, effective = await current_strategy_config(session, request, market)
    instance = await ensure_maker_instance_record(session, market)
    display_key = maker_instance_strategy_key(market, selected.strategy_key) or selected.strategy_key
    instance_status = maker_instance_status(
        market.symbol,
        request,
        configured_strategy_version=display_key,
        instance=instance,
    )
    apply_status = build_strategy_apply_status(
        market,
        instance_status,
        previous_strategy_key=previous_strategy_key,
        next_strategy_key=display_key,
        config_changed=config_json is not None,
    )
    await session.commit()
    return {
        "ok": True,
        "symbol": market.symbol,
        "selected": serialize_market_strategy_config(selected, template, effective_config=effective),
        "instance": instance_status,
        "apply_status": apply_status,
    }


@router.get("/admin/markets/{symbol}/strategy/runtime")
async def get_market_strategy_runtime(
    symbol: str,
    request: Request,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    bundle = await build_strategy_runtime_bundle(session, request, market)
    await session.commit()
    return bundle


@router.get("/admin/maker-instances")
async def list_market_maker_instances(
    request: Request,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    rows = await session.execute(select(Market).order_by(Market.symbol.asc()))
    items = []
    for market in rows.scalars():
        bundle = await build_strategy_runtime_bundle(session, request, market)
        instance = await ensure_maker_instance_record(session, market)
        readiness = build_maker_instance_start_readiness(market, bundle)
        items.append(
            maker_instance_status(
                market.symbol,
                request,
                configured_strategy_version=maker_instance_strategy_key(market, bundle["strategy"]["strategy_key"]),
                instance=instance,
                start_readiness=readiness,
            )
        )
    await session.commit()
    return {"items": items}


@router.get("/admin/markets/{symbol}/maker-instance")
async def get_market_maker_instance(
    symbol: str,
    request: Request,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    bundle = await build_strategy_runtime_bundle(session, request, market)
    instance = await ensure_maker_instance_record(session, market)
    await session.commit()
    readiness = build_maker_instance_start_readiness(market, bundle)
    return maker_instance_status(
        market.symbol,
        request,
        configured_strategy_version=maker_instance_strategy_key(market, bundle["strategy"]["strategy_key"]),
        instance=instance,
        start_readiness=readiness,
    )


@router.get("/admin/markets/{symbol}/maker-instance/logs")
async def get_market_maker_instance_logs(
    symbol: str,
    lines: int = Query(default=120, ge=1, le=500),
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    line_count = min(500, max(1, int(lines)))
    log_path = maker_instance_paths(market.symbol)["log"]
    instance = await ensure_maker_instance_record(session, market)
    await session.commit()
    log_lines, log_meta = current_run_log_tail(log_path, instance, max_lines=line_count)
    return {
        "symbol": market.symbol,
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


@maker_serialized
async def start_maker_instance_for_market(
    market: Market,
    request: Request,
    admin_user: User,
    session: AsyncSession,
    *,
    cleanup_before_start: bool = True,
) -> dict:
    await session.refresh(market)
    await session.refresh(admin_user)
    if not market.is_active:
        raise HTTPException(status_code=400, detail="market is inactive")
    from app.services.maker_initialization import ensure_maker_initialized
    initialized = await ensure_maker_initialized(session, market, actor_id=admin_user.id)
    await session.commit()
    internal_service = getattr(request.app.state, 'contract_ladder_service', None)
    if internal_service is not None:
        invalidate = getattr(getattr(internal_service, 'adapter', None), 'invalidate_identity', None)
        if invalidate:
            invalidate(market.symbol, market.id)
        await internal_service.refresh()
    bundle = await build_strategy_runtime_bundle(session, request, market)
    from app.services.strategy_accounts import validate_uid
    for bot in bundle['accounts']['makers']:
        await validate_uid(session, bot['uid'], market.id, 'maker')
    readiness = build_maker_instance_start_readiness(market, bundle)
    if not readiness["ok"]:
        if any(item.get("code") == "no_enabled_maker" for item in readiness["blockers"]):
            raise HTTPException(status_code=400, detail="market has no enabled maker bots")
        blocker_detail = "; ".join(item.get("detail", "") for item in readiness["blockers"] if item.get("detail"))
        raise HTTPException(status_code=400, detail=f"maker instance start readiness failed: {blocker_detail}")

    if bundle["strategy"]["strategy_key"] in INTERNAL_MAKER_STRATEGIES:
        svc = request.app.state.contract_ladder_service
        record = svc.records.get(market.symbol)
        if not record:
            raise HTTPException(409, "请先在铺单策略页面配置当前策略")
        draft = deepcopy(record["config"])
        draft["enabled"] = True
        try:
            await svc.publish(market.symbol, draft, record["desired_version"], admin_user.id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return maker_instance_status(market.symbol, request, configured_strategy_version=bundle["strategy"]["strategy_key"], start_readiness=readiness)

    instance = await ensure_maker_instance_record(session, market)
    current = maker_instance_status(
        market.symbol,
        request,
        configured_strategy_version=maker_instance_strategy_key(market, bundle["strategy"]["strategy_key"]) or bundle["strategy"]["strategy_key"],
        instance=instance,
        start_readiness=readiness,
    )
    if current["running"] and current.get("heartbeat_status") == "ok":
        return current
    api_base = api_base_url_from_request(request)
    fallback_liquidity_canceled = 0
    if market.product_type == PRODUCT_TYPE_PERP:
        fallback_liquidity_canceled = await cancel_contract_fallback_liquidity_orders(request, session, market)
    elif cleanup_before_start:
        cleanup = await cancel_market_bot_open_orders(request, session, market)
        fallback_liquidity_canceled = int(cleanup.get("canceled_count") or 0)
    try:
        return get_bot_orchestrator(request).start_process(
            market=market,
            admin_user=admin_user,
            instance=instance,
            strategy_key=maker_instance_strategy_key(market, bundle["strategy"]["strategy_key"]) or bundle["strategy"]["strategy_key"],
            api_base_url=api_base,
            public_ws_url=ws_url_from_request(request, "/ws/public"),
            private_ws_url=ws_url_from_request(request, "/ws/private"),
            start_readiness=readiness,
            fallback_liquidity_canceled=fallback_liquidity_canceled,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def stop_maker_instance_process(symbol: str) -> dict:
    paths = maker_instance_paths(symbol)
    pid = read_pid(paths["pid"])
    running = pid_is_running(pid)
    exited = not running
    if pid and not running:
        paths["pid"].unlink(missing_ok=True)
    if pid and running:
        identity_ok, identity_reason = pid_matches_maker_instance(
            pid,
            symbol,
            project_root=PROJECT_ROOT,
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
        exited = wait_for_pid_exit(pid)
        if not exited:
            try:
                os.killpg(pid, signal.SIGTERM)
            except OSError:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            exited = wait_for_pid_exit(pid, timeout_seconds=4.0)
        if not exited:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            exited = wait_for_pid_exit(pid, timeout_seconds=2.0)
        if exited:
            paths["pid"].unlink(missing_ok=True)
    return {"pid": pid, "was_running": running, "exited": exited}


async def cancel_market_bot_orders_for_restart(request: Request, session: AsyncSession, market: Market) -> dict:
    if market.product_type == PRODUCT_TYPE_PERP:
        cleanup = await cancel_contract_market_bot_open_orders(request, session, market)
    else:
        cleanup = await cancel_market_bot_open_orders(request, session, market)
    cleanup["skipped"] = False
    cleanup["reason"] = "clean_restart"
    return cleanup


@maker_serialized
async def clean_restart_maker_instance_for_market(
    market: Market,
    request: Request,
    admin_user: User,
    session: AsyncSession,
) -> dict:
    stop_result = stop_maker_instance_process(market.symbol)
    instance = await ensure_maker_instance_record(session, market)
    if not stop_result.get("exited"):
        instance.status = "stopping"
        instance.pid = stop_result.get("pid")
        await session.commit()
        raise HTTPException(
            status_code=409,
            detail="maker instance did not exit cleanly; refusing to restart before old process stops",
        )

    instance.status = "stopped"
    instance.pid = None
    instance.stopped_at = datetime.now(tz=UTC)

    cleanup = await cancel_market_bot_orders_for_restart(request, session, market)
    if not cleanup.get("ok", False):
        await session.commit()
        raise HTTPException(status_code=409, detail="maker order cleanup failed before restart")

    instance_status = await start_maker_instance_for_market(
        market,
        request,
        admin_user,
        session,
        cleanup_before_start=False,
    )
    return {
        "ok": True,
        "instance": instance_status,
        "cleanup": cleanup,
        "stop": stop_result,
    }


async def cancel_market_bot_open_orders(
    request: Request,
    session: AsyncSession,
    market: Market,
    *, maker_only: bool = False,
) -> dict:
    symbol = market.symbol
    market_id = market.id
    qty_step = Decimal(market.qty_step)
    base_asset = market.base_asset
    quote_asset = market.quote_asset
    rows = await session.execute(
        select(MarketBotAccount, User)
        .join(User, User.id == MarketBotAccount.user_id)
        .where(MarketBotAccount.market_id == market_id, *( [MarketBotAccount.role == "maker"] if maker_only else []))
        .order_by(MarketBotAccount.id.asc())
    )
    bot_accounts = [
        {
            "bot_id": bot.id,
            "source": "market_bot",
            "uid": user.id,
            "username": user.username,
            "role": bot.role,
            "user": user,
        }
        for bot, user in rows.all()
    ]
    seen_user_ids = {int(item["uid"]) for item in bot_accounts}
    if not maker_only:
        legacy_rows = await session.execute(
            select(User)
            .join(Order, Order.user_id == User.id)
            .where(
                Order.market_id == market_id,
                Order.status.in_(LIVE_ORDER_STATUSES),
                User.role == "mm_bot",
                User.id.not_in(seen_user_ids or [-1]),
            )
            .distinct()
            .order_by(User.username.asc())
        )
        for user in legacy_rows.scalars():
            bot_accounts.append(
                {
                    "bot_id": None,
                    "source": "legacy_mm_bot",
                    "uid": user.id,
                    "username": user.username,
                    "role": "legacy_mm_bot",
                    "user": user,
                }
            )
    service = get_service(request)
    accounts: list[dict] = []
    canceled_count = 0
    failed: list[dict] = []
    warnings: list[dict] = []
    for item in bot_accounts:
        user = item["user"]
        try:
            result = await service.cancel_all(session, user, symbol, target_user_id=user.id)
            count = int(result.get("count") or 0)
            canceled_count += count
            accounts.append(
                {
                    "bot_id": item["bot_id"],
                    "source": item["source"],
                    "uid": item["uid"],
                    "username": item["username"],
                    "role": item["role"],
                    "canceled_count": count,
                }
            )
        except Exception as exc:
            await session.rollback()
            try:
                fallback = await force_cancel_user_market_open_orders(
                    request,
                    session,
                    market_id=market_id,
                    symbol=symbol,
                    qty_step=qty_step,
                    base_asset=base_asset,
                    quote_asset=quote_asset,
                    user_id=int(item["uid"]),
                )
                count = int(fallback.get("count") or 0)
                canceled_count += count
                account_result = {
                    "bot_id": item["bot_id"],
                    "source": item["source"],
                    "uid": item["uid"],
                    "username": item["username"],
                    "role": item["role"],
                    "canceled_count": count,
                    "forced": True,
                    "fallback_reason": str(exc),
                    "release_shortfalls": fallback.get("release_shortfalls", []),
                }
                accounts.append(account_result)
                if fallback.get("release_shortfalls"):
                    warnings.append(account_result)
            except Exception as fallback_exc:
                await session.rollback()
                failed.append(
                    {
                        "bot_id": item["bot_id"],
                        "source": item["source"],
                        "uid": item["uid"],
                        "username": item["username"],
                        "error": str(exc),
                        "fallback_error": str(fallback_exc),
                    }
                )
    return {
        "ok": not failed,
        "symbol": symbol,
        "canceled_count": canceled_count,
        "accounts": accounts,
        "failed": failed,
        "warnings": warnings,
    }


async def cancel_contract_market_bot_open_orders(
    request: Request,
    session: AsyncSession,
    market: Market,
    *, maker_only: bool = False,
) -> dict:
    rows = await session.execute(
        select(MarketBotAccount, User)
        .join(User, User.id == MarketBotAccount.user_id)
        .where(MarketBotAccount.market_id == market.id, *( [MarketBotAccount.role == "maker"] if maker_only else []))
        .order_by(MarketBotAccount.id.asc())
    )
    contract_service = request.app.state.contract_service
    accounts: list[dict] = []
    canceled_count = 0
    failed: list[dict] = []
    for bot, user in rows.all():
        order_rows = await session.execute(
            select(Order).where(
                Order.market_id == market.id,
                Order.product_type == PRODUCT_TYPE_PERP,
                Order.user_id == user.id,
                Order.status.in_(LIVE_ORDER_STATUSES),
            )
        )
        orders = list(order_rows.scalars())
        account_count = 0
        for order in orders:
            try:
                try:
                    fast_result = await contract_service.cancel_order_fast(session, user, order.order_id)
                except Exception:
                    await session.rollback()
                    fast_result = None
                if fast_result is None:
                    await contract_service.cancel_order(session, user, order.order_id)
                account_count += 1
            except Exception as exc:
                await session.rollback()
                failed.append(
                    {
                        "bot_id": bot.id,
                        "uid": user.id,
                        "username": user.username,
                        "order_id": order.order_id,
                        "error": str(exc),
                    }
                )
        canceled_count += account_count
        accounts.append(
            {
                "bot_id": bot.id,
                "uid": user.id,
                "username": user.username,
                "role": bot.role,
                "canceled_count": account_count,
            }
        )
    return {
        "ok": not failed,
        "symbol": market.symbol,
        "product_type": PRODUCT_TYPE_PERP,
        "canceled_count": canceled_count,
        "accounts": accounts,
        "failed": failed,
        "warnings": [],
    }


async def cancel_contract_fallback_liquidity_orders(
    request: Request,
    session: AsyncSession,
    market: Market,
) -> int:
    liquidity_service = getattr(request.app.state, "contract_liquidity_service", None)
    if liquidity_service is None:
        return 0
    bid_user, _ = await ensure_contract_liquidity_user(session, market, "bid")
    ask_user, _ = await ensure_contract_liquidity_user(session, market, "ask")
    return await liquidity_service.cancel_liquidity_orders_for_market(
        session,
        market,
        (bid_user, ask_user),
    )


@router.post("/admin/markets/{symbol}/contract-fallback-liquidity/cancel")
async def cancel_contract_fallback_liquidity(
    symbol: str,
    payload: ConfirmExecuteRequest,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    if market.product_type != PRODUCT_TYPE_PERP:
        raise HTTPException(status_code=400, detail="market is not a PERP contract")
    if not payload.confirm_execute:
        raise HTTPException(status_code=400, detail="confirm_execute is required to cancel fallback contract liquidity")
    canceled_count = await cancel_contract_fallback_liquidity_orders(request, session, market)
    if canceled_count > 0:
        await record_admin_operation(
            session,
            actor=admin_user,
            request=request,
            domain="contract",
            operation_type="cancel_fallback_liquidity",
            target_type="market",
            target_id=market.id,
            target_symbol=market.symbol,
            status="success",
            summary=f"撤销 {market.symbol} 合约兜底流动性挂单",
            result={"symbol": market.symbol, "product_type": market.product_type, "canceled_count": canceled_count},
        )
    await session.commit()
    return {
        "ok": True,
        "symbol": market.symbol,
        "product_type": market.product_type,
        "canceled_count": canceled_count,
    }


async def reset_contract_market_bot_runtime_state(
    request: Request,
    session: AsyncSession,
    market: Market,
) -> dict:
    cleanup = await cancel_contract_market_bot_open_orders(request, session, market)
    now = datetime.now(tz=UTC)
    rows = await session.execute(
        select(MarketBotAccount, User)
        .join(User, User.id == MarketBotAccount.user_id)
        .where(MarketBotAccount.market_id == market.id)
        .order_by(MarketBotAccount.id.asc())
    )
    service = request.app.state.contract_service
    reset_positions = 0
    reset_accounts = 0
    account_items: list[dict] = []
    zero = Decimal("0")
    for bot, user in rows.all():
        positions = (
            await session.execute(
                select(ContractPosition).where(
                    ContractPosition.user_id == user.id,
                    ContractPosition.market_id == market.id,
                )
            )
        ).scalars().all()
        for position in positions:
            await session.delete(position)
            reset_positions += 1
        account = await session.scalar(
            select(ContractAccount).where(
                ContractAccount.user_id == user.id,
                ContractAccount.margin_asset == (market.margin_asset or market.quote_asset),
            )
        )
        if account is not None:
            before = snapshot_contract_account(account)
            account.used_margin = zero
            account.unrealized_pnl = zero
            account.realized_pnl = zero
            await service.refresh_account(session, account)
            account.updated_at = now
            await add_contract_ledger_entry(
                session,
                account,
                change_type="admin_demo_reset",
                amount=zero,
                before=before,
                market_id=market.id,
                note=f"demo_reset:{market.symbol}",
                created_at=now,
            )
            reset_accounts += 1
            account_items.append(
                {
                    "bot_id": bot.id,
                    "source": "market_bot",
                    "uid": user.id,
                    "username": user.username,
                    "wallet_balance": decimal_to_str(Decimal(account.wallet_balance)),
                    "available_margin": decimal_to_str(Decimal(account.available_margin)),
                    "used_margin": decimal_to_str(Decimal(account.used_margin)),
                }
            )
    for side in ("bid", "ask"):
        username = contract_liquidity_username(market.symbol, side)
        user = await session.scalar(select(User).where(User.username == username))
        if user is None:
            continue
        positions = (
            await session.execute(
                select(ContractPosition).where(
                    ContractPosition.user_id == user.id,
                    ContractPosition.market_id == market.id,
                )
            )
        ).scalars().all()
        for position in positions:
            await session.delete(position)
            reset_positions += 1
        account = await session.scalar(
            select(ContractAccount).where(
                ContractAccount.user_id == user.id,
                ContractAccount.margin_asset == (market.margin_asset or market.quote_asset),
            )
        )
        if account is not None:
            before = snapshot_contract_account(account)
            account.used_margin = zero
            account.unrealized_pnl = zero
            account.realized_pnl = zero
            await service.refresh_account(session, account)
            account.updated_at = now
            await add_contract_ledger_entry(
                session,
                account,
                change_type="admin_demo_reset",
                amount=zero,
                before=before,
                market_id=market.id,
                note=f"demo_reset:{market.symbol}:fallback:{side}",
                created_at=now,
            )
            reset_accounts += 1
            account_items.append(
                {
                    "bot_id": None,
                    "source": "fallback_liquidity",
                    "uid": user.id,
                    "username": user.username,
                    "wallet_balance": decimal_to_str(Decimal(account.wallet_balance)),
                    "available_margin": decimal_to_str(Decimal(account.available_margin)),
                    "used_margin": decimal_to_str(Decimal(account.used_margin)),
                }
            )
    await session.commit()
    return {
        "ok": True,
        "symbol": market.symbol,
        "product_type": market.product_type,
        "cleanup": cleanup,
        "reset_positions": reset_positions,
        "reset_accounts": reset_accounts,
        "accounts": account_items,
    }


@router.post("/admin/markets/{symbol}/contract-maker-state/reset")
async def reset_contract_market_bot_state(
    symbol: str,
    payload: ConfirmExecuteRequest,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    if market.product_type != PRODUCT_TYPE_PERP:
        raise HTTPException(status_code=400, detail="market is not a PERP contract")
    if not payload.confirm_execute:
        raise HTTPException(status_code=400, detail="confirm_execute is required to reset contract maker state")
    response = await reset_contract_market_bot_runtime_state(request, session, market)
    await record_admin_operation(
        session,
        actor=admin_user,
        request=request,
        domain="contract",
        operation_type="reset_contract_maker_state",
        target_type="market",
        target_id=market.id,
        target_symbol=market.symbol,
        status="success",
        summary=f"重置 {market.symbol} 合约机器人运行状态",
        result={
            "cleanup": response.get("cleanup"),
            "reset_positions": response.get("reset_positions"),
            "reset_accounts": response.get("reset_accounts"),
        },
    )
    await session.commit()
    return response


async def force_cancel_user_market_open_orders(
    request: Request,
    session: AsyncSession,
    *,
    market_id: int,
    symbol: str,
    qty_step: Decimal,
    base_asset: str,
    quote_asset: str,
    user_id: int,
) -> dict:
    service = get_service(request)
    now = datetime.now(tz=UTC)
    rows = await session.execute(
        select(Order).where(
            Order.market_id == market_id,
            Order.user_id == user_id,
            Order.status.in_(LIVE_ORDER_STATUSES),
        )
    )
    orders = list(rows.scalars())
    release_shortfalls: list[dict] = []
    changed_bids: list[list[str]] = []
    changed_asks: list[list[str]] = []
    async with service.runtime.market_locks[symbol]:
        for order in orders:
            with suppress(Exception):
                side_name, _, changes = service.runtime.engine.cancel_order(symbol, order.order_id)
                if side_name == SIDE_BUY:
                    changed_bids.extend(changes)
                elif side_name == SIDE_SELL:
                    changed_asks.extend(changes)
            # 与慢速 cancel 路径保持一致：引擎/耐用行取消后同步清除快速镜像，
            # 否则机器人报价会以"镜像有单、引擎无单"的幽灵形态残留，导致
            # 策略 QuoteSet 的撤单/改单永久失败（fast order path unavailable）。
            service._fast_orders.pop(order.order_id, None)
            service._fast_unregister_client_id(user_id, market_id, order.client_order_id, order.order_id)
            remaining = quantize_step(Decimal(order.remaining_quantity), qty_step)
            if remaining > 0:
                if order.side == SIDE_BUY:
                    release = (Decimal(order.price) * remaining) if order.price is not None else Decimal("0")
                    asset = quote_asset
                else:
                    release = remaining
                    asset = base_asset
                if release > 0:
                    balance = await service.runtime.account_service.get_balance(session, user_id, asset)
                    available_frozen = Decimal(balance.frozen)
                    actual_release = min(release, available_frozen)
                    if actual_release > 0:
                        await service.runtime.account_service.release(
                            session,
                            user_id,
                            asset,
                            actual_release,
                            related_order_id=order.order_id,
                            note="force_cancel_release",
                            created_at=now,
                        )
                    if actual_release < release:
                        release_shortfalls.append(
                            {
                                "order_id": order.order_id,
                                "asset": asset,
                                "expected_release": str(release),
                                "actual_release": str(actual_release),
                            }
                        )
            order.status = ORDER_STATUS_CANCELED
            order.canceled_at = now
            order.updated_at = now
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            await service._rebuild_engine_book_from_db(session, market_id, reason="force_cancel_commit_failed")
            raise
        service.runtime.orderbook_snapshot_unlocked(symbol, 50)
    if orders:
        await service._broadcast_order_flow(session, symbol, orders, {user_id}, changed_bids, changed_asks, [])
    return {"count": len(orders), "forced": True, "release_shortfalls": release_shortfalls}


@router.post("/admin/markets/{symbol}/maker-instance/start")
async def start_market_maker_instance(
    symbol: str,
    payload: ConfirmExecuteRequest,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    if not payload.confirm_execute:
        raise HTTPException(status_code=400, detail="confirm_execute is required to start a maker instance")
    instance = await start_maker_instance_for_market(market, request, admin_user, session)
    await record_admin_operation(
        session,
        actor=admin_user,
        request=request,
        domain="bot",
        operation_type="maker_instance_start",
        target_type="market",
        target_id=market.id,
        target_symbol=market.symbol,
        status="success",
        summary=f"启动 {market.symbol} 做市实例",
        result={"symbol": market.symbol, "status": instance.get("status"), "pid": instance.get("pid")},
    )
    await session.commit()
    return {"ok": True, "instance": instance}


@router.post("/admin/markets/{symbol}/maker-instance/stop")
@maker_serialized
async def stop_market_maker_instance(
    symbol: str,
    payload: ConfirmExecuteRequest,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    if not payload.confirm_execute:
        raise HTTPException(status_code=400, detail="confirm_execute is required to stop a maker instance")
    selected_for_stop, _, _ = await current_strategy_config(session, request, market)
    if selected_for_stop.strategy_key in INTERNAL_MAKER_STRATEGIES:
        from app.api.liquidity_control import maker_control, MakerAction
        return await maker_control(symbol, MakerAction(action="stop"), request, admin_user, session)
    stop_result = stop_maker_instance_process(market.symbol)
    selected, _, _ = await current_strategy_config(session, request, market)
    instance = await ensure_maker_instance_record(session, market)
    instance.status = "stopped" if stop_result.get("exited") else "stopping"
    instance.pid = None if stop_result.get("exited") else stop_result.get("pid")
    display_key = maker_instance_strategy_key(market, selected.strategy_key)
    instance.strategy_key = display_key
    if stop_result.get("exited"):
        instance.stopped_at = datetime.now(tz=UTC)
    instance_payload = maker_instance_status(
        market.symbol,
        request,
        configured_strategy_version=display_key,
        instance=instance,
    )
    await record_admin_operation(
        session,
        actor=admin_user,
        request=request,
        domain="bot",
        operation_type="maker_instance_stop",
        target_type="market",
        target_id=market.id,
        target_symbol=market.symbol,
        status="success",
        summary=f"停止 {market.symbol} 做市实例",
        result={"stop_result": stop_result, "instance": instance_payload},
    )
    await session.commit()
    return {
        "ok": True,
        "instance": instance_payload,
    }


@router.post("/admin/markets/{symbol}/maker-instance/stop-and-cancel")
@maker_serialized
async def stop_and_cancel_market_maker_instance(
    symbol: str,
    payload: ConfirmExecuteRequest,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    if not payload.confirm_execute:
        raise HTTPException(status_code=400, detail="confirm_execute is required to stop and cancel maker orders")
    market_id = market.id
    market_symbol = market.symbol
    selected_for_stop, _, _ = await current_strategy_config(session, request, market)
    if selected_for_stop.strategy_key in INTERNAL_MAKER_STRATEGIES:
        from app.api.liquidity_control import maker_control, MakerAction
        return await maker_control(symbol, MakerAction(action="stop"), request, admin_user, session)
    stop_result = stop_maker_instance_process(market_symbol)
    selected, _, _ = await current_strategy_config(session, request, market)
    selected_key = maker_instance_strategy_key(market, selected.strategy_key)
    cleanup: dict = {
        "ok": False,
        "symbol": market_symbol,
        "canceled_count": 0,
        "accounts": [],
        "failed": [],
        "skipped": True,
        "reason": "maker process has not exited",
    }
    if stop_result.get("exited"):
        if market.product_type == PRODUCT_TYPE_PERP:
            cleanup = await cancel_contract_market_bot_open_orders(request, session, market)
        else:
            cleanup = await cancel_market_bot_open_orders(request, session, market)
        cleanup["skipped"] = False
    instance = await session.scalar(select(MarketMakerInstance).where(MarketMakerInstance.market_id == market_id))
    if instance is None:
        instance = MarketMakerInstance(market_id=market_id, symbol=market_symbol, status="stopped")
        session.add(instance)
        await session.flush()
    instance.status = "stopped" if stop_result.get("exited") else "stopping"
    instance.pid = None if stop_result.get("exited") else stop_result.get("pid")
    instance.strategy_key = selected_key
    if stop_result.get("exited"):
        instance.stopped_at = datetime.now(tz=UTC)
    instance_payload = maker_instance_status(
        market_symbol,
        request,
        configured_strategy_version=selected_key,
        instance=instance,
    )
    response = {
        "ok": cleanup.get("ok", False) and bool(stop_result.get("exited")),
        "instance": instance_payload,
        "cleanup": cleanup,
    }
    await record_admin_operation(
        session,
        actor=admin_user,
        request=request,
        domain="bot",
        operation_type="maker_instance_stop_and_cancel",
        target_type="market",
        target_id=market_id,
        target_symbol=market_symbol,
        status="success",
        summary=f"停止 {market_symbol} 做市实例并撤销机器人挂单",
        result={
            "ok": response["ok"],
            "stop_result": stop_result,
            "cleanup": cleanup,
            "instance": instance_payload,
        },
    )
    await session.commit()
    return response


@router.post("/admin/markets/{symbol}/maker-instance/restart")
async def restart_market_maker_instance(
    symbol: str,
    payload: ConfirmExecuteRequest,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    if not payload.confirm_execute:
        raise HTTPException(status_code=400, detail="confirm_execute is required to restart a maker instance")
    result = await clean_restart_maker_instance_for_market(market, request, admin_user, session)
    await record_admin_operation(
        session,
        actor=admin_user,
        request=request,
        domain="bot",
        operation_type="maker_instance_restart",
        target_type="market",
        target_id=market.id,
        target_symbol=market.symbol,
        status="success",
        summary=f"重启 {market.symbol} 做市实例",
        result={
            "instance": result.get("instance"),
            "cleanup": result.get("cleanup"),
        },
    )
    await session.commit()
    return result


@router.post("/admin/users/{user_id}/reset-balances")
async def reset_user_balances(
    user_id: int,
    payload: ConfirmExecuteRequest,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    if not payload.confirm_execute:
        raise HTTPException(status_code=400, detail="confirm_execute is required to reset user balances")
    target_user = await session.scalar(select(User).where(User.id == user_id))
    await get_service(request).reset_user(session, user_id)
    await record_admin_operation(
        session,
        actor=admin_user,
        request=request,
        domain="account",
        operation_type="reset_user_balances",
        target_type="user",
        target_id=user_id,
        status="success",
        summary=f"重置用户 {target_user.username if target_user else user_id} 现货余额",
        result={"user_id": user_id, "username": target_user.username if target_user else None},
    )
    await session.commit()
    return {"ok": True}


@router.post("/admin/reset-test-users")
async def reset_test_users(
    payload: ConfirmExecuteRequest,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    if not payload.confirm_execute:
        raise HTTPException(status_code=400, detail="confirm_execute is required to reset test users")
    rows = await session.execute(
        select(User).where(User.role.in_(["manual_user", "mm_bot"])).order_by(User.id.asc())
    )
    users = list(rows.scalars())
    service = get_service(request)
    for user in users:
        await service.reset_user(session, user.id)
    response = {"ok": True, "count": len(users)}
    await record_admin_operation(
        session,
        actor=admin_user,
        request=request,
        domain="account",
        operation_type="reset_test_users",
        target_type="test_users",
        status="success",
        summary=f"批量重置 {len(users)} 个测试账户现货余额",
        result={"count": len(users), "user_ids": [user.id for user in users], "usernames": [user.username for user in users]},
    )
    await session.commit()
    return response


@router.post("/admin/users/{user_id}/adjust-balance")
async def adjust_balance(
    user_id: int,
    payload: AdjustBalanceRequest,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    target_user = await session.scalar(select(User).where(User.id == user_id))
    await get_service(request).adjust_balance(session, user_id, payload.asset, payload.amount, payload.reason)
    await record_admin_operation(
        session,
        actor=admin_user,
        request=request,
        domain="account",
        operation_type="adjust_spot_balance",
        target_type="user",
        target_id=user_id,
        status="success",
        summary=f"调整用户 {target_user.username if target_user else user_id} 现货余额 {payload.asset} {payload.amount}",
        result={
            "user_id": user_id,
            "username": target_user.username if target_user else None,
            "asset": payload.asset,
            "amount": str(payload.amount),
            "reason": payload.reason,
        },
    )
    await session.commit()
    return {"ok": True}


@router.get("/admin/markets/{symbol}")
async def get_market(
    symbol: str,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    return serialize_market(market)


@router.put("/admin/markets/{symbol}")
async def update_market(
    symbol: str,
    payload: UpdateMarketRequest,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    changes = payload.model_dump(exclude_none=True)
    type_changed = changes.get('product_type', market.product_type) != market.product_type
    if type_changed:
        configs = (await session.scalars(select(MarketStrategyConfig).where(MarketStrategyConfig.market_id == market.id))).all()
        instance = await session.scalar(select(MarketMakerInstance).where(MarketMakerInstance.market_id == market.id))
        if any((row.config_json or {}).get('config', {}).get('enabled') for row in configs) or (instance and instance.status in {'running', 'starting'}):
            raise HTTPException(409, "请先停止铺单策略并撤净挂单，再修改空币对的产品类型")
        if await session.scalar(select(Order.id).where(Order.market_id == market.id).limit(1)) or await session.scalar(select(Trade.id).where(Trade.market_id == market.id).limit(1)):
            raise HTTPException(409, "已有订单或成交的币对不能直接修改产品类型，请创建独立币对")
    for key, value in changes.items():
        setattr(market, key, value)
    if "price_tick" in changes and "price_precision" not in changes:
        market.price_precision = decimal_scale(changes["price_tick"])
    if ("qty_step" in changes or "min_qty" in changes) and "qty_precision" not in changes:
        market.qty_precision = max(decimal_scale(market.qty_step), decimal_scale(market.min_qty))
    if type_changed:
        if market.product_type == 'PERP':
            market.margin_asset = market.margin_asset or market.quote_asset
            tier = await session.scalar(select(ContractRiskLimitTier.id).where(ContractRiskLimitTier.market_id == market.id))
            if tier is None:
                session.add(ContractRiskLimitTier(market_id=market.id, tier=1, notional_floor=Decimal(0), max_leverage=market.max_leverage, maintenance_margin_rate=market.maintenance_margin_rate, maintenance_amount=Decimal(0)))
        from app.services.maker_initialization import ensure_maker_initialized
        await ensure_maker_initialized(session, market, strategy_key='SIMPLE_BBO' if market.product_type == 'PERP' else 'LITE')
    await session.commit()
    return {"ok": True}


@router.put("/admin/markets/{symbol}/fees")
async def update_market_fees(
    symbol: str,
    payload: UpdateMarketFeesRequest,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    before = {
        "maker_fee_rate": str(market.default_maker_fee_rate),
        "taker_fee_rate": str(market.default_taker_fee_rate),
    }
    market.default_maker_fee_rate = payload.maker_fee_rate
    market.default_taker_fee_rate = payload.taker_fee_rate
    await record_admin_operation(
        session,
        actor=admin_user,
        request=request,
        domain="market",
        operation_type="update_market_fees",
        target_type="market",
        target_id=market.id,
        target_symbol=market.symbol,
        status="success",
        summary=f"更新 {market.symbol} 市场默认手续费",
        result={
            "symbol": market.symbol,
            "before": before,
            "after": {
                "maker_fee_rate": str(payload.maker_fee_rate),
                "taker_fee_rate": str(payload.taker_fee_rate),
            },
            "boundary": "market defaults only apply when no UID fee profile exists; historical trades and ledgers are not recalculated",
        },
    )
    await session.commit()
    return {"ok": True, "symbol": market.symbol}


@router.put("/admin/users/{user_id}/fees")
async def update_user_fees(
    user_id: int,
    payload: UpdateUserFeesRequest,
    request: Request,
    symbol: str = Query(...),
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    existing = await session.scalar(
        select(FeeProfile).where(FeeProfile.user_id == user_id, FeeProfile.market_id == market.id)
    )
    before = (
        {
            "maker_fee_rate": str(existing.maker_fee_rate),
            "taker_fee_rate": str(existing.taker_fee_rate),
        }
        if existing is not None
        else None
    )
    await ensure_fee_profile(session, user_id, market.id, payload.maker_fee_rate, payload.taker_fee_rate)
    await record_admin_operation(
        session,
        actor=admin_user,
        request=request,
        domain="account",
        operation_type="update_user_fees",
        target_type="user",
        target_id=user_id,
        target_symbol=market.symbol,
        status="success",
        summary=f"更新用户 {user.username} 在 {market.symbol} 的 UID 手续费",
        result={
            "user_id": user_id,
            "username": user.username,
            "role": user.role,
            "symbol": market.symbol,
            "scope": "single_market",
            "profile_existed": existing is not None,
            "before": before,
            "after": {
                "maker_fee_rate": str(payload.maker_fee_rate),
                "taker_fee_rate": str(payload.taker_fee_rate),
            },
            "boundary": "UID fee profile only affects future trades; historical trades, spot ledger, contract total_fees and PnL are not recalculated",
        },
    )
    await session.commit()
    return {"ok": True, "user_id": user_id, "symbol": market.symbol}


@router.put("/admin/users/{user_id}/fees-all")
async def update_user_fees_all_markets(
    user_id: int,
    payload: UpdateUserFeesRequest,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    rows = await session.execute(select(Market).order_by(Market.symbol.asc()))
    markets = list(rows.scalars())
    existing_rows = await session.execute(select(FeeProfile).where(FeeProfile.user_id == user_id))
    existing_by_market_id = {profile.market_id: profile for profile in existing_rows.scalars()}
    count = 0
    created_symbols: list[str] = []
    updated_symbols: list[str] = []
    for market in markets:
        if market.id in existing_by_market_id:
            updated_symbols.append(market.symbol)
        else:
            created_symbols.append(market.symbol)
        await ensure_fee_profile(session, user_id, market.id, payload.maker_fee_rate, payload.taker_fee_rate)
        count += 1
    await record_admin_operation(
        session,
        actor=admin_user,
        request=request,
        domain="account",
        operation_type="update_user_fees_all",
        target_type="user",
        target_id=user_id,
        status="success",
        summary=f"更新用户 {user.username} 全市场 UID 手续费",
        result={
            "user_id": user_id,
            "username": user.username,
            "role": user.role,
            "scope": "all_markets",
            "updated": count,
            "created_profile_count": len(created_symbols),
            "updated_profile_count": len(updated_symbols),
            "created_symbols": created_symbols,
            "updated_symbols": updated_symbols,
            "maker_fee_rate": str(payload.maker_fee_rate),
            "taker_fee_rate": str(payload.taker_fee_rate),
            "boundary": "UID fee profiles only affect future trades; historical trades, spot ledger, contract total_fees and PnL are not recalculated",
        },
    )
    await session.commit()
    return {"ok": True, "updated": count}


@router.post("/admin/markets/{symbol}/reset")
async def reset_market(
    symbol: str,
    payload: ConfirmExecuteRequest,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    if not payload.confirm_execute:
        raise HTTPException(status_code=400, detail="confirm_execute is required to reset market data")
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    if market.product_type != PRODUCT_TYPE_SPOT:
        raise HTTPException(status_code=400, detail="spot market reset is not supported for PERP markets")
    response = await get_service(request).reset_market(session, market)
    await record_admin_operation(
        session,
        actor=admin_user,
        request=request,
        domain="market",
        operation_type="reset_spot_market",
        target_type="market",
        target_id=market.id,
        target_symbol=market.symbol,
        status="success",
        summary=f"撤销 {market.symbol} 现货市场全部当前挂单",
        result=response,
    )
    await session.commit()
    return response


@router.post("/admin/markets/{symbol}/seed-book")
async def seed_market_book(
    symbol: str,
    payload: SeedMarketBookRequest,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    if market.product_type != PRODUCT_TYPE_SPOT:
        raise HTTPException(status_code=400, detail="seed-book is only supported for SPOT markets")
    plan = build_seed_book_plan(market, payload)
    if not plan:
        raise HTTPException(status_code=400, detail="seed plan is empty; mid_price is too close to zero")
    if payload.dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "symbol": market.symbol,
            "planned_orders": len(plan),
            "preview": [
                {
                    "side": item["side"],
                    "price": decimal_to_str(item["price"]),
                    "quantity": decimal_to_str(item["quantity"]),
                }
                for item in plan[:10]
            ],
        }
    if not payload.confirm_execute:
        raise HTTPException(
            status_code=400,
            detail="confirm_execute is required when dry_run is false",
        )
    if not market.is_active:
        raise HTTPException(status_code=400, detail="market is inactive")

    maker_accounts = await enabled_market_maker_accounts(session, market)
    if len(maker_accounts) < 2:
        raise HTTPException(
            status_code=400,
            detail="market must have at least two enabled maker bots before seeding a test book",
        )
    bid_bot, bid_user = maker_accounts[0]
    ask_bot, ask_user = maker_accounts[1]

    service = get_service(request)
    if payload.cancel_existing:
        await service.reset_market(session, market)

    now = datetime.now(tz=UTC)
    for bot, user in ((bid_bot, bid_user), (ask_bot, ask_user)):
        await ensure_user_asset_template(
            session,
            user,
            market.base_asset,
            Decimal(bot.initial_base_amount),
            note="admin_seed_market_book",
            now=now,
        )
        await ensure_user_asset_template(
            session,
            user,
            market.quote_asset,
            Decimal(bot.initial_quote_amount),
            note="admin_seed_market_book",
            now=now,
        )

    placed_orders = 0
    rejected_orders = 0
    for index, item in enumerate(plan, start=1):
        user = bid_user if item["side"] == SIDE_BUY else ask_user
        response = await service.place_order(
            session,
            user,
            OrderCreateRequest(
                symbol=market.symbol,
                side=item["side"],
                type="limit",
                tif=TIF_GTC,
                price=item["price"],
                quantity=item["quantity"],
                client_order_id=f"admin-seed-{market.symbol}-{index}",
            ),
        )
        order = response.get("order", {})
        if order.get("status") == "rejected":
            rejected_orders += 1
        else:
            placed_orders += 1

    response = {
        "ok": rejected_orders == 0,
        "symbol": market.symbol,
        "placed_orders": placed_orders,
        "rejected_orders": rejected_orders,
        "cancel_existing": payload.cancel_existing,
        "dry_run": False,
        "confirm_execute": True,
        "accounts": {
            "bid": {"bot_id": bid_bot.id, "uid": bid_user.id, "username": bid_user.username},
            "ask": {"bot_id": ask_bot.id, "uid": ask_user.id, "username": ask_user.username},
        },
    }
    await record_admin_operation(
        session,
        actor=admin_user,
        request=request,
        domain="market",
        operation_type="seed_spot_orderbook",
        target_type="market",
        target_id=market.id,
        target_symbol=market.symbol,
        status="success",
        summary=f"初始化 {market.symbol} 现货测试盘口",
        result=response,
    )
    await session.commit()
    return response


@router.post("/admin/markets/{symbol}/sweep-preview")
async def preview_market_sweep(
    symbol: str,
    payload: MarketSweepPreviewRequest,
    request: Request,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    if market.product_type != PRODUCT_TYPE_SPOT:
        raise HTTPException(status_code=400, detail="sweep-preview is only supported for SPOT markets")
    snapshot, _, _ = await request.app.state.runtime.orderbook_snapshot(market.symbol, payload.depth)
    return build_sweep_preview(market, snapshot, payload)


@router.post("/admin/markets/{symbol}/wipe-data")
async def wipe_market_data(
    symbol: str,
    payload: ConfirmExecuteRequest,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    if not payload.confirm_execute:
        raise HTTPException(status_code=400, detail="confirm_execute is required to wipe market data")
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    if market.product_type != PRODUCT_TYPE_SPOT:
        raise HTTPException(status_code=400, detail="spot market wipe-data is not supported for PERP markets")
    response = await get_service(request).wipe_market_data(session, market)
    await record_admin_operation(
        session,
        actor=admin_user,
        request=request,
        domain="market",
        operation_type="wipe_spot_market_data",
        target_type="market",
        target_id=market.id,
        target_symbol=market.symbol,
        status="success",
        summary=f"清空 {market.symbol} 现货市场历史数据",
        result=response,
    )
    await session.commit()
    return response


@router.post("/admin/markets/{symbol}/wipe-display-history")
async def wipe_market_display_history(
    symbol: str, payload: ConfirmExecuteRequest, request: Request,
    admin_user: User = Depends(get_admin_user), session: AsyncSession = Depends(get_db_session),
):
    if not payload.confirm_execute:
        raise HTTPException(400, "confirm_execute is required")
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None:
        raise HTTPException(404, "market not found")
    response = await get_service(request).wipe_market_klines(session, market, clear_display_history=True)
    await record_admin_operation(session, actor=admin_user, request=request, domain="market",
        operation_type="wipe_market_display_history", target_type="market", target_id=market.id,
        target_symbol=market.symbol, status="success", summary=f"清理 {market.symbol} 行情展示历史，保留订单账务与持仓", result=response)
    await session.commit()
    return response


@router.post("/admin/markets/{symbol}/wipe-klines")
async def wipe_market_klines(
    symbol: str,
    payload: ConfirmExecuteRequest,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    if not payload.confirm_execute:
        raise HTTPException(status_code=400, detail="confirm_execute is required to wipe market klines")
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    response = await get_service(request).wipe_market_klines(session, market)
    await record_admin_operation(
        session,
        actor=admin_user,
        request=request,
        domain="market",
        operation_type="wipe_market_klines",
        target_type="market",
        target_id=market.id,
        target_symbol=market.symbol,
        status="success",
        summary=f"清除 {market.symbol} 历史 K 线",
        result=response,
    )
    await session.commit()
    return response


@router.post("/admin/ops/liquidity-state")
async def update_liquidity_state(
    payload: dict,
    request: Request,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    symbol = str(payload.get("symbol") or "").upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    request.app.state.runtime.liquidity_metrics[symbol] = payload
    instance = await ensure_maker_instance_record(session, market)
    run_meta = maker_instance_run_meta(instance)
    event_status = str(payload.get("service_state") or "").lower()
    now = datetime.now(tz=UTC)
    allowed_statuses = {"starting", "running", "switching", "rollback", "stopping", "stopped", "error"}
    next_status = event_status if event_status in allowed_statuses else "running"
    previous_status = str(instance.status or "stopped").lower()
    heartbeat_age_seconds = (
        (now - ensure_utc(instance.last_heartbeat_at)).total_seconds()
        if instance.last_heartbeat_at is not None
        else None
    )
    persist_due = (
        next_status != previous_status
        or next_status != "running"
        or bool(payload.get("last_error") or payload.get("error"))
        or heartbeat_age_seconds is None
        or heartbeat_age_seconds >= LIQUIDITY_STATE_PERSIST_INTERVAL_SECONDS
    )
    if not persist_due:
        # Runtime metrics remain current for admin/diagnostics, while SQLite
        # heartbeat persistence is bounded to one write per interval.
        return {"ok": True, "persisted": False, "instance": maker_instance_status(symbol, request, instance=instance)}
    event_pid: int | None = None
    if payload.get("pid") is not None:
        try:
            event_pid = int(payload["pid"])
        except (TypeError, ValueError):
            event_pid = None
    if next_status == "stopping":
        if event_pid is not None and not pid_is_running(event_pid):
            instance.status = "stopped"
            instance.stopped_at = now
            instance.pid = None
    else:
        instance.status = next_status
    instance.last_heartbeat_at = now
    incoming_run_meta = payload.get("_run") if isinstance(payload.get("_run"), dict) else None
    instance.last_metrics_json = with_maker_instance_run_meta(payload, incoming_run_meta or run_meta)
    instance.strategy_key = str(payload.get("strategy_version") or instance.strategy_key or "").upper() or None
    instance.last_error = str(payload.get("last_error") or payload.get("error") or "") or instance.last_error
    if event_pid is not None and next_status != "stopping":
        instance.pid = event_pid
    if instance.status == "stopped":
        instance.stopped_at = now
        instance.pid = None
    elif instance.status in {"starting", "running", "switching", "rollback"} and instance.started_at is None:
        instance.started_at = now
    await session.commit()
    return {"ok": True, "persisted": True, "instance": maker_instance_status(symbol, request, instance=instance)}


@router.get("/admin/ops/liquidity-config/{symbol}")
async def get_liquidity_runtime_config(
    symbol: str,
    request: Request,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    symbol = symbol.upper()
    runtime = request.app.state.runtime
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    config, _, effective = await effective_strategy_config(session, request, market, "PERP_MM")
    legacy_overrides = runtime.get_liquidity_runtime_overrides(symbol)
    overrides = merge_config(config.config_json or {}, legacy_overrides)
    return {
        "symbol": symbol,
        "config": effective,
        "overrides": overrides,
        "has_live_config": bool(legacy_live_strategy_config(request, symbol, "PERP_MM")),
        "storage": "database",
    }


@router.put("/admin/ops/liquidity-config/{symbol}")
async def update_liquidity_runtime_config(
    symbol: str,
    payload: dict,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    symbol = symbol.upper()
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    await ensure_strategy_templates(session)
    config = await ensure_strategy_config_for_key(session, market, "PERP_MM")
    config.config_json = deepcopy(payload)
    config.updated_by_user_id = admin_user.id
    runtime = request.app.state.runtime
    runtime.set_liquidity_runtime_overrides(symbol, payload)
    await session.commit()
    _, _, effective = await effective_strategy_config(session, request, market, "PERP_MM")
    overrides = merge_config(config.config_json or {}, runtime.get_liquidity_runtime_overrides(symbol))
    return {
        "ok": True,
        "symbol": symbol,
        "config": effective,
        "overrides": overrides,
        "has_live_config": bool(legacy_live_strategy_config(request, symbol, "PERP_MM")),
        "storage": "database",
    }


@router.get("/admin/ops/liquidity-lite-config/{symbol}")
async def get_liquidity_lite_runtime_config(
    symbol: str,
    request: Request,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    symbol = symbol.upper()
    runtime = request.app.state.runtime
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    config, _, effective = await effective_strategy_config(session, request, market, "LITE")
    legacy_overrides = runtime.get_liquidity_lite_runtime_overrides(symbol)
    overrides = merge_config(config.config_json or {}, legacy_overrides)
    return {
        "symbol": symbol,
        "config": effective,
        "overrides": overrides,
        "has_live_config": bool(legacy_live_strategy_config(request, symbol, "LITE")),
        "storage": "database",
    }


@router.put("/admin/ops/liquidity-lite-config/{symbol}")
async def update_liquidity_lite_runtime_config(
    symbol: str,
    payload: dict,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    symbol = symbol.upper()
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    await ensure_strategy_templates(session)
    config = await ensure_strategy_config_for_key(session, market, "LITE")
    config.config_json = deepcopy(payload)
    config.updated_by_user_id = admin_user.id
    runtime = request.app.state.runtime
    runtime.set_liquidity_lite_runtime_overrides(symbol, payload)
    await session.commit()
    _, _, effective = await effective_strategy_config(session, request, market, "LITE")
    overrides = merge_config(config.config_json or {}, runtime.get_liquidity_lite_runtime_overrides(symbol))
    return {
        "ok": True,
        "symbol": symbol,
        "config": effective,
        "overrides": overrides,
        "has_live_config": bool(legacy_live_strategy_config(request, symbol, "LITE")),
        "storage": "database",
    }


@router.get("/admin/ops/liquidity-strategy/{symbol}")
async def get_liquidity_strategy_selection(
    symbol: str,
    request: Request,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    symbol = symbol.upper()
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    selected, _, _ = await current_strategy_config(session, request, market)
    await session.commit()
    return {
        "symbol": symbol,
        "strategy_version": selected.strategy_key,
        "storage": "database",
    }


@router.put("/admin/ops/liquidity-strategy/{symbol}")
@maker_serialized
async def update_liquidity_strategy_selection(
    symbol: str,
    payload: dict,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    symbol = symbol.upper()
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    version = parse_strategy_key_or_400(payload.get("strategy_version") or payload.get("strategy_key"))
    version = validate_strategy_key_for_market(market, version)
    current, _, _ = await current_strategy_config(session, request, market)
    if version in INTERNAL_MAKER_STRATEGIES or current.strategy_key in INTERNAL_MAKER_STRATEGIES:
        raise HTTPException(409, "内部铺单策略切换请使用铺单策略页面，确保旧策略已停止并撤净挂单")
    await ensure_strategy_templates(session)
    selected = await set_selected_market_strategy(session, market, version, updated_by_user_id=admin_user.id)
    runtime = request.app.state.runtime
    runtime.set_liquidity_strategy_selection(symbol, version)
    await session.commit()
    return {
        "ok": True,
        "symbol": symbol,
        "strategy_version": selected.strategy_key,
        "storage": "database",
    }


@router.get("/admin/ops/liquidity-dump/{symbol}")
async def export_liquidity_diagnostics(
    symbol: str,
    request: Request,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
    depth: int = Query(default=20, ge=5, le=100),
    recent_trades: int = Query(default=20, ge=5, le=100),
):
    symbol = symbol.upper()
    runtime = request.app.state.runtime
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")

    metrics = deepcopy(runtime.liquidity_metrics.get(symbol, {}))
    strategy_version = runtime.get_liquidity_strategy_selection(symbol)
    book_snapshot, _, _ = await runtime.orderbook_snapshot(symbol, depth)
    book_summary = summarize_orderbook_snapshot(book_snapshot)
    stats = runtime.market_data.compute_stats(symbol, book_snapshot)
    bid_owner = await fetch_level_owner_summary(session, market.id, "buy", book_summary["best_bid"])
    ask_owner = await fetch_level_owner_summary(session, market.id, "sell", book_summary["best_ask"])

    v2_live_config = metrics.get("runtime_config", {}) or default_runtime_config_snapshot()
    v2_overrides = runtime.get_liquidity_runtime_overrides(symbol)
    lite_live_config = metrics.get("lite_runtime_config", {}) or default_lite_runtime_config_snapshot()
    lite_overrides = runtime.get_liquidity_lite_runtime_overrides(symbol)

    recent_trade_items = runtime.market_data.recent_trade_items(symbol, recent_trades)
    open_order_rows = await session.execute(
        select(Order.side, User.username, func.count(), func.coalesce(func.sum(Order.remaining_quantity), 0))
        .join(User, User.id == Order.user_id)
        .where(
            Order.market_id == market.id,
            Order.status.in_(LIVE_ORDER_STATUSES),
        )
        .group_by(Order.side, User.username)
        .order_by(Order.side.asc(), User.username.asc())
    )
    open_order_summary = [
        {
            "side": side,
            "username": username,
            "open_order_count": int(order_count or 0),
            "remaining_quantity": decimal_to_str(remaining_qty),
        }
        for side, username, order_count, remaining_qty in open_order_rows.all()
    ]

    return {
        "symbol": symbol,
        "exported_at_ms": to_millis(datetime.now(tz=UTC)),
        "strategy_version": strategy_version,
        "market": serialize_market(market),
        "diagnosis": build_liquidity_diagnosis(metrics, book_summary, bid_owner, ask_owner),
        "market_top": {
            "bid": bid_owner,
            "ask": ask_owner,
        },
        "orderbook": {
            **book_summary,
            "stats": stats,
            "snapshot_depth": depth,
        },
        "liquidity_metrics": metrics,
        "configs": {
            "v2": {
                "effective": merge_liquidity_config(v2_live_config, v2_overrides),
                "live": v2_live_config,
                "overrides": v2_overrides,
            },
            "lite": {
                "effective": merge_liquidity_config(lite_live_config, lite_overrides),
                "live": lite_live_config,
                "overrides": lite_overrides,
            },
        },
        "recent_trades": recent_trade_items,
        "open_orders": {
            "by_user_side": open_order_summary,
        },
    }


@router.get("/ops/bots")
async def get_bot_metrics(
    request: Request,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market_rows = await session.execute(select(Market).order_by(Market.symbol.asc()))
    markets = list(market_rows.scalars())
    market_map = {market.symbol: market for market in markets}
    market_ids = [market.id for market in markets]

    rows = await session.execute(select(User).where(User.role == "mm_bot").order_by(User.username.asc()))
    mm_users = list(rows.scalars())
    mm_user_ids = [user.id for user in mm_users]

    bot_pair_rows = await session.execute(
        select(MarketBotAccount, User)
        .join(User, User.id == MarketBotAccount.user_id)
        .order_by(MarketBotAccount.market_id.asc(), MarketBotAccount.id.asc())
    )
    bot_pairs = list(bot_pair_rows.all())
    bot_user_ids = sorted({user.id for _, user in bot_pairs})
    all_user_ids = sorted(set(mm_user_ids) | set(bot_user_ids))

    balances_by_user: dict[int, list[Balance]] = {}
    if all_user_ids:
        balance_rows = await session.execute(select(Balance).where(Balance.user_id.in_(all_user_ids)))
        for balance in balance_rows.scalars():
            balances_by_user.setdefault(balance.user_id, []).append(balance)

    contract_accounts_by_user_asset: dict[tuple[int, str], ContractAccount] = {}
    if bot_user_ids:
        contract_rows = await session.execute(select(ContractAccount).where(ContractAccount.user_id.in_(bot_user_ids)))
        for account in contract_rows.scalars():
            contract_accounts_by_user_asset[(account.user_id, account.margin_asset.upper())] = account

    open_orders_by_user: dict[int, int] = {}
    if mm_user_ids:
        open_order_rows = await session.execute(
            select(Order.user_id, func.count())
            .where(Order.user_id.in_(mm_user_ids), Order.status.in_(LIVE_ORDER_STATUSES))
            .group_by(Order.user_id)
        )
        open_orders_by_user = {int(user_id): int(count) for user_id, count in open_order_rows.all()}

    market_open_orders_by_user: dict[tuple[int, int], int] = {}
    if market_ids and bot_user_ids:
        market_open_order_rows = await session.execute(
            select(Order.market_id, Order.user_id, func.count())
            .where(
                Order.market_id.in_(market_ids),
                Order.status.in_(LIVE_ORDER_STATUSES),
            )
            .group_by(Order.market_id, Order.user_id)
        )
        bot_user_id_set = set(bot_user_ids)
        market_open_orders_by_user = {
            (int(market_id), int(user_id)): int(count)
            for market_id, user_id, count in market_open_order_rows.all()
            if int(user_id) in bot_user_id_set
        }

    instance_rows = await session.execute(select(MarketMakerInstance).where(MarketMakerInstance.market_id.in_(market_ids)))
    instances_by_market = {instance.market_id: instance for instance in instance_rows.scalars()}
    for market in markets:
        instance = instances_by_market.get(market.id)
        if instance is None:
            instance = MarketMakerInstance(market_id=market.id, symbol=market.symbol, status="stopped")
            session.add(instance)
            instances_by_market[market.id] = instance
        elif instance.symbol != market.symbol:
            instance.symbol = market.symbol

    bots_by_market: dict[int, list[tuple[MarketBotAccount, User]]] = {}
    for bot, user in bot_pairs:
        bots_by_market.setdefault(bot.market_id, []).append((bot, user))

    items = []
    for user in mm_users:
        metrics = request.app.state.runtime.bot_metrics.get(user.username, {})
        items.append(
            {
                "username": user.username,
                "api_status": "ok" if user.is_active else "paused",
                "latest_order_latency_ms": metrics.get("place_order_ms"),
                "latest_cancel_latency_ms": metrics.get("cancel_order_ms"),
                "open_order_count": open_orders_by_user.get(user.id, 0),
                "inventory": [
                    {"asset": balance.asset, "available": str(balance.available), "frozen": str(balance.frozen)}
                    for balance in balances_by_user.get(user.id, [])
                ],
                "recent_trade_id": None,
                "last_heartbeat": metrics.get("last_heartbeat"),
            }
        )
    liquidity = []
    for symbol in sorted(request.app.state.runtime.liquidity_metrics):
        item = deepcopy(request.app.state.runtime.liquidity_metrics[symbol])
        market = market_map.get(symbol)
        if market is not None:
            for side_key, side in (("bid", "buy"), ("ask", "sell")):
                price_value = item.get("best_bid") if side == "buy" else item.get("best_ask")
                if not price_value:
                    continue
                rows = await session.execute(
                    select(User.username, User.role)
                    .join(Order, Order.user_id == User.id)
                    .where(
                        Order.market_id == market.id,
                        Order.status.in_(LIVE_ORDER_STATUSES),
                        Order.side == side,
                        Order.price == Decimal(str(price_value)),
                    )
                    .order_by(User.username.asc())
                )
                item.setdefault("market_top", {})[side_key] = summarize_level_owner(list(rows.all()))
        liquidity.append(item)
    market_workspaces = []
    for market in markets:
        selected, _, effective = await current_strategy_config(session, request, market)
        instance = instances_by_market[market.id]
        bots = []
        enabled_makers = 0
        enabled_flows = 0
        for bot, user in bots_by_market.get(market.id, []):
            item = serialize_market_bot_account_snapshot(
                market,
                bot,
                user,
                balances_by_user=balances_by_user,
                contract_accounts_by_user_asset=contract_accounts_by_user_asset,
            )
            account_metrics = request.app.state.runtime.bot_metrics.get(item["username"], {})
            item["open_order_count"] = market_open_orders_by_user.get((market.id, item["uid"]), 0)
            item["recent_trade_id"] = None
            item["last_heartbeat"] = account_metrics.get("last_heartbeat")
            item["api_status"] = "ok" if item.get("is_enabled") else "paused"
            bots.append(item)
            if item.get("is_enabled") and item.get("role") == "maker":
                enabled_makers += 1
            if item.get("is_enabled") and item.get("role") == "flow":
                enabled_flows += 1
        persisted_metrics = instance.last_metrics_json if isinstance(instance.last_metrics_json, dict) else {}
        metrics = deepcopy(request.app.state.runtime.liquidity_metrics.get(market.symbol, {}) or persisted_metrics or {})
        enabled_bots = [bot for bot in bots if bot.get("is_enabled")]
        start_readiness = build_maker_instance_start_readiness(
            market,
            {
                "strategy": {"strategy_key": selected.strategy_key},
                "bots": enabled_bots,
                "accounts": {
                    "makers": [bot for bot in enabled_bots if bot.get("role") == "maker"],
                    "flows": [bot for bot in enabled_bots if bot.get("role") == "flow"],
                },
                "warnings": [],
            },
        )
        instance_snapshot = maker_instance_status(
            market.symbol,
            request,
            configured_strategy_version=maker_instance_strategy_key(market, selected.strategy_key),
            instance=instance,
            start_readiness=start_readiness,
        )
        runtime = request.app.state.runtime
        orderbook, orderbook_seq, orderbook_ts = await runtime.orderbook_snapshot(
            market.symbol,
            coordinator_orderbook_depth(effective),
        )
        contract_snapshots = getattr(runtime, "contract_price_snapshots", {})
        coordinator = build_symbol_coordinator_state(
            market=market,
            strategy_config=effective,
            liquidity_metrics=metrics,
            orderbook=orderbook,
            instance_status=instance_snapshot,
            bots=bots,
            contract_price_snapshot=contract_snapshots.get(market.symbol.upper(), {}) if isinstance(contract_snapshots, dict) else {},
            source_counts=recent_trade_source_counts(request, market.symbol),
            persistence_metrics=(
                getattr(getattr(runtime, "persistence_writer", None), "metrics_snapshot", lambda: {})()
            ),
        )
        robot_cards = build_symbol_robot_cards(
            market=market,
            instance_status=instance_snapshot,
            bots=bots,
            coordinator=coordinator,
            log_lines=instance_snapshot.get("last_log_lines") if isinstance(instance_snapshot.get("last_log_lines"), list) else [],
        )
        market_workspaces.append(
            {
                "symbol": market.symbol,
                "market": serialize_market(market),
                "instance": instance_snapshot,
                "strategy": {
                    "strategy_key": selected.strategy_key,
                    "display_name": selected.strategy_key,
                    "effective_config": effective,
                },
                "bot_counts": {
                    "total": len(bots),
                    "enabled": sum(1 for bot in bots if bot.get("is_enabled")),
                    "makers": enabled_makers,
                    "flows": enabled_flows,
                },
                "bots": bots,
                "robot_cards": robot_cards,
                "coordinator": coordinator,
                "orderbook_invariants": {
                    **runtime.market_data.orderbook_invariant_snapshot(market.symbol),
                    **runtime.orderbook_reconcile_snapshot(market.symbol),
                    "snapshot_seq": orderbook_seq,
                    "snapshot_ts": orderbook_ts,
                },
                "liquidity_metrics": metrics,
            }
        )
    await session.commit()
    return {"items": items, "liquidity": liquidity, "markets": market_workspaces}


@router.get("/admin/liquidity/diagnostics")
async def get_liquidity_diagnostics(
    request: Request,
    symbol: str | None = Query(default=None),
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Run a centralized read-only liquidity and continuity diagnosis."""
    service = LiquidityDiagnosticsService(request.app.state.runtime)
    result = await service.diagnose(session, symbol=symbol)
    if symbol and not result["items"]:
        raise HTTPException(status_code=404, detail="market not found")
    return result
