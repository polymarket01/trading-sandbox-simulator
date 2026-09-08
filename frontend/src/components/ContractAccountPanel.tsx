import { fmt } from "../lib/format";
import type { ContractAccount, ContractFundingEvent, ContractLedgerItem, ContractLiquidationEvent, ContractPosition, ContractPriceState, ContractSetting, MarketTicker } from "../types";

function pnlClass(value?: string | number | null) {
  const num = Number(value ?? 0);
  if (num > 0) return "text-emerald-300";
  if (num < 0) return "text-rose-300";
  return "text-slate-100";
}

export function ContractAccountPanel({
  account,
  positions,
  ticker,
  setting,
  priceState,
  priceDigits,
  quantityDigits,
  username,
  fundingEvents = [],
  ledgerEntries = [],
  liquidationEvents = [],
}: {
  account?: ContractAccount;
  positions: ContractPosition[];
  ticker?: MarketTicker;
  setting?: ContractSetting;
  priceState?: ContractPriceState;
  priceDigits: number;
  quantityDigits: number;
  username?: string;
  fundingEvents?: ContractFundingEvent[];
  ledgerEntries?: ContractLedgerItem[];
  liquidationEvents?: ContractLiquidationEvent[];
}) {
  const activePositions = positions.filter((item) => Number(item.quantity) > 0 && item.side !== "flat");
  const nextFunding = priceState?.next_funding_time ? new Date(priceState.next_funding_time).toLocaleString("zh-CN", { hour12: false }) : "-";
  return (
    <section className="panel rounded-2xl p-4">
      <div className="mb-4 flex items-center justify-between gap-3">
        <div>
          <h3 className="font-display text-lg">合约保证金账户</h3>
          <div className="mt-1 text-xs text-slate-500">{username ?? "-"} · {account?.margin_asset ?? "USDT"} · 独立于现货钱包</div>
        </div>
        <span className="rounded-lg bg-white/8 px-2.5 py-1 text-xs text-slate-300">{setting?.leverage ?? "-"}x</span>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <AccountMetric label="保证金钱包" value={`${fmt(account?.wallet_balance, 4)} ${account?.margin_asset ?? "USDT"}`} />
        <AccountMetric label="可用保证金" value={`${fmt(account?.available_margin, 4)} ${account?.margin_asset ?? "USDT"}`} />
        <AccountMetric label="占用保证金" value={`${fmt(account?.used_margin, 4)} ${account?.margin_asset ?? "USDT"}`} />
        <AccountMetric label="手续费" value={`${fmt(account?.total_fees, 6)} ${account?.margin_asset ?? "USDT"}`} />
        <AccountMetric label="未实现盈亏" value={fmt(account?.unrealized_pnl, 4)} valueClass={pnlClass(account?.unrealized_pnl)} />
        <AccountMetric label="已实现盈亏" value={fmt(account?.realized_pnl, 4)} valueClass={pnlClass(account?.realized_pnl)} />
      </div>
      <div className="mt-4 rounded-2xl border border-white/8 bg-slate-950/25 p-3">
        <div className="mb-3 flex items-center justify-between gap-2">
          <h4 className="font-display text-sm">保证金流水</h4>
          <span className="text-xs text-slate-500">{ledgerEntries.length} 条</span>
        </div>
        <div className="space-y-2">
          {ledgerEntries.slice(0, 6).map((item) => {
            const amount = Number(item.amount);
            const usedDelta = Number(item.used_margin_after) - Number(item.used_margin_before);
            const amountClass = amount > 0 ? "text-emerald-300" : amount < 0 ? "text-rose-300" : usedDelta !== 0 ? "text-cyan-200" : "text-slate-100";
            const time = item.created_at ? new Date(item.created_at).toLocaleString("zh-CN", { hour12: false }) : "-";
            return (
              <div key={item.entry_id} className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 rounded-xl bg-white/5 px-3 py-2 text-xs">
                <div className="min-w-0">
                  <div className="truncate text-slate-300">{item.symbol ?? "保证金"} · {item.change_type}</div>
                  <div className="mt-1 truncate text-slate-500">{time} · 保证金钱包 {fmt(item.wallet_after, 4)} · 占用 {fmt(item.used_margin_after, 4)}</div>
                </div>
                <div className={`self-center font-mono tabular-nums ${amountClass}`}>{amount !== 0 ? fmt(item.amount, 6) : fmt(usedDelta, 6)}</div>
              </div>
            );
          })}
          {ledgerEntries.length === 0 && (
            <div className="rounded-xl bg-slate-950/35 px-3 py-5 text-center text-sm text-slate-500">
              暂无合约保证金流水。
            </div>
          )}
        </div>
      </div>
      <div className="mt-4 rounded-2xl border border-white/8 bg-slate-950/25 p-3">
        <div className="mb-3 flex items-center justify-between gap-2">
          <h4 className="font-display text-sm">持仓</h4>
          <span className="text-xs text-slate-500">标记价 {fmt(priceState?.mark_price ?? ticker?.mid_price ?? ticker?.last_price, priceDigits)}</span>
        </div>
        <div className="mb-3 grid grid-cols-3 gap-2">
          <AccountMetric label="指数价" value={fmt(priceState?.index_price, priceDigits)} />
          <AccountMetric label="资金费率" value={`${fmt(Number(priceState?.funding_rate ?? 0) * 100, 4)}%`} />
          <AccountMetric label="模式" value={priceState?.funding_rate_mode ?? "-"} />
          <AccountMetric label="下次资金费" value={nextFunding} />
        </div>
        <div className="space-y-2">
          {activePositions.map((item) => (
            <div key={`${item.symbol}-${item.side}`} className="rounded-xl bg-white/5 px-3 py-3">
              <div className="mb-2 flex items-center justify-between gap-2">
                <div className="font-mono text-sm text-white">{item.symbol}</div>
                <span className={`rounded-full px-2 py-0.5 text-xs ${item.side === "long" ? "bg-emerald-400/14 text-emerald-100" : "bg-rose-500/14 text-rose-100"}`}>
                  {item.side}
                </span>
              </div>
              <div className="grid grid-cols-2 gap-2 text-xs">
                <PositionMetric label="数量" value={fmt(item.quantity, quantityDigits)} />
                <PositionMetric label="杠杆" value={`${fmt(item.leverage, 2)}x`} />
                <PositionMetric label="开仓均价" value={fmt(item.entry_price, priceDigits)} />
                <PositionMetric label="标记价格" value={fmt(item.mark_price, priceDigits)} />
                <PositionMetric label="保证金" value={fmt(item.isolated_margin, 4)} />
                <PositionMetric label="维持保证金" value={fmt(item.maintenance_margin, 4)} />
                <PositionMetric label="风险阶梯" value={`T${item.risk_tier ?? 1} / ${fmt(item.risk_max_leverage ?? item.leverage, 2)}x`} />
                <PositionMetric label="维持率" value={`${fmt(Number(item.maintenance_margin_rate ?? 0) * 100, 4)}%`} />
                <PositionMetric label="维持扣减" value={fmt(item.maintenance_amount ?? 0, 4)} />
                <PositionMetric label="强平价格" value={fmt(item.liquidation_price, priceDigits)} />
                <PositionMetric label="未实现盈亏" value={fmt(item.unrealized_pnl, 4)} valueClass={pnlClass(item.unrealized_pnl)} />
              </div>
            </div>
          ))}
          {activePositions.length === 0 && (
            <div className="rounded-xl bg-slate-950/35 px-3 py-6 text-center text-sm text-slate-500">
              当前没有合约持仓。
            </div>
          )}
        </div>
      </div>
      <div className="mt-4 rounded-2xl border border-white/8 bg-slate-950/25 p-3">
        <div className="mb-3 flex items-center justify-between gap-2">
          <h4 className="font-display text-sm">资金费流水</h4>
          <span className="text-xs text-slate-500">{fundingEvents.length} 条</span>
        </div>
        <div className="space-y-2">
          {fundingEvents.slice(0, 5).map((item) => {
            const amount = Number(item.amount);
            const amountClass = amount > 0 ? "text-emerald-300" : amount < 0 ? "text-rose-300" : "text-slate-100";
            const time = item.funding_time ? new Date(item.funding_time).toLocaleString("zh-CN", { hour12: false }) : "-";
            return (
              <div key={item.event_id} className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 rounded-xl bg-white/5 px-3 py-2 text-xs">
                <div className="min-w-0">
                  <div className="truncate text-slate-300">{item.symbol} · {item.position_side} · {item.funding_rate_mode}</div>
                  <div className="mt-1 truncate text-slate-500">{time} · 费率 {fmt(Number(item.funding_rate) * 100, 4)}%</div>
                </div>
                <div className={`self-center font-mono tabular-nums ${amountClass}`}>{fmt(item.amount, 6)}</div>
              </div>
            );
          })}
          {fundingEvents.length === 0 && (
            <div className="rounded-xl bg-slate-950/35 px-3 py-5 text-center text-sm text-slate-500">
              暂无资金费结算记录。
            </div>
          )}
        </div>
      </div>
      <div className="mt-4 rounded-2xl border border-white/8 bg-slate-950/25 p-3">
        <div className="mb-3 flex items-center justify-between gap-2">
          <h4 className="font-display text-sm">强平记录</h4>
          <span className="text-xs text-slate-500">{liquidationEvents.length} 条</span>
        </div>
        <div className="space-y-2">
          {liquidationEvents.slice(0, 5).map((item) => {
            const realized = Number(item.realized_pnl);
            const pnlClassName = realized > 0 ? "text-emerald-300" : realized < 0 ? "text-rose-300" : "text-slate-100";
            const time = item.liquidated_at ? new Date(item.liquidated_at).toLocaleString("zh-CN", { hour12: false }) : "-";
            return (
              <div key={item.event_id} className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 rounded-xl bg-white/5 px-3 py-2 text-xs">
                <div className="min-w-0">
                  <div className="truncate text-slate-300">{item.symbol} · {item.position_side} · {item.reason}</div>
                  <div className="mt-1 truncate text-slate-500">{time} · 标记 {fmt(item.mark_price, priceDigits)} · 强平 {fmt(item.liquidation_price, priceDigits)}</div>
                </div>
                <div className={`self-center font-mono tabular-nums ${pnlClassName}`}>{fmt(item.realized_pnl, 6)}</div>
              </div>
            );
          })}
          {liquidationEvents.length === 0 && (
            <div className="rounded-xl bg-slate-950/35 px-3 py-5 text-center text-sm text-slate-500">
              暂无强平记录。
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function AccountMetric({ label, value, valueClass = "text-slate-100" }: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="min-w-0 rounded-xl bg-white/5 px-3 py-2">
      <div className="truncate text-xs text-slate-500">{label}</div>
      <div className={`mt-1 font-mono text-sm tabular-nums ${valueClass}`} title={value}><span className="num-fixed num-money">{value}</span></div>
    </div>
  );
}

function PositionMetric({ label, value, valueClass = "text-slate-100" }: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="min-w-0">
      <div className="truncate text-slate-500">{label}</div>
      <div className={`mt-1 font-mono tabular-nums ${valueClass}`} title={value}><span className="num-fixed num-money">{value}</span></div>
    </div>
  );
}
