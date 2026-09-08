from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from time import perf_counter
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reconciliation_run import ReconciliationDifference, ReconciliationRun
from app.services.accounting_proof_service import verify_accounting_proof_chain
from app.services.robot_financial_checkpoint_service import verify_robot_financial_checkpoint_chain
from app.services.financial_outbox_service import verify_financial_outbox_replay_requests
from app.core.config import settings


@dataclass(frozen=True)
class ReconciliationCheck:
    code: str
    domain: str
    severity: str
    sql: str
    entity_type: str
    recommendation: str


ROBOT_PREDICATE = "(u.role = 'mm_bot' OR lower(u.username) LIKE '%_mm_%' OR lower(u.username) LIKE '%_flow_%')"
SYSTEM_PREDICATE = "(u.role = 'admin' OR lower(u.username) LIKE 'contract_liq_%')"


CHECKS = (
    ReconciliationCheck("spot_negative_balance", "spot", "blocking", "SELECT count(*) n FROM balances WHERE available < 0 OR frozen < 0", "balance", "停止相关账户交易并人工调查"),
    ReconciliationCheck("spot_chain_gap_customer", "spot", "blocking", f"""
        WITH w AS (SELECT l.*, lag(available_after) OVER(PARTITION BY user_id,asset ORDER BY id) pa,
          lag(frozen_after) OVER(PARTITION BY user_id,asset ORDER BY id) pf FROM ledger_entries l)
        SELECT count(*) n FROM w JOIN users u ON u.id=w.user_id WHERE pa IS NOT NULL
          AND (abs(available_before-pa)>0.00000001 OR abs(frozen_before-pf)>0.00000001)
          AND NOT {ROBOT_PREDICATE} AND NOT {SYSTEM_PREDICATE}
    """, "ledger", "客户链断点必须隔离，不得自动修账"),
    ReconciliationCheck("spot_chain_gap_robot", "robot", "warning", f"""
        WITH w AS (SELECT l.*, lag(available_after) OVER(PARTITION BY user_id,asset ORDER BY id) pa,
          lag(frozen_after) OVER(PARTITION BY user_id,asset ORDER BY id) pf FROM ledger_entries l)
        SELECT count(*) n FROM w JOIN users u ON u.id=w.user_id WHERE pa IS NOT NULL
          AND (abs(available_before-pa)>0.00000001 OR abs(frozen_before-pf)>0.00000001) AND {ROBOT_PREDICATE}
    """, "ledger", "保留原始差异，完成 checkpoint 前禁止裁剪"),
    ReconciliationCheck("spot_redundant_total_precision", "robot", "warning", f"""
        WITH w AS (SELECT l.*,lag(balance_after) OVER(PARTITION BY user_id,asset ORDER BY id) pb FROM ledger_entries l)
        SELECT count(*) n FROM w JOIN users u ON u.id=w.user_id WHERE pb IS NOT NULL
          AND abs(balance_before-pb)>0.00000001 AND {ROBOT_PREDICATE}
    """, "ledger", "SQLite NUMERIC 实际以 REAL 存储；总额冗余字段不可作为独立资金事实"),
    ReconciliationCheck("spot_latest_snapshot_mismatch", "spot", "blocking", """
        WITH latest AS (SELECT l.*,row_number() OVER(PARTITION BY user_id,asset ORDER BY id DESC) rn FROM ledger_entries l)
        SELECT count(*) n FROM balances b LEFT JOIN latest l ON l.user_id=b.user_id AND l.asset=b.asset AND l.rn=1
        WHERE (l.user_id IS NULL AND NOT EXISTS(SELECT 1 FROM accounting_transactions WHERE event_type='shadow_opening_balance' AND status='committed'))
          OR abs(b.available-l.available_after)>0.00000001 OR abs(b.frozen-l.frozen_after)>0.00000001
    """, "balance", "停止差异账户交易并输出显式调整方案"),
    ReconciliationCheck("spot_orphan_ledger_order", "spot", "warning", "SELECT count(*) n FROM ledger_entries l LEFT JOIN orders o ON o.order_id=l.related_order_id WHERE l.related_order_id IS NOT NULL AND o.order_id IS NULL", "ledger", "归因历史裁剪并保留隔离报告"),
    ReconciliationCheck("spot_orphan_ledger_trade", "spot", "warning", "SELECT count(*) n FROM ledger_entries l LEFT JOIN trades t ON t.trade_id=l.related_trade_id WHERE l.related_trade_id IS NOT NULL AND t.trade_id IS NULL", "ledger", "归因历史裁剪并保留隔离报告"),
    ReconciliationCheck("trade_missing_order", "orders", "warning", "SELECT count(*) n FROM trades t LEFT JOIN orders a ON a.order_id=t.taker_order_id LEFT JOIN orders b ON b.order_id=t.maker_order_id WHERE a.order_id IS NULL OR b.order_id IS NULL", "trade", "checkpoint 完成前禁止订单成交裁剪"),
    ReconciliationCheck("duplicate_live_client_order", "orders", "blocking", "SELECT count(*) n FROM (SELECT user_id,market_id,product_type,client_order_id FROM orders WHERE client_order_id IS NOT NULL AND status IN ('new','partially_filled') GROUP BY 1,2,3,4 HAVING count(*)>1)", "order", "依赖数据库唯一约束拒绝重复请求"),
    ReconciliationCheck("spot_legacy_trade_missing_settlement", "spot", "warning", """
        SELECT count(*) n FROM (SELECT t.trade_id FROM trades t LEFT JOIN ledger_entries l ON l.related_trade_id=t.trade_id AND l.change_type='trade_settlement'
        WHERE t.product_type='SPOT' AND t.business_key LIKE 'legacy:%' GROUP BY t.trade_id HAVING count(l.id)<4)
    """, "trade", "历史裁剪导致证据缺失；保留隔离报告且禁止继续裁剪"),
    ReconciliationCheck("spot_current_trade_missing_settlement", "spot", "blocking", """
        SELECT count(*) n FROM (SELECT t.trade_id FROM trades t LEFT JOIN ledger_entries l ON l.related_trade_id=t.trade_id AND l.change_type='trade_settlement'
        WHERE t.product_type='SPOT' AND t.business_key NOT LIKE 'legacy:%' GROUP BY t.trade_id HAVING count(l.id)<4)
    """, "trade", "隔离缺少 maker/taker 双边结算证据的新成交"),
    ReconciliationCheck("contract_account_formula", "contract", "blocking", "SELECT count(*) n FROM contract_accounts WHERE abs(available_margin-(wallet_balance+unrealized_pnl-used_margin))>0.00001 OR wallet_balance<0 OR used_margin<0", "contract_account", "停止相关保证金账户交易并调查"),
    ReconciliationCheck("contract_account_formula_sqlite_precision", "contract", "warning", "SELECT count(*) n FROM contract_accounts WHERE abs(available_margin-(wallet_balance+unrealized_pnl-used_margin))>0.00000001 AND abs(available_margin-(wallet_balance+unrealized_pnl-used_margin))<=0.00001 AND wallet_balance>=0 AND used_margin>=0", "contract_account", "保留 SQLite REAL 公式舍入差异；固定精度迁移前不得宣称逐原子精确"),
    ReconciliationCheck("contract_chain_gap_customer", "contract", "blocking", f"""
        WITH w AS (SELECT l.*,lag(wallet_after) OVER(PARTITION BY user_id,margin_asset ORDER BY id) pw,
          lag(available_after) OVER(PARTITION BY user_id,margin_asset ORDER BY id) pa,lag(used_margin_after) OVER(PARTITION BY user_id,margin_asset ORDER BY id) pu,
          lag(unrealized_pnl_after) OVER(PARTITION BY user_id,margin_asset ORDER BY id) pun,lag(realized_pnl_after) OVER(PARTITION BY user_id,margin_asset ORDER BY id) pr,
          lag(total_fees_after) OVER(PARTITION BY user_id,margin_asset ORDER BY id) pf FROM contract_ledger_entries l)
        SELECT count(*) n FROM w JOIN users u ON u.id=w.user_id WHERE pw IS NOT NULL AND
          (abs(wallet_before-pw)>0.00000001 OR abs(available_before-pa)>0.00000001 OR abs(used_margin_before-pu)>0.00000001 OR abs(unrealized_pnl_before-pun)>0.00000001 OR abs(realized_pnl_before-pr)>0.00000001 OR abs(total_fees_before-pf)>0.00000001)
          AND NOT {ROBOT_PREDICATE} AND NOT {SYSTEM_PREDICATE}
    """, "contract_ledger", "客户合约链断点必须隔离，不得自动修账"),
    ReconciliationCheck("contract_chain_gap_robot", "robot", "warning", f"""
        WITH w AS (SELECT l.*,lag(wallet_after) OVER(PARTITION BY user_id,margin_asset ORDER BY id) pw,
          lag(available_after) OVER(PARTITION BY user_id,margin_asset ORDER BY id) pa,lag(used_margin_after) OVER(PARTITION BY user_id,margin_asset ORDER BY id) pu,
          lag(unrealized_pnl_after) OVER(PARTITION BY user_id,margin_asset ORDER BY id) pun,lag(realized_pnl_after) OVER(PARTITION BY user_id,margin_asset ORDER BY id) pr,
          lag(total_fees_after) OVER(PARTITION BY user_id,margin_asset ORDER BY id) pf FROM contract_ledger_entries l)
        SELECT count(*) n FROM w JOIN users u ON u.id=w.user_id WHERE pw IS NOT NULL AND
          (abs(wallet_before-pw)>0.00000001 OR abs(available_before-pa)>0.00000001 OR abs(used_margin_before-pu)>0.00000001 OR abs(unrealized_pnl_before-pun)>0.00000001 OR abs(realized_pnl_before-pr)>0.00000001 OR abs(total_fees_before-pf)>0.00000001)
          AND {ROBOT_PREDICATE}
    """, "contract_ledger", "历史机器人差异保留隔离报告；新锁域防止继续产生"),
    ReconciliationCheck("contract_latest_snapshot_mismatch", "contract", "blocking", """
        WITH latest AS (SELECT l.*,row_number() OVER(PARTITION BY user_id,margin_asset ORDER BY id DESC) rn FROM contract_ledger_entries l)
        SELECT count(*) n FROM contract_accounts a LEFT JOIN latest l ON l.user_id=a.user_id AND l.margin_asset=a.margin_asset AND l.rn=1
        WHERE l.user_id IS NULL OR abs(a.wallet_balance-l.wallet_after)>0.00000001 OR abs(a.available_margin-l.available_after)>0.00000001 OR abs(a.used_margin-l.used_margin_after)>0.00000001 OR abs(a.unrealized_pnl-l.unrealized_pnl_after)>0.00000001 OR abs(a.realized_pnl-l.realized_pnl_after)>0.00000001 OR abs(a.total_fees-l.total_fees_after)>0.00000001
    """, "contract_account", "停止差异账户交易并输出显式调整方案"),
    ReconciliationCheck("contract_duplicate_position", "contract", "blocking", "SELECT count(*) n FROM (SELECT user_id,market_id,side FROM contract_positions GROUP BY 1,2,3 HAVING count(*)>1)", "position", "拒绝非法重复持仓"),
    ReconciliationCheck("contract_legacy_trade_missing_ledger", "contract", "warning", """
        SELECT count(*) n FROM (SELECT t.trade_id FROM trades t LEFT JOIN contract_ledger_entries l ON l.related_trade_id=t.trade_id
        WHERE t.product_type='PERP' AND t.business_key LIKE 'legacy:%' GROUP BY t.trade_id HAVING count(l.id)<2)
    """, "trade", "历史裁剪导致证据缺失；保留隔离报告且禁止继续裁剪"),
    ReconciliationCheck("contract_current_trade_missing_ledger", "contract", "blocking", """
        SELECT count(*) n FROM (SELECT t.trade_id FROM trades t LEFT JOIN contract_ledger_entries l ON l.related_trade_id=t.trade_id
        WHERE t.product_type='PERP' AND t.business_key NOT LIKE 'legacy:%' GROUP BY t.trade_id HAVING count(l.id)<2)
    """, "trade", "隔离缺少双边保证金账务证据的新成交"),
    ReconciliationCheck("funding_settlement_mismatch", "funding", "blocking", """
        SELECT count(*) n FROM (SELECT s.id FROM contract_funding_settlements s
        LEFT JOIN contract_funding_events e ON e.market_id=s.market_id AND e.funding_time=s.funding_time
        LEFT JOIN contract_ledger_entries l ON l.related_event_id=e.event_id
        GROUP BY s.id HAVING s.status!='settled' OR s.settled_count!=count(DISTINCT e.event_id) OR count(DISTINCT e.event_id)!=count(DISTINCT l.entry_id))
    """, "funding_settlement", "批次保持失败态并人工调查，禁止部分完成"),
    ReconciliationCheck("funding_unallocated_rounding", "funding", "blocking", "SELECT count(*) n FROM (SELECT market_id,funding_time FROM contract_funding_events GROUP BY 1,2 HAVING abs(sum(amount))>0.00000001)", "funding_settlement", "差额必须进入明确 rounding account"),
    ReconciliationCheck("insurance_chain_gap", "liquidation", "blocking", """
        WITH w AS (SELECT e.*,lag(balance_after) OVER(PARTITION BY margin_asset ORDER BY created_at,id) p FROM contract_insurance_events e)
        SELECT count(*) n FROM w WHERE p IS NOT NULL AND balance_before!=p
    """, "insurance_event", "停止保险基金变更并调查链断点"),
    ReconciliationCheck("liquidation_missing_ledger", "liquidation", "blocking", "SELECT count(*) n FROM contract_liquidation_events e LEFT JOIN contract_ledger_entries l ON l.related_event_id=e.event_id WHERE l.id IS NULL", "liquidation", "隔离缺少账户流水的强平事件"),
    ReconciliationCheck("adl_missing_ledger", "liquidation", "blocking", "SELECT count(*) n FROM contract_adl_events e LEFT JOIN contract_ledger_entries l ON l.related_event_id=e.event_id WHERE l.id IS NULL", "adl_event", "隔离缺少账户流水的 ADL 事件"),
    ReconciliationCheck("liquidation_insurance_adl_conservation", "liquidation", "blocking", """
        WITH insurance AS (
          SELECT related_liquidation_event_id event_id,
            sum(CASE WHEN event_type='bad_debt_cover' THEN -amount ELSE 0 END) covered
          FROM contract_insurance_events GROUP BY related_liquidation_event_id
        ), adl AS (
          SELECT liquidation_event_id event_id,sum(covered_amount) covered
          FROM contract_adl_events GROUP BY liquidation_event_id
        )
        SELECT count(*) n FROM contract_liquidation_events e
        LEFT JOIN insurance i ON i.event_id=e.event_id LEFT JOIN adl a ON a.event_id=e.event_id
        WHERE abs(e.insurance_covered-coalesce(i.covered,0))>0.00000001
          OR abs(e.adl_covered-coalesce(a.covered,0))>0.00000001
          OR abs(e.residual_bad_debt-(e.adl_covered+e.adl_residual))>0.00000001
    """, "liquidation", "强平坏账、保险基金、ADL 与剩余缺口必须守恒"),
    ReconciliationCheck("accounting_opening_missing", "accounting", "blocking", """
        SELECT CASE WHEN EXISTS(SELECT 1 FROM balances WHERE available!=0 OR frozen!=0)
          OR EXISTS(SELECT 1 FROM contract_accounts WHERE wallet_balance!=0 OR available_margin!=0 OR used_margin!=0 OR unrealized_pnl!=0 OR realized_pnl!=0 OR total_fees!=0)
          OR EXISTS(SELECT 1 FROM contract_insurance_funds WHERE balance!=0)
        THEN CASE WHEN EXISTS(SELECT 1 FROM accounting_transactions WHERE event_type='shadow_opening_balance') THEN 0 ELSE 1 END ELSE 0 END n
    """, "accounting_checkpoint", "没有显式开账锚点时不得声称影子账本可重建"),
    ReconciliationCheck("accounting_spot_snapshot_mismatch", "accounting", "blocking", """
        WITH a AS (
          SELECT owner_id,asset,
            sum(CASE WHEN account_code='customer_spot_available' THEN quantity ELSE 0 END) available,
            sum(CASE WHEN account_code='customer_spot_frozen' THEN quantity ELSE 0 END) frozen
          FROM accounting_entries WHERE account_domain='spot' AND owner_type='customer' GROUP BY owner_id,asset
        )
        SELECT count(*) n FROM balances b LEFT JOIN a ON a.owner_id=CAST(b.user_id AS TEXT) AND a.asset=b.asset
        WHERE abs(b.available-coalesce(a.available,0))>0.00001 OR abs(b.frozen-coalesce(a.frozen,0))>0.00001
    """, "balance", "影子复式重建现货快照不一致时禁止提升 source-of-truth"),
    ReconciliationCheck("accounting_spot_snapshot_sqlite_precision", "accounting", "warning", """
        WITH a AS (
          SELECT owner_id,asset,
            sum(CASE WHEN account_code='customer_spot_available' THEN quantity ELSE 0 END) available,
            sum(CASE WHEN account_code='customer_spot_frozen' THEN quantity ELSE 0 END) frozen
          FROM accounting_entries WHERE account_domain='spot' AND owner_type='customer' GROUP BY owner_id,asset
        )
        SELECT count(*) n FROM balances b LEFT JOIN a ON a.owner_id=CAST(b.user_id AS TEXT) AND a.asset=b.asset
        WHERE (abs(b.available-coalesce(a.available,0))>0.00000001 AND abs(b.available-coalesce(a.available,0))<=0.00001)
           OR (abs(b.frozen-coalesce(a.frozen,0))>0.00000001 AND abs(b.frozen-coalesce(a.frozen,0))<=0.00001)
    """, "balance", "保留 SQLite REAL 精度差异；不得将其当成资金调整或 source-of-truth 已精确迁移"),
    ReconciliationCheck("accounting_contract_snapshot_mismatch", "accounting", "blocking", """
        WITH a AS (
          SELECT owner_id,asset,
            sum(CASE WHEN account_code='customer_contract_wallet' THEN quantity ELSE 0 END) wallet,
            sum(CASE WHEN account_code='customer_contract_available' THEN quantity ELSE 0 END) available,
            sum(CASE WHEN account_code='customer_margin_used' THEN quantity ELSE 0 END) used_margin,
            sum(CASE WHEN account_code='customer_contract_unrealized_pnl' THEN quantity ELSE 0 END) unrealized,
            sum(CASE WHEN account_code='customer_contract_realized_pnl' THEN quantity ELSE 0 END) realized,
            sum(CASE WHEN account_code='customer_contract_total_fees' THEN quantity ELSE 0 END) fees
          FROM accounting_entries WHERE account_domain='contract' AND owner_type='customer' GROUP BY owner_id,asset
        )
        SELECT count(*) n FROM contract_accounts c LEFT JOIN a ON a.owner_id=CAST(c.user_id AS TEXT) AND a.asset=c.margin_asset
        WHERE abs(c.wallet_balance-coalesce(a.wallet,0))>0.00001 OR abs(c.available_margin-coalesce(a.available,0))>0.00001
          OR abs(c.used_margin-coalesce(a.used_margin,0))>0.00001 OR abs(c.unrealized_pnl-coalesce(a.unrealized,0))>0.00001
          OR abs(c.realized_pnl-coalesce(a.realized,0))>0.00001 OR abs(c.total_fees-coalesce(a.fees,0))>0.00001
    """, "contract_account", "影子复式重建合约快照不一致时禁止提升 source-of-truth"),
    ReconciliationCheck("accounting_contract_snapshot_sqlite_precision", "accounting", "warning", """
        WITH a AS (
          SELECT owner_id,asset,
            sum(CASE WHEN account_code='customer_contract_wallet' THEN quantity ELSE 0 END) wallet,
            sum(CASE WHEN account_code='customer_contract_available' THEN quantity ELSE 0 END) available,
            sum(CASE WHEN account_code='customer_margin_used' THEN quantity ELSE 0 END) used_margin,
            sum(CASE WHEN account_code='customer_contract_unrealized_pnl' THEN quantity ELSE 0 END) unrealized,
            sum(CASE WHEN account_code='customer_contract_realized_pnl' THEN quantity ELSE 0 END) realized,
            sum(CASE WHEN account_code='customer_contract_total_fees' THEN quantity ELSE 0 END) fees
          FROM accounting_entries WHERE account_domain='contract' AND owner_type='customer' GROUP BY owner_id,asset
        )
        SELECT count(*) n FROM contract_accounts c LEFT JOIN a ON a.owner_id=CAST(c.user_id AS TEXT) AND a.asset=c.margin_asset
        WHERE (abs(c.wallet_balance-coalesce(a.wallet,0))>0.00000001 AND abs(c.wallet_balance-coalesce(a.wallet,0))<=0.00001)
           OR (abs(c.available_margin-coalesce(a.available,0))>0.00000001 AND abs(c.available_margin-coalesce(a.available,0))<=0.00001)
           OR (abs(c.used_margin-coalesce(a.used_margin,0))>0.00000001 AND abs(c.used_margin-coalesce(a.used_margin,0))<=0.00001)
           OR (abs(c.unrealized_pnl-coalesce(a.unrealized,0))>0.00000001 AND abs(c.unrealized_pnl-coalesce(a.unrealized,0))<=0.00001)
           OR (abs(c.realized_pnl-coalesce(a.realized,0))>0.00000001 AND abs(c.realized_pnl-coalesce(a.realized,0))<=0.00001)
           OR (abs(c.total_fees-coalesce(a.fees,0))>0.00000001 AND abs(c.total_fees-coalesce(a.fees,0))<=0.00001)
    """, "contract_account", "保留 SQLite REAL 精度差异；生产资金核心必须迁移固定精度存储"),
    ReconciliationCheck("accounting_insurance_snapshot_mismatch", "accounting", "blocking", """
        WITH a AS (
          SELECT asset,sum(quantity) balance FROM accounting_entries
          WHERE account_domain='insurance' AND owner_id='insurance_fund' AND account_code='system_insurance_fund' GROUP BY asset
        )
        SELECT count(*) n FROM contract_insurance_funds f LEFT JOIN a ON a.asset=f.margin_asset
        WHERE abs(f.balance-coalesce(a.balance,0))>0.00000001
    """, "insurance_fund", "影子复式重建保险基金不一致时停止清算资金变更"),
    ReconciliationCheck("accounting_spot_post_cutover_missing", "accounting", "blocking", """
        WITH cutoff AS (SELECT max(CAST(json_extract(metadata_json,'$.spot_ledger_max_id') AS INTEGER)) id FROM accounting_transactions WHERE event_type='shadow_opening_balance')
        SELECT count(*) n FROM ledger_entries l CROSS JOIN cutoff c LEFT JOIN accounting_transactions t ON t.source_ledger_entry_id=l.entry_id
        WHERE l.id>coalesce(c.id,0) AND t.transaction_id IS NULL
    """, "ledger", "开账后每条现货流水必须同事务进入影子复式"),
    ReconciliationCheck("accounting_contract_post_cutover_missing", "accounting", "blocking", """
        WITH cutoff AS (SELECT max(CAST(json_extract(metadata_json,'$.contract_ledger_max_id') AS INTEGER)) id FROM accounting_transactions WHERE event_type='shadow_opening_balance')
        SELECT count(*) n FROM contract_ledger_entries l CROSS JOIN cutoff c LEFT JOIN accounting_transactions t ON t.source_ledger_entry_id=l.entry_id
        WHERE l.id>coalesce(c.id,0) AND t.transaction_id IS NULL
    """, "contract_ledger", "开账后每条合约流水必须同事务进入影子复式"),
    ReconciliationCheck("accounting_orphan_spot_source", "accounting", "blocking", """
        SELECT count(*) n FROM accounting_transactions t
        LEFT JOIN ledger_entries l ON l.entry_id=coalesce(
          t.source_ledger_entry_id,json_extract(t.metadata_json,'$.legacy_ledger_entry_id')
        )
        WHERE json_type(t.metadata_json,'$.legacy_ledger_entry_id') IS NOT NULL
          AND t.status='committed' AND l.entry_id IS NULL
    """, "accounting_transaction", "隔离引用不存在现货流水的复式交易；禁止用孤儿 source 证明余额"),
    ReconciliationCheck("accounting_orphan_contract_source", "accounting", "blocking", """
        SELECT count(*) n FROM accounting_transactions t
        LEFT JOIN contract_ledger_entries l ON l.entry_id=coalesce(
          t.source_ledger_entry_id,json_extract(t.metadata_json,'$.legacy_contract_ledger_entry_id')
        )
        WHERE json_type(t.metadata_json,'$.legacy_contract_ledger_entry_id') IS NOT NULL
          AND t.status='committed' AND l.entry_id IS NULL
    """, "accounting_transaction", "隔离引用不存在合约流水的复式交易；禁止用孤儿 source 证明保证金"),
    ReconciliationCheck("accounting_spot_source_delta_mismatch", "accounting", "blocking", """
        WITH cutoff AS (SELECT max(CAST(json_extract(metadata_json,'$.spot_ledger_max_id') AS INTEGER)) id FROM accounting_transactions WHERE event_type='shadow_opening_balance'),
        q AS (
          SELECT t.transaction_id,t.source_ledger_entry_id,
            sum(CASE WHEN e.account_code='customer_spot_available' THEN e.quantity ELSE 0 END) available_delta,
            sum(CASE WHEN e.account_code='customer_spot_frozen' THEN e.quantity ELSE 0 END) frozen_delta
          FROM accounting_transactions t JOIN accounting_entries e ON e.transaction_id=t.transaction_id
          WHERE t.source_ledger_entry_id IS NOT NULL AND t.event_type!='voided_legacy_noop'
          GROUP BY t.transaction_id,t.source_ledger_entry_id
        )
        SELECT count(*) n FROM ledger_entries l CROSS JOIN cutoff c JOIN q ON q.source_ledger_entry_id=l.entry_id
        LEFT JOIN accounting_transactions r ON r.reversal_of_transaction_id=q.transaction_id
        WHERE l.id>coalesce(c.id,0) AND r.transaction_id IS NULL
          AND (abs((l.available_after-l.available_before)-q.available_delta)>0.00001
            OR abs((l.frozen_after-l.frozen_before)-q.frozen_delta)>0.00001)
    """, "accounting_transaction", "业务流水分量与复式 source transaction 不一致，必须显式 reversal"),
    ReconciliationCheck("accounting_spot_source_sqlite_precision", "accounting", "warning", """
        WITH cutoff AS (SELECT max(CAST(json_extract(metadata_json,'$.spot_ledger_max_id') AS INTEGER)) id FROM accounting_transactions WHERE event_type='shadow_opening_balance'),
        q AS (
          SELECT t.transaction_id,t.source_ledger_entry_id,
            sum(CASE WHEN e.account_code='customer_spot_available' THEN e.quantity ELSE 0 END) available_delta,
            sum(CASE WHEN e.account_code='customer_spot_frozen' THEN e.quantity ELSE 0 END) frozen_delta
          FROM accounting_transactions t JOIN accounting_entries e ON e.transaction_id=t.transaction_id
          WHERE t.source_ledger_entry_id IS NOT NULL AND t.event_type!='voided_legacy_noop'
          GROUP BY t.transaction_id,t.source_ledger_entry_id
        )
        SELECT count(*) n FROM ledger_entries l CROSS JOIN cutoff c JOIN q ON q.source_ledger_entry_id=l.entry_id
        LEFT JOIN accounting_transactions r ON r.reversal_of_transaction_id=q.transaction_id
        WHERE l.id>coalesce(c.id,0) AND r.transaction_id IS NULL AND (
          (abs((l.available_after-l.available_before)-q.available_delta)>0.00000001 AND abs((l.available_after-l.available_before)-q.available_delta)<=0.00001) OR
          (abs((l.frozen_after-l.frozen_before)-q.frozen_delta)>0.00000001 AND abs((l.frozen_after-l.frozen_before)-q.frozen_delta)<=0.00001))
    """, "accounting_transaction", "保留 SQLite REAL 绑定前后造成的微量 source 差异；新流水改用持久化后差额"),
    ReconciliationCheck("accounting_contract_source_delta_mismatch", "accounting", "blocking", """
        WITH cutoff AS (SELECT max(CAST(json_extract(metadata_json,'$.contract_ledger_max_id') AS INTEGER)) id FROM accounting_transactions WHERE event_type='shadow_opening_balance'),
        q AS (
          SELECT t.transaction_id,t.source_ledger_entry_id,
            sum(CASE WHEN e.account_code='customer_contract_wallet' THEN e.quantity ELSE 0 END) wallet_delta,
            sum(CASE WHEN e.account_code='customer_contract_available' THEN e.quantity ELSE 0 END) available_delta,
            sum(CASE WHEN e.account_code='customer_margin_used' THEN e.quantity ELSE 0 END) used_delta,
            sum(CASE WHEN e.account_code='customer_contract_unrealized_pnl' THEN e.quantity ELSE 0 END) unrealized_delta,
            sum(CASE WHEN e.account_code='customer_contract_realized_pnl' THEN e.quantity ELSE 0 END) realized_delta,
            sum(CASE WHEN e.account_code='customer_contract_total_fees' THEN e.quantity ELSE 0 END) fees_delta
          FROM accounting_transactions t JOIN accounting_entries e ON e.transaction_id=t.transaction_id
          WHERE t.source_ledger_entry_id IS NOT NULL AND t.event_type!='voided_legacy_noop'
          GROUP BY t.transaction_id,t.source_ledger_entry_id
        )
        SELECT count(*) n FROM contract_ledger_entries l CROSS JOIN cutoff c JOIN q ON q.source_ledger_entry_id=l.entry_id
        LEFT JOIN accounting_transactions r ON r.reversal_of_transaction_id=q.transaction_id
        WHERE l.id>coalesce(c.id,0) AND r.transaction_id IS NULL AND (
          abs((l.wallet_after-l.wallet_before)-q.wallet_delta)>0.00001 OR
          abs((l.available_after-l.available_before)-q.available_delta)>0.00001 OR
          abs((l.used_margin_after-l.used_margin_before)-q.used_delta)>0.00001 OR
          abs((l.unrealized_pnl_after-l.unrealized_pnl_before)-q.unrealized_delta)>0.00001 OR
          abs((l.realized_pnl_after-l.realized_pnl_before)-q.realized_delta)>0.00001 OR
          abs((l.total_fees_after-l.total_fees_before)-q.fees_delta)>0.00001)
    """, "accounting_transaction", "合约流水六维分量与复式 source transaction 不一致，必须显式 reversal"),
    ReconciliationCheck("accounting_contract_source_sqlite_precision", "accounting", "warning", """
        WITH cutoff AS (SELECT max(CAST(json_extract(metadata_json,'$.contract_ledger_max_id') AS INTEGER)) id FROM accounting_transactions WHERE event_type='shadow_opening_balance'),
        q AS (
          SELECT t.transaction_id,t.source_ledger_entry_id,
            sum(CASE WHEN e.account_code='customer_contract_wallet' THEN e.quantity ELSE 0 END) wallet_delta,
            sum(CASE WHEN e.account_code='customer_contract_available' THEN e.quantity ELSE 0 END) available_delta,
            sum(CASE WHEN e.account_code='customer_margin_used' THEN e.quantity ELSE 0 END) used_delta,
            sum(CASE WHEN e.account_code='customer_contract_unrealized_pnl' THEN e.quantity ELSE 0 END) unrealized_delta,
            sum(CASE WHEN e.account_code='customer_contract_realized_pnl' THEN e.quantity ELSE 0 END) realized_delta,
            sum(CASE WHEN e.account_code='customer_contract_total_fees' THEN e.quantity ELSE 0 END) fees_delta
          FROM accounting_transactions t JOIN accounting_entries e ON e.transaction_id=t.transaction_id
          WHERE t.source_ledger_entry_id IS NOT NULL AND t.event_type!='voided_legacy_noop'
          GROUP BY t.transaction_id,t.source_ledger_entry_id
        )
        SELECT count(*) n FROM contract_ledger_entries l CROSS JOIN cutoff c JOIN q ON q.source_ledger_entry_id=l.entry_id
        LEFT JOIN accounting_transactions r ON r.reversal_of_transaction_id=q.transaction_id
        WHERE l.id>coalesce(c.id,0) AND r.transaction_id IS NULL AND (
          (abs((l.wallet_after-l.wallet_before)-q.wallet_delta)>0.00000001 AND abs((l.wallet_after-l.wallet_before)-q.wallet_delta)<=0.00001) OR
          (abs((l.available_after-l.available_before)-q.available_delta)>0.00000001 AND abs((l.available_after-l.available_before)-q.available_delta)<=0.00001) OR
          (abs((l.used_margin_after-l.used_margin_before)-q.used_delta)>0.00000001 AND abs((l.used_margin_after-l.used_margin_before)-q.used_delta)<=0.00001) OR
          (abs((l.unrealized_pnl_after-l.unrealized_pnl_before)-q.unrealized_delta)>0.00000001 AND abs((l.unrealized_pnl_after-l.unrealized_pnl_before)-q.unrealized_delta)<=0.00001) OR
          (abs((l.realized_pnl_after-l.realized_pnl_before)-q.realized_delta)>0.00000001 AND abs((l.realized_pnl_after-l.realized_pnl_before)-q.realized_delta)<=0.00001) OR
          (abs((l.total_fees_after-l.total_fees_before)-q.fees_delta)>0.00000001 AND abs((l.total_fees_after-l.total_fees_before)-q.fees_delta)<=0.00001))
    """, "accounting_transaction", "保留 SQLite REAL 绑定前后造成的微量六维 source 差异；新流水改用持久化后差额"),
    ReconciliationCheck("accounting_insurance_post_cutover_missing", "accounting", "blocking", """
        WITH cutoff AS (SELECT max(CAST(json_extract(metadata_json,'$.insurance_event_max_id') AS INTEGER)) id FROM accounting_transactions WHERE event_type='shadow_opening_balance')
        SELECT count(*) n FROM contract_insurance_events e CROSS JOIN cutoff c LEFT JOIN accounting_transactions t ON t.source_event_id=e.event_id
        WHERE e.id>coalesce(c.id,0) AND t.transaction_id IS NULL
    """, "insurance_event", "开账后每条保险基金事件必须同事务进入影子复式"),
    ReconciliationCheck("accounting_funding_event_post_cutover_missing", "accounting", "blocking", """
        WITH cutoff AS (SELECT max(CAST(json_extract(metadata_json,'$.funding_event_max_id') AS INTEGER)) id FROM accounting_transactions WHERE event_type='shadow_opening_balance')
        SELECT count(*) n FROM contract_funding_events e CROSS JOIN cutoff c
        LEFT JOIN accounting_transactions t ON t.source_event_id=e.event_id AND t.event_type='funding_fee'
        WHERE e.id>coalesce(c.id,0) AND t.transaction_id IS NULL
    """, "funding_event", "开账后资金费事件必须可追踪到复式交易和 settlement batch"),
    ReconciliationCheck("accounting_liquidation_event_post_cutover_missing", "accounting", "blocking", """
        WITH cutoff AS (SELECT max(CAST(json_extract(metadata_json,'$.liquidation_event_max_id') AS INTEGER)) id FROM accounting_transactions WHERE event_type='shadow_opening_balance')
        SELECT count(*) n FROM contract_liquidation_events e CROSS JOIN cutoff c
        LEFT JOIN accounting_transactions t ON t.source_event_id=e.event_id AND t.event_type='liquidation'
        WHERE e.id>coalesce(c.id,0) AND t.transaction_id IS NULL
    """, "liquidation", "开账后强平事件必须可追踪到复式交易"),
    ReconciliationCheck("accounting_adl_event_post_cutover_missing", "accounting", "blocking", """
        WITH cutoff AS (SELECT max(CAST(json_extract(metadata_json,'$.adl_event_max_id') AS INTEGER)) id FROM accounting_transactions WHERE event_type='shadow_opening_balance')
        SELECT count(*) n FROM contract_adl_events e CROSS JOIN cutoff c
        LEFT JOIN accounting_transactions t ON t.source_event_id=e.event_id AND t.event_type='adl_deleverage'
        WHERE e.id>coalesce(c.id,0) AND t.transaction_id IS NULL
    """, "adl_event", "开账后 ADL 事件必须可追踪到复式交易"),
    ReconciliationCheck("accounting_voided_legacy_noop", "accounting", "warning", """
        SELECT count(*) n FROM accounting_transactions WHERE event_type='voided_legacy_noop'
    """, "accounting_transaction", "保留无资金变化旧事件及失败 run 证据；不得计入客户余额重建"),
    ReconciliationCheck("accounting_reversed_noop_shadow", "accounting", "warning", """
        SELECT count(*) n FROM accounting_transactions
        WHERE event_type='reversal' AND json_extract(metadata_json,'$.classification')='noop_shadow_mismatch'
    """, "accounting_transaction", "保留已显式 reversal 的无业务效果影子交易，不修改原记录"),
    ReconciliationCheck("accounting_unbalanced", "accounting", "blocking", """
        SELECT count(*) n FROM (SELECT transaction_id,asset FROM accounting_entries GROUP BY transaction_id,asset HAVING abs(sum(debit)-sum(credit))>0.00000001)
    """, "accounting_transaction", "影子复式账本不平衡时立即阻断提升 source-of-truth"),
    ReconciliationCheck("outbox_checkpoint_missing", "outbox", "blocking", """
        SELECT CASE WHEN EXISTS(SELECT 1 FROM ledger_entries) OR EXISTS(SELECT 1 FROM contract_ledger_entries)
          THEN CASE WHEN EXISTS(SELECT 1 FROM outbox_checkpoints WHERE checkpoint_name='financial-outbox-v1') THEN 0 ELSE 1 END
          ELSE 0 END n
    """, "outbox_checkpoint", "没有 cutover checkpoint 时不得声称资金事件可恢复"),
    ReconciliationCheck("outbox_spot_post_cutover_missing", "outbox", "blocking", """
        WITH cutoff AS (SELECT CAST(json_extract(watermark_json,'$.spot_ledger_max_id') AS INTEGER) id FROM outbox_checkpoints WHERE checkpoint_name='financial-outbox-v1')
        SELECT count(*) n FROM ledger_entries l CROSS JOIN cutoff c
        LEFT JOIN financial_outbox_events e ON e.source_ledger_entry_id=l.entry_id
        WHERE l.id>coalesce(c.id,0) AND e.event_id IS NULL
    """, "ledger", "cutover 后每条现货资金流水必须同事务产生 durable outbox event"),
    ReconciliationCheck("outbox_contract_post_cutover_missing", "outbox", "blocking", """
        WITH cutoff AS (SELECT CAST(json_extract(watermark_json,'$.contract_ledger_max_id') AS INTEGER) id FROM outbox_checkpoints WHERE checkpoint_name='financial-outbox-v1')
        SELECT count(*) n FROM contract_ledger_entries l CROSS JOIN cutoff c
        LEFT JOIN financial_outbox_events e ON e.source_ledger_entry_id=l.entry_id
        WHERE l.id>coalesce(c.id,0) AND e.event_id IS NULL
    """, "contract_ledger", "cutover 后每条合约资金流水必须同事务产生 durable outbox event"),
    ReconciliationCheck("outbox_insurance_post_cutover_missing", "outbox", "blocking", """
        WITH cutoff AS (SELECT CAST(json_extract(watermark_json,'$.insurance_event_max_id') AS INTEGER) id FROM outbox_checkpoints WHERE checkpoint_name='financial-outbox-v1')
        SELECT count(*) n FROM contract_insurance_events i CROSS JOIN cutoff c
        LEFT JOIN financial_outbox_events e ON e.source_event_id=i.event_id AND e.event_type='insurance_event_committed'
        WHERE i.id>coalesce(c.id,0) AND e.event_id IS NULL
    """, "insurance_event", "cutover 后每条保险基金事件必须同事务产生 durable outbox event"),
    ReconciliationCheck("outbox_trade_post_cutover_missing", "outbox", "blocking", """
        WITH cutoff AS (SELECT CAST(json_extract(watermark_json,'$.trade_max_id') AS INTEGER) id FROM outbox_checkpoints WHERE checkpoint_name='financial-outbox-v1')
        SELECT count(*) n FROM trades t CROSS JOIN cutoff c
        LEFT JOIN financial_outbox_events e ON e.source_event_id=t.trade_id AND e.event_type='trade_committed'
        WHERE t.id>coalesce(c.id,0) AND e.event_id IS NULL
    """, "trade", "cutover 后每笔成交必须同事务产生唯一 durable trade event"),
    ReconciliationCheck("outbox_delivered_without_receipt", "outbox", "blocking", """
        SELECT count(*) n FROM financial_outbox_events e LEFT JOIN outbox_consumer_receipts r ON r.event_id=e.event_id
        WHERE e.status='delivered' AND r.id IS NULL
    """, "outbox_event", "delivered 必须有 durable consumer receipt"),
    ReconciliationCheck("outbox_dead_event", "outbox", "blocking", """
        SELECT count(*) n FROM financial_outbox_events WHERE status='dead'
    """, "outbox_event", "dead event 表示资金事件未完成投递，必须人工处置"),
    ReconciliationCheck("outbox_stale_processing", "outbox", "warning", """
        SELECT count(*) n FROM financial_outbox_events
        WHERE status='processing' AND claimed_at<datetime('now','-60 seconds')
    """, "outbox_event", "过期 lease 应由 dispatcher 自动重新抢占；持续存在需检查 worker"),
    ReconciliationCheck("outbox_retry_history", "outbox", "warning", """
        SELECT count(*) n FROM financial_outbox_events WHERE attempts>1
    """, "outbox_event", "保留重复投递历史；消费者必须按 event_id 幂等"),
    ReconciliationCheck("outbox_overdue_pending", "outbox", "warning", """
        SELECT count(*) n FROM financial_outbox_events
        WHERE status='pending' AND available_at<datetime('now','-30 seconds')
    """, "outbox_event", "资金事件长时间 pending 时检查 dispatcher 和 last_error"),
)


