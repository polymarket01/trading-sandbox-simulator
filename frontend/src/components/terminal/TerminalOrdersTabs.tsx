import { useState } from "react";
import { bjTime, fmt, sideColor } from "../../lib/format";
import { isOpenOrder, orderStatusLabel, orderStatusTone, tifLabel, typeLabel } from "../../lib/paper";
import type { OrderItem, TradeItem } from "../../types";
import { EmptyState, SkeletonRows, StatusPill } from "../ui";
import type { TerminalPosition } from "./types";

type Tab = "positions" | "open" | "history" | "fills";

export function TerminalOrdersTabs({
  positions = [],
  openOrders,
  historyOrders,
  fills,
  priceDigits,
  quantityDigits,
  loading,
  cancelingIds,
  closingIds,
  onCancel,
  onAmend,
  onClose,
  onViewAll,
}: {
  positions?: TerminalPosition[];
  openOrders: OrderItem[];
  historyOrders: OrderItem[];
  fills: TradeItem[];
  priceDigits: number;
  quantityDigits: number;
  loading?: boolean;
  cancelingIds?: Set<string>;
  closingIds?: Set<string>;
  onCancel: (order: OrderItem) => void;
  onAmend?: (order: OrderItem) => void;
  onClose?: (position: TerminalPosition) => void;
  onViewAll?: () => void;
}) {
  const [tab, setTab] = useState<Tab>("positions");

  if (loading) {
    return (
      <section className="panel hl-bottom-tabs rounded-2xl p-3">
        <SkeletonRows rows={3} />
      </section>
    );
  }

  const tabs: Array<{ key: Tab; label: string; count: number }> = [
    { key: "positions", label: "Positions", count: positions.length },
    { key: "open", label: "Open Orders", count: openOrders.length },
    { key: "history", label: "Order History", count: historyOrders.length },
    { key: "fills", label: "Trade History", count: fills.length },
  ];

  return (
    <section className="panel hl-bottom-tabs flex min-h-0 min-w-0 flex-col rounded-2xl p-2">
      <div className="hl-tab-row shrink-0">
        {tabs.map((item) => (
          <button key={item.key} type="button" data-active={tab === item.key} onClick={() => setTab(item.key)}>
            {item.label} <span className="font-mono text-[10px] text-slate-500">{item.count}</span>
          </button>
        ))}
        {onViewAll ? (
          <button type="button" onClick={onViewAll} className="ml-auto !border-0 !text-slate-400">View all</button>
        ) : null}
      </div>

      <div className="hl-tab-content scrollbar">
        {tab === "positions" ? (
          positions.length === 0 ? (
            <EmptyState compact title="No open positions" detail="Positions will appear here after a real contract fill." />
          ) : (
            <table className="w-full min-w-[720px] text-left text-[11px]">
              <thead className="text-[9px] uppercase tracking-[0.12em] text-slate-600">
                <tr>
                  <th>Market</th><th>Size</th><th className="text-right">Entry Price</th><th className="text-right">Mark Price</th><th className="text-right">PNL (ROE %)</th><th className="text-right">Liq. Price</th><th className="text-right">Margin</th><th className="text-right">Action</th>
                </tr>
              </thead>
              <tbody className="font-mono tabular-nums">
                {positions.map((position) => {
                  const margin = Number(position.isolated_margin || 0);
                  const pnl = Number(position.unrealized_pnl || 0);
                  const roe = margin > 0 ? (pnl / margin) * 100 : 0;
                  const closing = closingIds?.has(position.symbol);
                  return (
                    <tr key={position.symbol} className="border-t border-white/5">
                      <td className="py-1.5 text-slate-200">{position.symbol} <span className={position.side === "long" ? "text-emerald-300" : "text-rose-300"}>{position.side}</span></td>
                      <td className="py-1.5 text-slate-300">{fmt(position.quantity, quantityDigits)} · {position.leverage}x</td>
                      <td className="py-1.5 text-right text-slate-300">{fmt(position.entry_price, priceDigits)}</td>
                      <td className="py-1.5 text-right text-slate-300">{fmt(position.mark_price, priceDigits)}</td>
                      <td className={`py-1.5 text-right ${sideColor(pnl >= 0 ? "buy" : "sell")}`}>{fmt(pnl, 2)} <span className="text-[10px]">({roe.toFixed(2)}%)</span></td>
                      <td className="py-1.5 text-right text-amber-200/90">{fmt(position.liquidation_price, priceDigits)}</td>
                      <td className="py-1.5 text-right text-slate-300">{fmt(position.isolated_margin, 2)}</td>
                      <td className="py-1.5 text-right">{onClose ? <button type="button" disabled={closing} onClick={() => onClose(position)} className="rounded bg-amber-400/12 px-2 py-0.5 text-[10px] text-amber-100 disabled:opacity-50">{closing ? "Closing" : "Close"}</button> : "-"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )
        ) : null}

        {tab === "open" ? (
          openOrders.length === 0 ? (
            <EmptyState compact title="No open orders" detail="Unfilled and partially filled orders appear here." />
          ) : (
            <table className="w-full min-w-[720px] text-left text-[11px]">
              <thead className="text-[9px] uppercase tracking-[0.12em] text-slate-600"><tr><th>Time</th><th>Market</th><th>Side</th><th>Type</th><th className="text-right">Price</th><th className="text-right">Size</th><th className="text-right">Filled / Remaining</th><th>Status</th><th className="text-right">Action</th></tr></thead>
              <tbody className="font-mono tabular-nums">
                {openOrders.map((order) => {
                  const canceling = cancelingIds?.has(order.order_id);
                  return (
                    <tr key={order.order_id} className="border-t border-white/5">
                      <td className="py-1.5 text-slate-500">{bjTime(order.created_at)}</td><td className="py-1.5 text-slate-200">{order.symbol}</td><td className={`py-1.5 ${sideColor(order.side)}`}>{order.side}</td><td className="py-1.5 text-slate-400">{typeLabel(order.type)} · {tifLabel(order.tif)}</td><td className="py-1.5 text-right text-slate-200">{order.type === "limit" ? fmt(order.price, priceDigits) : "Market"}</td><td className="py-1.5 text-right text-slate-300">{fmt(order.quantity, quantityDigits)}</td><td className="py-1.5 text-right text-slate-400">{fmt(order.filled_quantity, quantityDigits)} / {fmt(order.remaining_quantity, quantityDigits)}</td><td className="py-1.5"><StatusPill tone={orderStatusTone(order.status)}>{orderStatusLabel(order.status)}</StatusPill></td>
                      <td className="py-1.5 text-right"><div className="flex justify-end gap-1">{onAmend && order.type === "limit" ? <button type="button" onClick={() => onAmend(order)} className="rounded bg-white/6 px-1.5 py-0.5 text-[10px] text-slate-300 hover:bg-white/12">Amend</button> : null}<button type="button" disabled={canceling} onClick={() => onCancel(order)} className="rounded bg-rose-400/12 px-1.5 py-0.5 text-[10px] text-rose-200 disabled:opacity-50">{canceling ? "Canceling" : "Cancel"}</button></div></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )
        ) : null}

        {tab === "history" ? (
          historyOrders.length === 0 ? <EmptyState compact title="No order history" /> : (
            <table className="w-full min-w-[720px] text-left text-[11px]"><thead className="text-[9px] uppercase tracking-[0.12em] text-slate-600"><tr><th>Time</th><th>Market</th><th>Side</th><th>Type</th><th className="text-right">Price</th><th className="text-right">Size</th><th className="text-right">Avg Price</th><th>Status</th></tr></thead><tbody className="font-mono tabular-nums">{historyOrders.slice(0, 200).map((order) => <tr key={order.order_id} className="border-t border-white/5"><td className="py-1.5 text-slate-500">{bjTime(order.created_at)}</td><td className="py-1.5 text-slate-200">{order.symbol}</td><td className={`py-1.5 ${sideColor(order.side)}`}>{order.side}</td><td className="py-1.5 text-slate-400">{typeLabel(order.type)} · {tifLabel(order.tif)}</td><td className="py-1.5 text-right text-slate-200">{order.type === "limit" ? fmt(order.price, priceDigits) : "Market"}</td><td className="py-1.5 text-right text-slate-300">{fmt(order.quantity, quantityDigits)}</td><td className="py-1.5 text-right text-slate-300">{fmt(order.avg_price, priceDigits)}</td><td className="py-1.5"><StatusPill tone={orderStatusTone(order.status)} title={order.reject_reason ?? undefined}>{orderStatusLabel(order.status)}</StatusPill></td></tr>)}</tbody></table>
          )
        ) : null}

        {tab === "fills" ? (
          fills.length === 0 ? <EmptyState compact title="No trade history" /> : (
            <table className="w-full min-w-[720px] text-left text-[11px]"><thead className="text-[9px] uppercase tracking-[0.12em] text-slate-600"><tr><th>Time</th><th>Market</th><th>Side</th><th className="text-right">Price</th><th className="text-right">Size</th><th className="text-right">Value</th><th className="text-right">Fee</th><th className="text-right">Realized PNL</th></tr></thead><tbody className="font-mono tabular-nums">{fills.slice(0, 200).map((item) => <tr key={`${item.trade_id}-${item.ts ?? item.executed_at}`} className="border-t border-white/5"><td className="py-1.5 text-slate-500">{bjTime(item.ts ?? item.executed_at ?? Date.now())}</td><td className="py-1.5 text-slate-200">{item.symbol}</td><td className={`py-1.5 ${sideColor(item.side ?? item.taker_side)}`}>{item.side ?? item.taker_side ?? "-"}</td><td className="py-1.5 text-right text-slate-200">{fmt(item.price, priceDigits)}</td><td className="py-1.5 text-right text-slate-300">{fmt(item.quantity, quantityDigits)}</td><td className="py-1.5 text-right text-slate-300">{fmt(item.quote_amount, 2)}</td><td className="py-1.5 text-right text-slate-400">{fmt(item.fee, 4)}</td><td className={`py-1.5 text-right ${sideColor(Number(item.realized_pnl ?? 0) >= 0 ? "buy" : "sell")}`}>{item.realized_pnl !== undefined && item.realized_pnl !== null ? fmt(item.realized_pnl, 2) : "-"}</td></tr>)}</tbody></table>
          )
        ) : null}
      </div>
    </section>
  );
}

export const orderIsOpen = isOpenOrder;
