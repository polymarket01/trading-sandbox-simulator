import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { ConfirmDialog, EmptyState, ErrorState, SkeletonBlock, StatusPill, TabBar } from "../components/ui";
import { bjDateTime, bjTime, fmt, sideColor } from "../lib/format";
import { isOpenOrder, orderStatusLabel, orderStatusTone, tifLabel, typeLabel } from "../lib/paper";
import { useAppStore } from "../store/useAppStore";
import type { OrderItem, PaperFundingItem, PaperLiquidationItem, PaperMarket, TradeItem } from "../types";

type OrderTab = "open" | "history" | "fills" | "funding" | "liquidation";

function toCsv(rows: Record<string, unknown>[], headers: Record<string, string>): string {
  const escape = (value: unknown) => {
    const text = String(value ?? "");
    return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  const lines = [Object.values(headers).join(",")];
  rows.forEach((row) => {
    lines.push(Object.keys(headers).map((key) => escape(row[key])).join(","));
  });
  return lines.join("\n");
}

export function PaperOrdersPage() {
  const pushToast = useAppStore((state) => state.pushToast);
  const [tab, setTab] = useState<OrderTab>("open");
  const [orders, setOrders] = useState<OrderItem[]>([]);
  const [trades, setTrades] = useState<TradeItem[]>([]);
  const [funding, setFunding] = useState<PaperFundingItem[]>([]);
  const [liquidations, setLiquidations] = useState<PaperLiquidationItem[]>([]);
  const [markets, setMarkets] = useState<PaperMarket[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [productFilter, setProductFilter] = useState<string>("");
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [cancelingIds, setCancelingIds] = useState<Set<string>>(() => new Set());
  const [amendTarget, setAmendTarget] = useState<OrderItem>();
  const [cancelAllOpen, setCancelAllOpen] = useState(false);
  const [cancelAllBusy, setCancelAllBusy] = useState(false);

  const marketSymbols = useMemo(() => {
    const map = new Map<number, string>();
    markets.forEach((market) => map.set(market.id, market.symbol));
    return map;
  }, [markets]);

  const refresh = useCallback(async () => {
    const [orderResponse, tradeResponse, fundingResponse, liquidationResponse, marketResponse] = await Promise.all([
      api.get<{ items: OrderItem[] }>("/paper/orders?limit=500"),
      api.get<{ items: TradeItem[] }>("/paper/trades?limit=500"),
      api.get<{ items: PaperFundingItem[] }>("/paper/funding?limit=500"),
      api.get<{ items: PaperLiquidationItem[] }>("/paper/liquidations?limit=500"),
      api.get<{ items: PaperMarket[] }>("/paper/markets"),
    ]);
    setOrders(orderResponse.items);
    setTrades(tradeResponse.items);
    setFunding(fundingResponse.items);
    setLiquidations(liquidationResponse.items);
    setMarkets(marketResponse.items);
    setLoading(false);
    setError("");
  }, []);

  useEffect(() => {
    void refresh().catch((reason) => {
      setError(reason instanceof Error ? reason.message : "订单读取失败");
      setLoading(false);
    });
  }, [refresh]);

  const openOrders = useMemo(() => orders.filter((item) => isOpenOrder(item.status)), [orders]);
  const historyOrders = useMemo(() => orders.filter((item) => !isOpenOrder(item.status)), [orders]);
  const openSymbols = useMemo(() => [...new Set(openOrders.map((item) => item.symbol))], [openOrders]);

  const filtered = useCallback(
    (items: OrderItem[]) =>
      items.filter((item) => {
        if (productFilter && item.product_type !== productFilter) return false;
        if (statusFilter && item.status !== statusFilter) return false;
        return true;
      }),
    [productFilter, statusFilter],
  );

  const cancel = async (orderId: string) => {
    setCancelingIds((current) => new Set(current).add(orderId));
    try {
      await api.delete(`/paper/orders/${encodeURIComponent(orderId)}`);
      pushToast("success", `订单已撤销 · ${orderId}`);
      await refresh();
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "撤单失败";
      pushToast("error", `撤单失败：${message}`);
    } finally {
      setCancelingIds((current) => {
        const next = new Set(current);
        next.delete(orderId);
        return next;
      });
    }
  };

  const cancelAll = async () => {
    setCancelAllBusy(true);
    let failed = 0;
    for (const symbol of openSymbols) {
      try {
        await api.delete(`/paper/orders?symbol=${encodeURIComponent(symbol)}`);
      } catch {
        failed += 1;
      }
    }
    pushToast(failed === 0 ? "success" : "error", failed === 0 ? "全部当前委托已撤销" : `部分市场撤单失败（${failed} 个）`);
    setCancelAllOpen(false);
    setCancelAllBusy(false);
    await refresh();
  };

  const amend = async (nextPrice?: string, nextQuantity?: string) => {
    if (!amendTarget) return;
    const payload: Record<string, unknown> = {};
    if (nextPrice && nextPrice !== String(amendTarget.price)) payload.price = nextPrice;
    if (nextQuantity && Number(nextQuantity) !== Number(amendTarget.quantity)) payload.quantity = nextQuantity;
    if (Object.keys(payload).length === 0) {
      setAmendTarget(undefined);
      return;
    }
    try {
      await api.patch(`/paper/orders/${encodeURIComponent(amendTarget.order_id)}`, payload);
      pushToast("success", "订单已修改");
      setAmendTarget(undefined);
      await refresh();
    } catch (reason) {
      pushToast("error", `改单失败：${reason instanceof Error ? reason.message : "未知错误"}`);
    }
  };

  const exportCsv = () => {
    let rows: Record<string, unknown>[] = [];
    let headers: Record<string, string> = {};
    if (tab === "open" || tab === "history") {
      const source = tab === "open" ? openOrders : historyOrders;
      rows = filtered(source).map((order) => ({
        order_id: order.order_id,
        created_at: bjDateTime(order.created_at),
        symbol: order.symbol,
        product: order.product_type,
        side: order.side,
        type: typeLabel(order.type),
        tif: tifLabel(order.tif),
        status: orderStatusLabel(order.status),
        price: order.price ?? "市价",
        quantity: order.quantity,
        filled: order.filled_quantity,
        remaining: order.remaining_quantity,
        avg_price: order.avg_price ?? "",
        reject_reason: order.reject_reason ?? "",
      }));
      headers = {
        order_id: "订单号",
        created_at: "时间",
        symbol: "市场",
        product: "产品",
        side: "方向",
        type: "类型",
        tif: "生效方式",
        status: "状态",
        price: "价格",
        quantity: "数量",
        filled: "已成交",
        remaining: "剩余",
        avg_price: "成交均价",
        reject_reason: "拒绝原因",
      };
    } else if (tab === "fills") {
      rows = trades.map((item) => ({
        trade_id: item.trade_id,
        created_at: bjDateTime(item.ts ?? item.executed_at),
        symbol: item.symbol,
        side: item.side ?? item.taker_side,
        price: item.price,
        quantity: item.quantity,
        amount: item.quote_amount ?? "",
        fee: item.fee ?? "",
        realized_pnl: item.realized_pnl ?? "",
        source: item.source ?? "",
      }));
      headers = { trade_id: "成交号", created_at: "时间", symbol: "市场", side: "方向", price: "价格", quantity: "数量", amount: "金额", fee: "手续费", realized_pnl: "已实现盈亏", source: "来源" };
    } else if (tab === "funding") {
      rows = funding.map((item) => ({
        created_at: bjDateTime(item.created_at),
        symbol: marketSymbols.get(item.market_id) ?? `market#${item.market_id}`,
        rate: `${(Number(item.rate) * 100).toFixed(6)}%`,
        amount: item.amount,
      }));
      headers = { created_at: "时间", symbol: "市场", rate: "费率", amount: "支付/收取金额" };
    } else if (tab === "liquidation") {
      rows = liquidations.map((item) => ({
        created_at: bjDateTime(item.created_at),
        symbol: marketSymbols.get(item.market_id) ?? `market#${item.market_id}`,
        quantity: item.quantity,
        realized_pnl: item.realized_pnl,
        reason: item.reason,
      }));
      headers = { created_at: "时间", symbol: "市场", quantity: "数量", realized_pnl: "已实现盈亏", reason: "原因" };
    }
    const blob = new Blob(["\uFEFF" + toCsv(rows, headers)], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `paper-orders-${tab}-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.csv`;
    link.click();
    URL.revokeObjectURL(url);
    pushToast("success", "CSV 已导出");
  };

  const statusOptions = useMemo(() => {
    const set = new Set<string>();
    orders.forEach((item) => set.add(item.status));
    return Array.from(set).sort();
  }, [orders]);

  return (
    <>
      <div className="space-y-3">
        <section className="panel rounded-2xl p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h1 className="font-display text-xl text-white">订单与成交</h1>
              <p className="mt-1 text-xs text-slate-500">全部为本机 Paper matcher 真实处理的订单与成交记录，可导出 CSV。</p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <select value={productFilter} onChange={(event) => setProductFilter(event.target.value)} className="paper-input w-auto px-3 py-2 text-xs">
                <option value="">全部产品</option>
                <option value="SPOT">现货</option>
                <option value="PERP">永续</option>
              </select>
              {tab === "open" || tab === "history" ? (
                <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)} className="paper-input w-auto px-3 py-2 text-xs">
                  <option value="">全部状态</option>
                  {statusOptions.map((item) => (
                    <option key={item} value={item}>
                      {orderStatusLabel(item)}
                    </option>
                  ))}
                </select>
              ) : null}
              {tab === "open" && openSymbols.length > 0 ? (
                <button type="button" onClick={() => setCancelAllOpen(true)} className="rounded-xl bg-rose-400/10 px-3 py-2 text-xs text-rose-200 hover:bg-rose-400/20">
                  全部撤单（{openSymbols.length} 个市场）
                </button>
              ) : null}
              <button type="button" onClick={() => void refresh()} className="rounded-xl bg-white/6 px-3 py-2 text-xs text-slate-300 hover:bg-white/10">
                刷新
              </button>
              <button type="button" onClick={exportCsv} className="rounded-xl bg-cyan-400/12 px-3 py-2 text-xs text-cyan-100 hover:bg-cyan-400/20">
                导出 CSV
              </button>
            </div>
          </div>
          <div className="mt-3">
            <TabBar<OrderTab>
              items={[
                { key: "open", label: "当前委托", count: openOrders.length },
                { key: "history", label: "历史委托", count: historyOrders.length },
                { key: "fills", label: "成交记录", count: trades.length },
                { key: "funding", label: "资金费率", count: funding.length },
                { key: "liquidation", label: "强平记录", count: liquidations.length },
              ]}
              value={tab}
              onChange={setTab}
            />
          </div>
          {error ? <div className="mt-3 rounded-xl bg-rose-400/10 px-3 py-2 text-xs text-rose-200">{error}</div> : null}
        </section>

        {loading ? (
          <section className="panel rounded-2xl p-4">
            <SkeletonBlock className="h-8" />
            <div className="mt-4 space-y-2">
              {Array.from({ length: 8 }).map((_, index) => (
                <SkeletonBlock key={index} className="h-9" />
              ))}
            </div>
          </section>
        ) : (
          <section className="panel rounded-2xl p-4">
            {tab === "open" ? (
              filtered(openOrders).length === 0 ? (
                <EmptyState title="暂无当前委托" detail="下单后未成交或部分成交的订单会显示在这里。" />
              ) : (
                <OrderTable
                  rows={filtered(openOrders)}
                  cancelingIds={cancelingIds}
                  onCancel={(order) => void cancel(order.order_id)}
                  onAmend={setAmendTarget}
                  showActions
                />
              )
            ) : null}
            {tab === "history" ? (
              filtered(historyOrders).length === 0 ? (
                <EmptyState title="暂无历史委托" />
              ) : (
                <OrderTable rows={filtered(historyOrders)} showActions={false} />
              )
            ) : null}
            {tab === "fills" ? (
              trades.length === 0 ? (
                <EmptyState title="暂无成交记录" detail="每一笔真实进入 Paper matcher 的成交都会记录在这里。" />
              ) : (
                <div className="scrollbar overflow-x-auto">
                  <table className="w-full min-w-[900px] text-left text-xs">
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
                      {trades.slice(0, 300).map((item) => (
                        <tr key={`${item.trade_id}-${item.ts ?? item.executed_at}`} className="border-t border-white/5">
                          <td className="py-2 text-slate-500">{bjTime(item.ts ?? item.executed_at ?? Date.now())}</td>
                          <td className="py-2 text-slate-200">{item.symbol}</td>
                          <td className={`py-2 ${sideColor(item.side ?? item.taker_side)}`}>
                            {item.side === "buy" || item.taker_side === "buy" ? "买" : "卖"}
                            {item.position_action || item.taker_position_action ? (
                              <span className="ml-1 text-slate-500">· {item.position_action === "close" || item.taker_position_action === "close" ? "平" : "开"}</span>
                            ) : null}
                          </td>
                          <td className="py-2 text-right text-slate-200">{fmt(item.price, 8)}</td>
                          <td className="py-2 text-right text-slate-300">{fmt(item.quantity, 8)}</td>
                          <td className="py-2 text-right text-slate-300">{fmt(item.quote_amount, 2)}</td>
                          <td className="py-2 text-right text-slate-400">{fmt(item.fee, 6)}</td>
                          <td className={`py-2 text-right ${sideColor(Number(item.realized_pnl ?? 0) >= 0 ? "buy" : "sell")}`}>{item.realized_pnl !== undefined && item.realized_pnl !== null ? fmt(item.realized_pnl, 2) : "—"}</td>
                          <td className="py-2 text-center text-slate-400">{item.source ?? "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )
            ) : null}
            {tab === "funding" ? (
              funding.length === 0 ? (
                <EmptyState title="暂无资金费率记录" detail="资金费率结算记录会显示在这里。" />
              ) : (
                <div className="scrollbar overflow-x-auto">
                  <table className="w-full min-w-[600px] text-left text-xs">
                    <thead className="text-[11px] uppercase tracking-[0.12em] text-slate-500">
                      <tr>
                        <th className="pb-2">时间</th>
                        <th className="pb-2">市场</th>
                        <th className="pb-2 text-right">费率</th>
                        <th className="pb-2 text-right">支付 / 收取金额（USDT）</th>
                      </tr>
                    </thead>
                    <tbody className="font-mono tabular-nums">
                      {funding.slice(0, 300).map((item) => (
                        <tr key={item.event_id} className="border-t border-white/5">
                          <td className="py-2 text-slate-500">{bjDateTime(item.created_at)}</td>
                          <td className="py-2 text-slate-200">{marketSymbols.get(item.market_id) ?? `market#${item.market_id}`}</td>
                          <td className={`py-2 text-right ${Number(item.rate) > 0 ? "text-rose-300" : Number(item.rate) < 0 ? "text-emerald-300" : "text-slate-400"}`}>{(Number(item.rate) * 100).toFixed(6)}%</td>
                          <td className="py-2 text-right text-slate-200">{fmt(item.amount, 4)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )
            ) : null}
            {tab === "liquidation" ? (
              liquidations.length === 0 ? (
                <EmptyState title="暂无强平记录" detail="强平事件（含保险基金与 ADL 处理）会记录在这里。" />
              ) : (
                <div className="scrollbar overflow-x-auto">
                  <table className="w-full min-w-[700px] text-left text-xs">
                    <thead className="text-[11px] uppercase tracking-[0.12em] text-slate-500">
                      <tr>
                        <th className="pb-2">时间</th>
                        <th className="pb-2">市场</th>
                        <th className="pb-2 text-right">数量</th>
                        <th className="pb-2 text-right">已实现盈亏</th>
                        <th className="pb-2">原因</th>
                      </tr>
                    </thead>
                    <tbody className="font-mono tabular-nums">
                      {liquidations.slice(0, 300).map((item) => (
                        <tr key={item.event_id} className="border-t border-white/5">
                          <td className="py-2 text-slate-500">{bjDateTime(item.created_at)}</td>
                          <td className="py-2 text-slate-200">{marketSymbols.get(item.market_id) ?? `market#${item.market_id}`}</td>
                          <td className="py-2 text-right text-slate-300">{fmt(item.quantity, 6)}</td>
                          <td className={`py-2 text-right ${sideColor(Number(item.realized_pnl) >= 0 ? "buy" : "sell")}`}>{fmt(item.realized_pnl, 2)}</td>
                          <td className="py-2 text-slate-400">{item.reason}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )
            ) : null}
          </section>
        )}
      </div>

      <AmendModal order={amendTarget} onClose={() => setAmendTarget(undefined)} onConfirm={(p, q) => void amend(p, q)} />
      <ConfirmDialog
        open={cancelAllOpen}
        title="全部撤单"
        danger
        busy={cancelAllBusy}
        confirmText={cancelAllBusy ? "撤单中..." : "确认全部撤单"}
        message={
          <>
            将撤销 <span className="font-mono text-slate-200">{openSymbols.length}</span> 个市场共{" "}
            <span className="font-mono text-slate-200">{openOrders.length}</span> 笔未完成订单，冻结资金将被释放。
          </>
        }
        onConfirm={() => void cancelAll()}
        onCancel={() => setCancelAllOpen(false)}
      />
    
    </>
  );
}

function OrderTable({
  rows,
  cancelingIds,
  onCancel,
  onAmend,
  showActions,
}: {
  rows: OrderItem[];
  cancelingIds?: Set<string>;
  onCancel?: (order: OrderItem) => void;
  onAmend?: (order: OrderItem) => void;
  showActions: boolean;
}) {
  return (
    <div className="scrollbar overflow-x-auto">
      <table className="w-full min-w-[900px] text-left text-xs">
        <thead className="text-[11px] uppercase tracking-[0.12em] text-slate-500">
          <tr>
            <th className="pb-2">时间</th>
            <th className="pb-2">市场</th>
            <th className="pb-2">产品</th>
            <th className="pb-2">方向</th>
            <th className="pb-2">类型</th>
            <th className="pb-2 text-right">价格</th>
            <th className="pb-2 text-right">数量</th>
            <th className="pb-2 text-right">已成交 / 剩余</th>
            <th className="pb-2 text-right">成交均价</th>
            <th className="pb-2 text-center">状态</th>
            {showActions ? <th className="pb-2 text-right">操作</th> : null}
          </tr>
        </thead>
        <tbody className="font-mono tabular-nums">
          {rows.slice(0, 300).map((order) => {
            const canceling = cancelingIds?.has(order.order_id);
            return (
              <tr key={order.order_id} className="border-t border-white/5">
                <td className="py-2 text-slate-500">{bjTime(order.created_at)}</td>
                <td className="py-2 text-slate-200">{order.symbol}</td>
                <td className="py-2 text-slate-400">{order.product_type === "PERP" ? "永续" : "现货"}</td>
                <td className={`py-2 ${sideColor(order.side)}`}>{order.side === "buy" ? "买" : "卖"}</td>
                <td className="py-2 text-slate-400">
                  {typeLabel(order.type)} · {tifLabel(order.tif)}
                  {order.position_action ? <span className="ml-1 text-slate-500">· {order.position_action === "close" ? "平仓" : "开仓"}</span> : null}
                </td>
                <td className="py-2 text-right text-slate-200">{order.type === "limit" ? fmt(order.price, 8) : "市价"}</td>
                <td className="py-2 text-right text-slate-300">{fmt(order.quantity, 8)}</td>
                <td className="py-2 text-right text-slate-400">
                  {fmt(order.filled_quantity, 8)} / {fmt(order.remaining_quantity, 8)}
                </td>
                <td className="py-2 text-right text-slate-300">{fmt(order.avg_price, 8)}</td>
                <td className="py-2 text-center">
                  <StatusPill tone={orderStatusTone(order.status)} title={order.reject_reason ?? undefined}>
                    {orderStatusLabel(order.status)}
                  </StatusPill>
                </td>
                {showActions ? (
                  <td className="py-2 text-right">
                    <div className="flex justify-end gap-1.5">
                      {onAmend && order.type === "limit" ? (
                        <button type="button" onClick={() => onAmend(order)} className="rounded-lg bg-white/6 px-2 py-1 text-[11px] text-slate-300 hover:bg-white/12">
                          改单
                        </button>
                      ) : null}
                      {onCancel ? (
                        <button type="button" disabled={canceling} onClick={() => onCancel(order)} className="rounded-lg bg-rose-400/12 px-2 py-1 text-[11px] text-rose-200 hover:bg-rose-400/20 disabled:opacity-50">
                          {canceling ? "撤单中..." : "撤单"}
                        </button>
                      ) : null}
                    </div>
                  </td>
                ) : null}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function AmendModal({ order, onClose, onConfirm }: { order?: OrderItem; onClose: () => void; onConfirm: (price?: string, quantity?: string) => void }) {
  const [nextPrice, setNextPrice] = useState("");
  const [nextQuantity, setNextQuantity] = useState("");
  useEffect(() => {
    if (!order) return;
    setNextPrice(order.price ?? "");
    setNextQuantity(order.quantity ?? "");
  }, [order]);
  if (!order) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm" role="dialog" aria-modal="true">
      <div className="w-full max-w-md rounded-3xl border border-white/10 bg-slate-900 p-5 shadow-glow">
        <h3 className="font-display text-base text-white">修改订单 · {order.symbol}</h3>
        <p className="mt-1 text-[11px] text-slate-500">同价减量保留原排队位置；改价或加量会移到档尾。已成交部分不能回退。</p>
        <div className="mt-4 space-y-3">
          <label className="block">
            <span className="mb-1 block text-xs text-slate-400">新价格</span>
            <input value={nextPrice} onChange={(event) => setNextPrice(event.target.value)} className="paper-input font-mono tabular-nums" inputMode="decimal" />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-slate-400">新数量（总数量）</span>
            <input value={nextQuantity} onChange={(event) => setNextQuantity(event.target.value)} className="paper-input font-mono tabular-nums" inputMode="decimal" />
          </label>
        </div>
        <div className="mt-5 flex justify-end gap-2">
          <button type="button" onClick={onClose} className="rounded-xl bg-white/6 px-4 py-2 text-sm text-slate-300 hover:bg-white/10">
            取消
          </button>
          <button
            type="button"
            onClick={() => {
              if (!nextPrice && !nextQuantity) return;
              onConfirm(nextPrice, nextQuantity);
            }}
            className="rounded-xl bg-cyan-300 px-4 py-2 text-sm font-semibold text-slate-950 hover:bg-cyan-200"
          >
            提交修改
          </button>
        </div>
      </div>
    </div>
  );
}