SAMPLE_QUERIES = {
    "spot_negative_balance": """
        SELECT CAST(id AS TEXT) entity_id,user_id,max(abs(available),abs(frozen)) difference,asset
        FROM balances WHERE available<0 OR frozen<0 ORDER BY difference DESC,id LIMIT :limit
    """,
    "spot_chain_gap_customer": f"""
        WITH w AS (SELECT l.*,lag(available_after) OVER(PARTITION BY user_id,asset ORDER BY id) pa,
          lag(frozen_after) OVER(PARTITION BY user_id,asset ORDER BY id) pf FROM ledger_entries l)
        SELECT entry_id entity_id,w.user_id,max(abs(available_before-pa),abs(frozen_before-pf)) difference,asset
        FROM w JOIN users u ON u.id=w.user_id WHERE pa IS NOT NULL
          AND (abs(available_before-pa)>0.00000001 OR abs(frozen_before-pf)>0.00000001)
          AND NOT {ROBOT_PREDICATE} AND NOT {SYSTEM_PREDICATE}
        ORDER BY difference DESC,w.id LIMIT :limit
    """,
    "spot_chain_gap_robot": f"""
        WITH w AS (SELECT l.*,lag(available_after) OVER(PARTITION BY user_id,asset ORDER BY id) pa,
          lag(frozen_after) OVER(PARTITION BY user_id,asset ORDER BY id) pf FROM ledger_entries l)
        SELECT entry_id entity_id,w.user_id,max(abs(available_before-pa),abs(frozen_before-pf)) difference,asset
        FROM w JOIN users u ON u.id=w.user_id WHERE pa IS NOT NULL
          AND (abs(available_before-pa)>0.00000001 OR abs(frozen_before-pf)>0.00000001) AND {ROBOT_PREDICATE}
        ORDER BY difference DESC,w.id LIMIT :limit
    """,
    "spot_redundant_total_precision": f"""
        WITH w AS (SELECT l.*,lag(balance_after) OVER(PARTITION BY user_id,asset ORDER BY id) pb FROM ledger_entries l)
        SELECT entry_id entity_id,w.user_id,abs(balance_before-pb) difference,asset
        FROM w JOIN users u ON u.id=w.user_id WHERE pb IS NOT NULL AND abs(balance_before-pb)>0.00000001
          AND {ROBOT_PREDICATE} ORDER BY difference DESC,w.id LIMIT :limit
    """,
    "spot_latest_snapshot_mismatch": """
        WITH latest AS (SELECT l.*,row_number() OVER(PARTITION BY user_id,asset ORDER BY id DESC) rn FROM ledger_entries l)
        SELECT CAST(b.id AS TEXT) entity_id,b.user_id,
          max(abs(b.available-coalesce(l.available_after,0)),abs(b.frozen-coalesce(l.frozen_after,0))) difference,b.asset
        FROM balances b LEFT JOIN latest l ON l.user_id=b.user_id AND l.asset=b.asset AND l.rn=1
        WHERE (l.user_id IS NULL AND NOT EXISTS(SELECT 1 FROM accounting_transactions WHERE event_type='shadow_opening_balance' AND status='committed'))
          OR abs(b.available-l.available_after)>0.00000001 OR abs(b.frozen-l.frozen_after)>0.00000001
        ORDER BY difference DESC,b.id LIMIT :limit
    """,
    "spot_orphan_ledger_order": """
        SELECT l.entry_id entity_id,l.user_id,1 difference,l.related_order_id source_id,l.asset
        FROM ledger_entries l LEFT JOIN orders o ON o.order_id=l.related_order_id
        WHERE l.related_order_id IS NOT NULL AND o.order_id IS NULL ORDER BY l.id LIMIT :limit
    """,
    "spot_orphan_ledger_trade": """
        SELECT l.entry_id entity_id,l.user_id,1 difference,l.related_trade_id source_id,l.asset
        FROM ledger_entries l LEFT JOIN trades t ON t.trade_id=l.related_trade_id
        WHERE l.related_trade_id IS NOT NULL AND t.trade_id IS NULL ORDER BY l.id LIMIT :limit
    """,
    "trade_missing_order": """
        SELECT t.trade_id entity_id,t.taker_user_id user_id,1 difference,t.taker_order_id,t.maker_order_id,t.product_type
        FROM trades t LEFT JOIN orders a ON a.order_id=t.taker_order_id LEFT JOIN orders b ON b.order_id=t.maker_order_id
        WHERE a.order_id IS NULL OR b.order_id IS NULL ORDER BY t.id LIMIT :limit
    """,
    "spot_legacy_trade_missing_settlement": """
        SELECT t.trade_id entity_id,t.taker_user_id user_id,4-count(l.id) difference,t.maker_user_id
        FROM trades t LEFT JOIN ledger_entries l ON l.related_trade_id=t.trade_id AND l.change_type='trade_settlement'
        WHERE t.product_type='SPOT' AND t.business_key LIKE 'legacy:%' GROUP BY t.trade_id
        HAVING count(l.id)<4 ORDER BY difference DESC,t.id LIMIT :limit
    """,
    "spot_current_trade_missing_settlement": """
        SELECT t.trade_id entity_id,t.taker_user_id user_id,4-count(l.id) difference,t.maker_user_id
        FROM trades t LEFT JOIN ledger_entries l ON l.related_trade_id=t.trade_id AND l.change_type='trade_settlement'
        WHERE t.product_type='SPOT' AND t.business_key NOT LIKE 'legacy:%' GROUP BY t.trade_id
        HAVING count(l.id)<4 ORDER BY difference DESC,t.id LIMIT :limit
    """,
    "contract_chain_gap_customer": f"""
        WITH w AS (SELECT l.*,lag(wallet_after) OVER(PARTITION BY user_id,margin_asset ORDER BY id) pw,
          lag(available_after) OVER(PARTITION BY user_id,margin_asset ORDER BY id) pa,
          lag(used_margin_after) OVER(PARTITION BY user_id,margin_asset ORDER BY id) pu,
          lag(unrealized_pnl_after) OVER(PARTITION BY user_id,margin_asset ORDER BY id) pun,
          lag(realized_pnl_after) OVER(PARTITION BY user_id,margin_asset ORDER BY id) pr,
          lag(total_fees_after) OVER(PARTITION BY user_id,margin_asset ORDER BY id) pf FROM contract_ledger_entries l)
        SELECT entry_id entity_id,w.user_id,max(abs(wallet_before-pw),abs(available_before-pa),abs(used_margin_before-pu),
          abs(unrealized_pnl_before-pun),abs(realized_pnl_before-pr),abs(total_fees_before-pf)) difference,margin_asset asset
        FROM w JOIN users u ON u.id=w.user_id WHERE pw IS NOT NULL AND
          (abs(wallet_before-pw)>0.00000001 OR abs(available_before-pa)>0.00000001 OR abs(used_margin_before-pu)>0.00000001 OR
           abs(unrealized_pnl_before-pun)>0.00000001 OR abs(realized_pnl_before-pr)>0.00000001 OR abs(total_fees_before-pf)>0.00000001)
          AND NOT {ROBOT_PREDICATE} AND NOT {SYSTEM_PREDICATE} ORDER BY difference DESC,w.id LIMIT :limit
    """,
    "contract_chain_gap_robot": f"""
        WITH w AS (SELECT l.*,lag(wallet_after) OVER(PARTITION BY user_id,margin_asset ORDER BY id) pw,
          lag(available_after) OVER(PARTITION BY user_id,margin_asset ORDER BY id) pa,
          lag(used_margin_after) OVER(PARTITION BY user_id,margin_asset ORDER BY id) pu,
          lag(unrealized_pnl_after) OVER(PARTITION BY user_id,margin_asset ORDER BY id) pun,
          lag(realized_pnl_after) OVER(PARTITION BY user_id,margin_asset ORDER BY id) pr,
          lag(total_fees_after) OVER(PARTITION BY user_id,margin_asset ORDER BY id) pf FROM contract_ledger_entries l)
        SELECT entry_id entity_id,w.user_id,max(abs(wallet_before-pw),abs(available_before-pa),abs(used_margin_before-pu),
          abs(unrealized_pnl_before-pun),abs(realized_pnl_before-pr),abs(total_fees_before-pf)) difference,margin_asset asset
        FROM w JOIN users u ON u.id=w.user_id WHERE pw IS NOT NULL AND
          (abs(wallet_before-pw)>0.00000001 OR abs(available_before-pa)>0.00000001 OR abs(used_margin_before-pu)>0.00000001 OR
           abs(unrealized_pnl_before-pun)>0.00000001 OR abs(realized_pnl_before-pr)>0.00000001 OR abs(total_fees_before-pf)>0.00000001)
          AND {ROBOT_PREDICATE} ORDER BY difference DESC,w.id LIMIT :limit
    """,
    "contract_legacy_trade_missing_ledger": """
        SELECT t.trade_id entity_id,t.taker_user_id user_id,2-count(l.id) difference,t.maker_user_id
        FROM trades t LEFT JOIN contract_ledger_entries l ON l.related_trade_id=t.trade_id
        WHERE t.product_type='PERP' AND t.business_key LIKE 'legacy:%' GROUP BY t.trade_id
        HAVING count(l.id)<2 ORDER BY difference DESC,t.id LIMIT :limit
    """,
    "contract_current_trade_missing_ledger": """
        SELECT t.trade_id entity_id,t.taker_user_id user_id,2-count(l.id) difference,t.maker_user_id
        FROM trades t LEFT JOIN contract_ledger_entries l ON l.related_trade_id=t.trade_id
        WHERE t.product_type='PERP' AND t.business_key NOT LIKE 'legacy:%' GROUP BY t.trade_id
        HAVING count(l.id)<2 ORDER BY difference DESC,t.id LIMIT :limit
    """,
    "accounting_voided_legacy_noop": """
        SELECT transaction_id entity_id,NULL user_id,
          abs(CAST(json_extract(metadata_json,'$.original_amount') AS REAL)) difference,
          source_ledger_entry_id source_id,json_extract(metadata_json,'$.domain') domain
        FROM accounting_transactions WHERE event_type='voided_legacy_noop'
        ORDER BY difference DESC,transaction_id LIMIT :limit
    """,
    "accounting_spot_source_delta_mismatch": """
        WITH q AS (
          SELECT t.transaction_id,t.source_ledger_entry_id,
            sum(CASE WHEN e.account_code='customer_spot_available' THEN e.quantity ELSE 0 END) available_delta,
            sum(CASE WHEN e.account_code='customer_spot_frozen' THEN e.quantity ELSE 0 END) frozen_delta
          FROM accounting_transactions t JOIN accounting_entries e ON e.transaction_id=t.transaction_id
          WHERE t.source_ledger_entry_id IS NOT NULL AND t.event_type!='voided_legacy_noop'
          GROUP BY t.transaction_id,t.source_ledger_entry_id
        )
        SELECT q.transaction_id entity_id,l.user_id,
          max(abs((l.available_after-l.available_before)-q.available_delta),abs((l.frozen_after-l.frozen_before)-q.frozen_delta)) difference,
          l.entry_id source_id,l.asset FROM ledger_entries l JOIN q ON q.source_ledger_entry_id=l.entry_id
        LEFT JOIN accounting_transactions r ON r.reversal_of_transaction_id=q.transaction_id
        WHERE r.transaction_id IS NULL AND
          (abs((l.available_after-l.available_before)-q.available_delta)>0.00000001 OR
           abs((l.frozen_after-l.frozen_before)-q.frozen_delta)>0.00000001)
        ORDER BY difference DESC,l.id LIMIT :limit
    """,
    "accounting_reversed_noop_shadow": """
        SELECT transaction_id entity_id,NULL user_id,1 difference,reversal_of_transaction_id source_id,
          json_extract(metadata_json,'$.source_ledger_entry_id') ledger_id
        FROM accounting_transactions WHERE event_type='reversal'
          AND json_extract(metadata_json,'$.classification')='noop_shadow_mismatch'
        ORDER BY transaction_id LIMIT :limit
    """,
    "outbox_dead_event": """
        SELECT event_id entity_id,user_id,attempts difference,event_type,aggregate_id,last_error
        FROM financial_outbox_events WHERE status='dead' ORDER BY attempts DESC,id LIMIT :limit
    """,
    "outbox_stale_processing": """
        SELECT event_id entity_id,user_id,attempts difference,event_type,aggregate_id,claimed_by
        FROM financial_outbox_events WHERE status='processing' AND claimed_at<datetime('now','-60 seconds')
        ORDER BY claimed_at,id LIMIT :limit
    """,
    "outbox_retry_history": """
        SELECT event_id entity_id,user_id,attempts difference,event_type,aggregate_id,last_error
        FROM financial_outbox_events WHERE attempts>1 ORDER BY attempts DESC,id LIMIT :limit
    """,
    "outbox_overdue_pending": """
        SELECT event_id entity_id,user_id,attempts difference,event_type,aggregate_id,last_error
        FROM financial_outbox_events WHERE status='pending' AND available_at<datetime('now','-30 seconds')
        ORDER BY available_at,id LIMIT :limit
    """,
}


