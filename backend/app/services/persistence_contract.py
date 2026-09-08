"""Explicit persistence semantics for the unified liquidity platform.

一体化后的持久化语义只有一个契约（见 docs/做市流动性测试_一体化架构设计.md）：

* **事实 durable**：用户/机器人的订单、成交、余额、账本走 write-behind
  journal + materializer，跨重启可恢复；
* **机器人报价临时**：QuoteSet / 报价挂单是重启重建的临时态，只保留
  生命周期锚点，不整单落库；
* **有界保留**：retention/guard 定期裁剪已物化事件与历史行情。

历史模式字符串（``sampled`` / ``paper_exchange`` / ``strict_durable`` /
``memory``）保留为兼容别名，全部归一为上面的统一契约语义：
- ``sampled``、``memory``：事实不持久（运行态内存权威）——仅用于单元测试
  与旧部署兼容，不再作为平台运行形态；
- ``paper_exchange``、``durable``、``strict_durable``：事实持久——平台的
  唯一运行形态。

本模块提供语义化查询函数（``facts_durable`` 等）作为消除
``is_sampled_mode()`` / ``is_paper_exchange_mode()`` 分支的过渡层；旧函数
保持可用直到调用点全部迁移完毕。
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings


SAMPLED_MODE = "sampled"
STRICT_DURABLE_MODE = "strict_durable"
LEGACY_MEMORY_MODE = "memory"
PAPER_EXCHANGE_MODE = "paper_exchange"
UNIFIED_DURABLE_MODE = "durable"

_FACT_DURABLE_MODES = {STRICT_DURABLE_MODE, PAPER_EXCHANGE_MODE, UNIFIED_DURABLE_MODE}
_RUNTIME_MEMORY_MODES = {SAMPLED_MODE, LEGACY_MEMORY_MODE}


def persistence_mode(value: str | None = None) -> str:
    """Return a stable public mode name without changing the raw setting.

    ``durable`` 是一体化平台的新名称；``paper_exchange`` 是其兼容别名
    （两者语义等价）。``sampled`` / ``memory`` 仅保留给测试与旧部署。
    """
    raw = str(value if value is not None else settings.persistence_mode).strip().lower()
    if raw in {"strict", "strict-durable", "strict_durable"}:
        return STRICT_DURABLE_MODE
    if raw in {"durable", "unified", "paper_exchange", "paper", "paper-trading", "paper_trading"}:
        return UNIFIED_DURABLE_MODE
    if raw in {"memory", "legacy_memory"}:
        return LEGACY_MEMORY_MODE
    if raw == SAMPLED_MODE:
        return SAMPLED_MODE
    # Unknown values fail closed to the sampled contract.  The settings
    # validation path still exposes the original raw value for diagnostics.
    return SAMPLED_MODE


def is_sampled_mode(value: str | None = None) -> bool:
    return persistence_mode(value) == SAMPLED_MODE


def is_strict_durable_mode(value: str | None = None) -> bool:
    return persistence_mode(value) == STRICT_DURABLE_MODE


def is_paper_exchange_mode(value: str | None = None) -> bool:
    return persistence_mode(value) in {PAPER_EXCHANGE_MODE, UNIFIED_DURABLE_MODE}


def is_runtime_only_mode(value: str | None = None) -> bool:
    return persistence_mode(value) in {SAMPLED_MODE, LEGACY_MEMORY_MODE}


# ---------------------------------------------------------------------------
# 语义化查询（一体化契约的过渡层；调用点迁移完成后旧模式函数退役）
# ---------------------------------------------------------------------------


def facts_durable(value: str | None = None) -> bool:
    """订单/成交/余额/账本等业务事实是否持久化并可跨重启恢复。"""
    return persistence_mode(value) in _FACT_DURABLE_MODES


def runtime_memory_authoritative(value: str | None = None) -> bool:
    """运行态（盘口/余额镜像）是否仅存内存、重启不恢复。

    一体化契约下该语义只对测试/旧部署为真；平台运行态始终 durable。
    """
    return persistence_mode(value) in _RUNTIME_MEMORY_MODES


def legacy_sampled_runtime(value: str | None = None) -> bool:
    """旧 sampled 专属语义（仅 sampled，不含 memory）。

    sampled 是 5174 的历史运行形态（journal 丢弃、运行态内存权威），
    一体化后退役；本函数用于收敛期间标注"sampled 特有"的调用点，
    与 ``runtime_memory_authoritative``（sampled+memory）区分。
    """
    return persistence_mode(value) == SAMPLED_MODE


def journal_enabled(value: str | None = None) -> bool:
    """write-behind 事件日志是否启用（事实可回放/可对账）。"""
    return facts_durable(value)


def paper_product_enabled(value: str | None = None) -> bool:
    """白标产品面（注册/登录/上币/品牌）是否启用。

    一体化后产品面由市场 visibility 决定（listed 市场即产品面），本函数
    仅作为 bootstrap/前端门禁的过渡别名，最终退役。
    """
    return is_paper_exchange_mode(value)


def platform_durable_contract(value: str | None = None) -> bool:
    """平台运行契约（事实 durable + 机器人报价临时 + maker 补足语义）。

    区别于 ``facts_durable``：strict 全事件溯源模式不含报价临时语义，
    不属于平台契约；本函数只对 paper_exchange / durable 归一形态为真。
    """
    return is_paper_exchange_mode(value)


def public_persistence_contract(*, run_id: str | None = None, storage: dict[str, Any] | None = None) -> dict[str, Any]:
    """Small contract shared by health, public history and quote responses."""

    mode = persistence_mode()
    durable = facts_durable()
    payload: dict[str, Any] = {
        "persistence_mode": mode,
        "product_mode": "unified_platform" if is_paper_exchange_mode() else "market_making_sandbox",
        "run_id": run_id,
        "runtime_state_durable": durable,
        "history_sampled": not durable,
        "display_only_history": not durable,
    }
    if storage is not None:
        payload["storage"] = storage
    return payload


# Restart-ephemeral robot quote ladders（内置 paperq- / Lite mmv2- / PERP_MM
# perpmm-）。这些报价是重启重建的临时态，允许在 write-behind 重放中采用
# 系统账户补足语义；FLOW IOC（flowv2-/perpmm-flow-）与用户订单永不匹配。
EPHEMERAL_ROBOT_QUOTE_PREFIXES = ("paperq-", "mmv2-", "perpmm-")


def is_ephemeral_robot_quote_client_id(client_order_id: str | None) -> bool:
    value = str(client_order_id or "")
    if "-flow-" in value or value.startswith("flow"):
        return False
    return value.startswith(EPHEMERAL_ROBOT_QUOTE_PREFIXES)


def is_flow_client_order_id(client_order_id: str | None) -> bool:
    value = str(client_order_id or "")
    return "-flow-" in value or value.startswith("flow")


def is_paper_robot_topup_eligible(*, user_role: str | None, client_order_id: str | None) -> bool:
    """机器人报价重放的补足资格（maker 系统账户语义）。

    LITE 报价的 client_order_id 是裸 tag（如 ``sell-near-01``），不能只靠
    前缀识别；平台 durable 契约下 mm_bot 的 resting 订单必然是报价（FLOW
    的 IOC 从不 resting）。FLOW 成交订单显式排除，保证 FLOW 账户余额不被
    凭空补足（数据真实性）。strict（全事件溯源）模式不补足。
    """
    if not is_paper_exchange_mode():
        return False
    from app.core.constants import ROLE_BOT

    if str(user_role or "") != str(ROLE_BOT):
        return False
    return not is_flow_client_order_id(client_order_id)
