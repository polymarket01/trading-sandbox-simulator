import { useState } from "react";
import { bjDateTime, bjTime, fmt, sideColor } from "../lib/format";
import type { LedgerItem, OrderItem, TradeItem } from "../types";

type Tab = "open" | "history" | "trades" | "ledger";
type OpenFilter = "all" | "buy" | "sell";
type OpenSort = "time_desc" | "time_asc" | "side";

export function OrdersTabs({
  openOrders,
  orderHistory,
  accountTrades,
  ledger,
  priceDigits,
  quantityDigits,
  onCancel,
}: {
  openOrders: OrderItem[];
  orderHistory: OrderItem[];
  accountTrades: TradeItem[];
  ledger: LedgerItem[];
  priceDigits: number;
  quantityDigits: number;
  onCancel: (orderId: string) => Promise<void>;
}) {
  const [tab, setTab] = useState<Tab>("open");
  const [openFilter, setOpenFilter] = useState<OpenFilter>("all");
  const [openSort, setOpenSort] = useState<OpenSort>("time_desc");
  const tabs: [Tab, string][] = [
    ["open", "当前订单"],
    ["history", "历史订单"],
    ["trades", "成交记录"],
    ["ledger", "账本流水"],
  ];
  const filteredOpenOrders = [...openOrders]
    .filter((item) => openFilter === "all" || item.side === openFilter)
    .sort((a, b) => {
      if (openSort === "time_asc") return a.created_at - b.created_at;
      if (openSort === "side") return a.side.localeCompare(b.side) || b.created_at - a.created_at;
      return b.created_at - a.created_at;
    });

  return (
    <section className="panel flex h-[620px] min-h-0 flex-col rounded-3xl p-4">
      <div className="mb-4 flex flex-wrap gap-2">
        {tabs.map(([value, label]) => (
          <button
            key={value}
            onClick={() => setTab(value)}
            className={`rounded-full border px-4 py-2 text-sm transition ${
              tab === value
                ? "border-cyan-300/30 bg-cyan-400/16 text-cyan-100"
                : "border-transparent bg-white/5 text-slate-300 hover:bg-white/8"
            }`}
          >
            {label}
          </button>
        ))}
      </div>
      {tab === "open" && (
        <div className="mb-4 flex flex-wrap items-center gap-2">
          {([
            ["all", "全部"],
            ["buy", "只看买单"],
            ["sell", "只看卖单"],
          ] as const).map(([value, label]) => (
            <button
              key={value}
              onClick={() => setOpenFilter(value)}
              className={`rounded-full px-3 py-1.5 text-xs transition ${
                openFilter === value ? "bg-cyan-400/16 text-cyan-100" : "bg-white/5 text-slate-300 hover:bg-white/8"
              }`}
            >
              {label}
            </button>
          ))}
          <div className="ml-auto flex items-center gap-2 text-xs text-slate-400">
            <span>排序</span>
            <select
              value={openSort}
              onChange={(event) => setOpenSort(event.target.value as OpenSort)}
              className="rounded-full border border-white/10 bg-white/5 px-3 py-1.5 text-slate-200 outline-none"
            >
              <option value="time_desc">时间倒序</option>
              <option value="time_asc">时间正序</option>
              <option value="side">方向优先</option>
            </select>
          </div>
        </div>
      )}
      <div className="scrollbar flex-1 overflow-auto">
        {tab === "open" && (
          <table className="min-w-full text-left text-sm">
            <thead className="text-slate-500">
              <tr>
                <th className="pb-2">时间</th><th className="pb-2">方向</th><th className="pb-2">价格</th><th className="pb-2">数量</th><th className="pb-2">状态</th><th className="pb-2"></th>
              </tr>
            </thead>
            <tbody>
              {filteredOpenOrders.slice(0, 500).map((item) => (
                <tr key={item.order_id} className="border-t border-white/5">
                  <td className="py-3 text-slate-400">{bjDateTime(item.created_at)}</td>
                  <td className={`py-3 ${sideColor(item.side)}`}>{item.side}</td>
                  <td className="py-3">{fmt(item.price, priceDigits)}</td>
                  <td className="py-3">{fmt(item.quantity, quantityDigits)}</td>
                  <td className="py-3">{item.status}</td>
                  <td className="py-3 text-right">
                    <button onClick={() => onCancel(item.order_id)} className="rounded-lg bg-white/6 px-3 py-1 text-xs text-slate-200">撤单</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {tab === "history" && (
          <div className="space-y-2">
            {orderHistory.map((item) => (
              <div key={item.order_id} className="grid gap-2 rounded-2xl border border-white/6 bg-white/5 px-4 py-3 md:grid-cols-[1.2fr_0.8fr_0.8fr_0.8fr_0.8fr]">
                <div className="text-slate-300">{bjDateTime(item.created_at)}</div>
                <div className={sideColor(item.side)}>{item.side} / {item.type}</div>
                <div>{fmt(item.price, priceDigits)}</div>
                <div>{fmt(item.filled_quantity, quantityDigits)} / {fmt(item.quantity, quantityDigits)}</div>
                <div>
                  <span className="rounded-full bg-white/6 px-2 py-1 text-xs text-slate-300">{item.status}</span>
                </div>
                {item.reject_reason && (
                  <div className="md:col-span-5 rounded-xl bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
                    拒单原因：{item.reject_reason}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
        {tab === "trades" && (
          <div className="space-y-2">
            {accountTrades.map((item) => (
              <div key={item.trade_id} className="grid gap-2 rounded-2xl bg-white/5 px-4 py-3 md:grid-cols-[1fr_0.7fr_0.8fr_0.8fr_1fr]">
                <div className="text-slate-400">{bjDateTime(item.ts ?? Date.now())}</div>
                <div className={sideColor(item.side)}>{item.side}</div>
                <div>{fmt(item.price, priceDigits)}</div>
                <div>{fmt(item.quantity, quantityDigits)}</div>
                <div className="text-slate-400">{item.liquidity_role} / fee {fmt(item.fee, 6)} {item.fee_asset}</div>
              </div>
            ))}
          </div>
        )}
        {tab === "ledger" && (
          <div className="space-y-2">
            {ledger.map((item) => (
              <div key={item.entry_id} className="grid gap-2 rounded-2xl bg-white/5 px-4 py-3 md:grid-cols-[1.1fr_0.8fr_0.7fr_1.2fr_1.2fr]">
                <div className="text-slate-400">{bjDateTime(item.created_at)}</div>
                <div>{item.asset}</div>
                <div>{item.change_type}</div>
                <div>{fmt(item.amount, 8)}</div>
                <div className="text-slate-500">A {fmt(item.available_before, 4)}→{fmt(item.available_after, 4)} / F {fmt(item.frozen_before, 4)}→{fmt(item.frozen_after, 4)}</div>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