class ReconciliationService:
    async def run(
        self,
        session: AsyncSession,
        *,
        scope: str = "full",
        sample_limit: int = 20,
        commit: bool = True,
    ) -> dict:
        started = datetime.now(tz=UTC)
        timer = perf_counter()
        run_id = f"recon_{uuid4().hex}"
        watermark = await self._watermark(session)
        results = []
        differences = []
        blocking_count = 0
        difference_count = 0
        run_max_difference = Decimal("0")
        for check in CHECKS:
            if scope != "full" and check.domain != scope:
                continue
            count = int((await session.execute(text(check.sql))).scalar_one() or 0)
            status = "passed" if count == 0 else ("failed" if check.severity == "blocking" else "warning")
            difference_count += count
            if count and check.severity == "blocking":
                blocking_count += count
            samples = []
            max_difference = None
            if count and check.code in SAMPLE_QUERIES:
                sample_rows = (await session.execute(
                    text(SAMPLE_QUERIES[check.code]), {"limit": max(1, min(sample_limit, 100))}
                )).mappings().all()
                for row in sample_rows:
                    sample = {}
                    for key, value in row.items():
                        sample[key] = str(value) if isinstance(value, Decimal) else value
                    samples.append(sample)
                numeric_differences = [Decimal(str(item["difference"])) for item in samples if item.get("difference") is not None]
                if numeric_differences:
                    maximum = max(numeric_differences)
                    max_difference = str(maximum)
                    run_max_difference = max(run_max_difference, maximum)
            result = {
                "code": check.code,
                "domain": check.domain,
                "severity": check.severity,
                "status": status,
                "difference_count": count,
                "max_difference": max_difference,
                "samples": samples,
                "recommendation": check.recommendation,
            }
            results.append(result)
            if count:
                diff = ReconciliationDifference(
                    difference_id=f"rd_{uuid4().hex}", run_id=run_id, domain=check.domain,
                    check_code=check.code, severity=check.severity, entity_type=check.entity_type,
                    entity_id=str(samples[0].get("entity_id")) if samples else None,
                    user_id=int(samples[0]["user_id"]) if samples and samples[0].get("user_id") is not None else None,
                    detail_json={
                        "difference_count": count, "max_difference": max_difference,
                        "samples": samples, "recommendation": check.recommendation,
                        "sample_limit": sample_limit,
                    },
                    created_at=started,
                )
                session.add(diff)
                differences.append(diff)
        if scope in {"full", "outbox"}:
            replay_verification = await verify_financial_outbox_replay_requests(
                session, sample_limit=sample_limit,
            )
            replay_count = int(replay_verification["invalid_count"])
            replay_status = "passed" if replay_count == 0 else "failed"
            difference_count += replay_count
            blocking_count += replay_count
            results.append({
                "code": "outbox_replay_request_invalid",
                "domain": "outbox",
                "severity": "blocking",
                "status": replay_status,
                "difference_count": replay_count,
                "max_difference": None,
                "samples": replay_verification["issues"],
                "recommendation": "隔离被篡改或缺少安全对账水位的重放请求，禁止继续投递",
            })
            if replay_count:
                session.add(ReconciliationDifference(
                    difference_id=f"rd_{uuid4().hex}",
                    run_id=run_id,
                    domain="outbox",
                    check_code="outbox_replay_request_invalid",
                    severity="blocking",
                    entity_type="outbox_replay",
                    entity_id=str(replay_verification["issues"][0].get("entity_id")),
                    user_id=None,
                    detail_json={
                        "difference_count": replay_count,
                        "samples": replay_verification["issues"],
                        "recommendation": "隔离被篡改或缺少安全对账水位的重放请求，禁止继续投递",
                        "sample_limit": sample_limit,
                    },
                    created_at=started,
                ))
        if scope in {"full", "accounting"}:
            proof = await verify_accounting_proof_chain(session, sample_limit=sample_limit)
            dynamic_checks = (
                (
                    "accounting_proof_checkpoint_missing",
                    "warning",
                    1 if proof["checkpoint_count"] == 0 else 0,
                    [],
                    "尚无可复验账务证明检查点；不影响当前交易，但 Gate E 长期门禁不能成立",
                ),
                (
                    "accounting_proof_chain_invalid",
                    "blocking",
                    int(proof["invalid_count"]),
                    proof["issues"],
                    "检查点载荷、摘要或前序哈希链无效，必须隔离证据并调查，禁止静默重写",
                ),
            )
            for code, severity, count, samples, recommendation in dynamic_checks:
                status = "passed" if count == 0 else ("failed" if severity == "blocking" else "warning")
                difference_count += count
                if count and severity == "blocking":
                    blocking_count += count
                results.append({
                    "code": code,
                    "domain": "accounting",
                    "severity": severity,
                    "status": status,
                    "difference_count": count,
                    "max_difference": None,
                    "samples": samples,
                    "recommendation": recommendation,
                })
                if count:
                    session.add(ReconciliationDifference(
                        difference_id=f"rd_{uuid4().hex}", run_id=run_id, domain="accounting",
                        check_code=code, severity=severity, entity_type="accounting_proof",
                        entity_id=str(samples[0].get("entity_id")) if samples else None,
                        user_id=None,
                        detail_json={
                            "difference_count": count,
                            "samples": samples,
                            "recommendation": recommendation,
                            "sample_limit": sample_limit,
                        },
                        created_at=started,
                    ))
        if scope in {"full", "robot"}:
            robot_checkpoint = await verify_robot_financial_checkpoint_chain(session, sample_limit=sample_limit)
            dynamic_checks = (
                (
                    "robot_financial_checkpoint_missing",
                    "warning",
                    1 if robot_checkpoint["checkpoint_count"] == 0 else 0,
                    [],
                    "尚无机器人金融 checkpoint；金融裁剪继续 fail-closed，不影响普通客户完整记录",
                ),
                (
                    "robot_financial_checkpoint_invalid",
                    "blocking",
                    int(robot_checkpoint["invalid_count"]),
                    robot_checkpoint["issues"],
                    "机器人 checkpoint 载荷、期初期末或哈希链无效，禁止裁剪并隔离调查",
                ),
                (
                    "robot_financial_pruning_configured",
                    "warning",
                    1 if settings.history_retention_financial_pruning_enabled else 0,
                    [],
                    "金融裁剪配置虽被打开但执行层会明确拒绝；应恢复 false，等待独立删除迁移",
                ),
            )
            for code, severity, count, samples, recommendation in dynamic_checks:
                status = "passed" if count == 0 else ("failed" if severity == "blocking" else "warning")
                difference_count += count
                if count and severity == "blocking":
                    blocking_count += count
                results.append({
                    "code": code,
                    "domain": "robot",
                    "severity": severity,
                    "status": status,
                    "difference_count": count,
                    "max_difference": None,
                    "samples": samples,
                    "recommendation": recommendation,
                })
                if count:
                    session.add(ReconciliationDifference(
                        difference_id=f"rd_{uuid4().hex}", run_id=run_id, domain="robot",
                        check_code=code, severity=severity, entity_type="robot_financial_checkpoint",
                        entity_id=str(samples[0].get("entity_id")) if samples else None,
                        user_id=None,
                        detail_json={
                            "difference_count": count,
                            "samples": samples,
                            "recommendation": recommendation,
                            "sample_limit": sample_limit,
                        },
                        created_at=started,
                    ))
        completed = datetime.now(tz=UTC)
        status = "failed" if blocking_count else ("warning" if difference_count else "passed")
        summary = {
            "run_id": run_id,
            "scope": scope,
            "status": status,
            "started_at": started.isoformat(),
            "completed_at": completed.isoformat(),
            "elapsed_ms": round((perf_counter() - timer) * 1000, 2),
            "watermark": watermark,
            "difference_count": difference_count,
            "max_difference": str(run_max_difference),
            "blocking_count": blocking_count,
            "allow_trading": blocking_count == 0,
            "checks": results,
            "automatic_repair": False,
        }
        session.add(ReconciliationRun(
            run_id=run_id, scope=scope, status=status, started_at=started, completed_at=completed,
            watermark_json=watermark, summary_json=summary, difference_count=difference_count,
            blocking_count=blocking_count, allow_trading="yes" if blocking_count == 0 else "no",
        ))
        if commit:
            await session.commit()
        else:
            await session.flush()
        return summary

    async def latest(self, session: AsyncSession) -> dict | None:
        row = await session.scalar(select(ReconciliationRun).order_by(ReconciliationRun.started_at.desc()).limit(1))
        return None if row is None else row.summary_json

    async def differences(
        self,
        session: AsyncSession,
        run_id: str,
        *,
        domain: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        stmt = select(ReconciliationDifference).where(ReconciliationDifference.run_id == run_id)
        count_stmt = select(func.count()).select_from(ReconciliationDifference).where(ReconciliationDifference.run_id == run_id)
        if domain:
            stmt = stmt.where(ReconciliationDifference.domain == domain)
            count_stmt = count_stmt.where(ReconciliationDifference.domain == domain)
        bounded_limit = max(1, min(limit, 500))
        bounded_offset = max(0, offset)
        total = int(await session.scalar(count_stmt) or 0)
        rows = (await session.execute(
            stmt.order_by(ReconciliationDifference.id.asc()).offset(bounded_offset).limit(bounded_limit)
        )).scalars()
        items = [{
            "difference_id": row.difference_id, "run_id": row.run_id, "domain": row.domain,
            "check_code": row.check_code, "severity": row.severity, "entity_type": row.entity_type,
            "entity_id": row.entity_id, "user_id": row.user_id, "detail": row.detail_json,
            "created_at": row.created_at.isoformat(),
        } for row in rows]
        return {"items": items, "total": total, "limit": bounded_limit, "offset": bounded_offset}

    async def _watermark(self, session: AsyncSession) -> dict:
        tables = ("orders", "trades", "ledger_entries", "contract_ledger_entries", "contract_funding_events", "contract_funding_settlements", "contract_liquidation_events", "contract_insurance_events", "contract_adl_events", "accounting_entries", "financial_outbox_events", "outbox_consumer_receipts")
        result = {}
        for table in tables:
            if table in {"accounting_entries", "outbox_consumer_receipts"}:
                result[table] = int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one() or 0)
            else:
                result[table] = int((await session.execute(text(f"SELECT coalesce(max(id),0) FROM {table}"))).scalar_one() or 0)
        return result
