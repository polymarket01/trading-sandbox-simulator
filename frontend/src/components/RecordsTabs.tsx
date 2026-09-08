import { useState } from "react";
import { bjTime, fmt, sideColor } from "../lib/format";
import { isOpenOrder, orderStatusLabel, orderStatusTone, tifLabel, typeLabel } from "../lib/paper";
import type { OrderItem, TradeItem } from "../types";
import { EmptyState, SkeletonRows, StatusPill, TabBar } from "./ui";

type RecordsTab = "open" | "history" | "fills";

export function RecordsTabs({
  openOrders,
  historyOrders,
  fills,
  priceDigits,
  quantityDigits,
  loading,
  cancelingIds,
  onCancel,
  onAmend,
  onViewAll,
}: {
  openOrders: OrderItem[];
  historyOrders: OrderItem[];
  fills: TradeItem[];
  priceDigits: number;
  quantityDigits: number;
  loading?: boolean;
  cancelingIds?: Set<string>;
  onCancel: (order: OrderItem) => void;
  onAmend?: (order: OrderItem) => void;
  onViewAll?: () => void;
}) {
  const [tab, setTab] = useState<RecordsTab>("open");

  if (loading) {
    return (
      <section className="panel rounded-2xl p-4">
        <SkeletonRows rows={4} />
      </section>
    );
  }

  return (
    <section className="panel rounded-2xl p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <TabBar<RecordsTab>
          items={[
            { key: "open", label: "当前委托", count: openOrders.length },
            { key: "history", label: "历史委托", count: historyOrders.length },
            { key: "fills", label: "我的成交", count: fills.length },
          ]}
          value={tab}
          onChange={setTab}
        />
        {onViewAll ? (
          <button type="button" onClick={onViewAll} className="rounded-lg bg-white/6 px-2.5 py-1.5 text-xs text-slate-300 hover:bg-white/10">
            查看全部订单
          </button>
        ) : null}
      </div>

      <div className="mt-3">
        {tab === "open" ? (
          openOrders.length === 0 ? (
            <EmptyState compact title="暂无当前委托" detail="在右侧下单面板提交订单后，会实时显示在这里。" />
          ) : (
            <div className="scrollbar max-h-[300px] overflow-auto">
              <table className="w-full min-w-[820px] text-left text-xs">
                <thead className="text-[11px] uppercase tracking-[0.12em] text-slate-500">
                  <tr>
                    <th className="pb-2">时间</th>
                    <th className="pb-2">市场</th>
                    <th className="pb-2">方向</th>
                    <th className="pb-2">类型</th>
                    <th className="pb-2 text-right">价格</th>
                    <th className="pb-2 text-right">数量</th>
                    <th className="pb-2 text-right">已成交 / 剩余</th>
                    <th className="pb-2 text-center">状态</th>
                    <th className="pb-2 text-right">操作</th>
                  </tr>
                </thead>
                <tbody className="font-mono tabular-nums">
                  {openOrders.map((order) => {
                    const canceling = cancelingIds?.has(order.order_id);
                    return (
                      <tr key={order.order_id} className="border-t border-white/5">
                        <td className="py-2 text-slate-500">{bjTime(order.created_at)}</td>
                        <td className="py-2 text-slate-200">{order.symbol}</td>
                        <td className={`py-2 ${sideColor(order.side)}`}>{order.side === "buy" ? "买" : "卖"}</td>
                        <td className="py-2 text-slate-400">
                          {typeLabel(order.type)} · {tifLabel(order.tif)}
                        </td>
                        <td className="py-2 text-right text-slate-200">{order.type === "limit" ? fmt(order.price, priceDigits) : "市价"}</td>
                        <td className="py-2 text-right text-slate-300">{fmt(order.quantity, quantityDigits)}</td>
                        <td className="py-2 text-right text-slate-400">
                          {fmt(order.filled_quantity, quantityDigits)} / {fmt(order.remaining_quantity, quantityDigits)}
                        </td>
                        <td className="py-2 text-center">
                          <StatusPill tone={orderStatusTone(order.status)}>{orderStatusLabel(order.status)}</StatusPill>
                        </td>
                        <td className="py-2 text-right">
                          <div className="flex justify-end gap-1.5">
                            {onAmend && order.type === "limit" ? (
                              <button
                                type="button"
                                onClick={() => onAmend(order)}
                                className="rounded-lg bg-white/6 px-2 py-1 text-[11px] text-slate-300 hover:bg-white/12"
                              >
                                改单
                              </button>
                            ) : null}
                            <button
                              type="button"
                              disabled={canceling}
                              onClick={() => onCancel(order)}
                              className="rounded-lg bg-rose-400/12 px-2 py-1 text-[11px] text-rose-200 hover:bg-rose-400/20 disabled:opacity-50"
                            >
                              {canceling ? "撤单中..." : "撤单"}
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )
        ) : null}

        {tab === "history" ? (
          historyOrders.length === 0 ? (
            <EmptyState compact title="暂无历史委托" detail="已成交、已撤销和已拒绝的订单会出现在这里。" />
          ) : (
            <div className="scrollbar max-h-[300px] overflow-auto">
              <table className="w-full min-w-[820px] text-left text-xs">
                <thead className="text-[11px] uppercase tracking-[0.12em] text-slate-500">
                  <tr>
                    <th className="pb-2">时间</th>
                    <th className="pb-2">市场</th>
                    <th className="pb-2">方向</th>
                    <th className="pb-2">类型</th>
                    <th className="pb-2 text-right">价格</th>
                    <th className="pb-2 text-right">数量</th>
                    <th className="pb-2 text-right">成交均价</th>
                    <th className="pb-2 text-center">状态</th>
                  </tr>
                </thead>
                <tbody className="font-mono tabular-nums">
                  {historyOrders.map((order) => (
                    <tr key={order.order_id} className="border-t border-white/5">
                      <td className="py-2 text-slate-500">{bjTime(order.created_at)}</td>
                      <td className="py-2 text-slate-200">{order.symbol}</td>
                      <td className={`py-2 ${sideColor(order.side)}`}>{order.side === "buy" ? "买" : "卖"}</td>
                      <td className="py-2 text-slate-400">
                        {typeLabel(order.type)} · {tifLabel(order.tif)}
                      </td>
                      <td className="py-2 text-right text-slate-200">{order.type === "limit" ? fmt(order.price, priceDigits) : "市价"}</td>
                      <td className="py-2 text-right text-slate-300">{fmt(order.quantity, quantityDigits)}</td>
                      <td className="py-2 text-right text-slate-300">{fmt(order.avg_price, priceDigits)}</td>
                      <td className="py-2 text-center">
                        <StatusPill tone={orderStatusTone(order.status)} title={order.reject_reason ?? undefined}>
                          {orderStatusLabel(order.status)}
                        </StatusPill>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        ) : null}

        {tab === "fills" ? (
          fills.length === 0 ? (
            <EmptyState compact title="暂无成交记录" detail="成交后这里会显示每一笔成交的方向、价格、数量和时间。" />
          ) : (
            <div className="scrollbar max-h-[300px] overflow-auto">
              <table className="w-full min-w-[820px] text-left text-xs">
                <thead className="text-[11px] uppercase tracking-[0.12em] text-slate-500">
                  <tr>
                    <th className="pb-2">时间</th>
                    <th className="pb-2">市场</th>
                    <th className="pb-2">方向</th>
                    <th className="pb-2 text-right">价格</th>
                    <th className="pb-2 text-right">数量</th>
                    <th className="pb-2 text-right">金额</th>
                    <th className="pb-2 text-right">手续费</th>
                    <th className="pb-2 text-right">已实现盈亏</th>
                    <th className="pb-2 text-center">来源</th>
                  </tr>
                </thead>
                <tbody className="font-mono tabular-nums">
                  {fills.slice(0, 100).map((item) => (
                    <tr key={`${item.trade_id}-${item.ts ?? item.executed_at}`} className="border-t border-white/5">
                      <td className="py-2 text-slate-500">{bjTime(item.ts ?? item.executed_at ?? Date.now())}</td>
                      <td className="py-2 text-slate-200">{item.symbol}</td>
                      <td className={`py-2 ${sideColor(item.side ?? item.taker_side)}`}>
                        {item.side === "buy" || item.taker_side === "buy" ? "买" : "卖"}
                        {item.position_action || item.taker_position_action ? (
                          <span className="ml-1 text-slate-500">· {item.position_action === "close" || item.taker_position_action === "close" ? "平仓" : "开仓"}</span>
                        ) : null}
                      </td>
                      <td className="py-2 text-right text-slate-200">{fmt(item.price, priceDigits)}</td>
                      <td className="py-2 text-right text-slate-300">{fmt(item.quantity, quantityDigits)}</td>
                      <td className="py-2 text-right text-slate-300">{fmt(item.quote_amount, 2)}</td>
                      <td className="py-2 text-right text-slate-400">{fmt(item.fee, 4)}</td>
                      <td className={`py-2 text-right ${sideColor(Number(item.realized_pnl ?? 0) >= 0 ? "buy" : "sell")}`}>
                        {item.realized_pnl !== undefined && item.realized_pnl !== null ? fmt(item.realized_pnl, 2) : "—"}
                      </td>
                      <td className="py-2 text-center text-slate-400">{item.source ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        ) : null}
      </div>
    </section>
  );
}

export const orderIsOpen = isOpenOrder;
