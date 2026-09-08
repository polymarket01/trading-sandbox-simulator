import { fmt, fmtPct, sideColor } from "../lib/format";
import { positionSideLabel, riskStatusLabel, riskStatusTone } from "../lib/paper";
import type { PaperPosition } from "../types";
import { EmptyState, StatusPill, SkeletonRows } from "./ui";

export function PositionsPanel({
  positions,
  priceDigits,
  quantityDigits,
  loading,
  closingIds,
  onClose,
}: {
  positions: PaperPosition[];
  priceDigits: number;
  quantityDigits: number;
  loading?: boolean;
  closingIds?: Set<string>;
  onClose?: (position: PaperPosition) => void;
}) {
  if (loading && positions.length === 0) {
    return (
      <section className="panel rounded-2xl p-4">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="font-display text-sm">当前仓位</h3>
        </div>
        <SkeletonRows rows={3} />
      </section>
    );
  }
  if (positions.length === 0) {
    return (
      <section className="panel rounded-2xl p-4">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="font-display text-sm">当前仓位</h3>
          <span className="text-[11px] text-slate-500">{positions.length} 个</span>
        </div>
        <EmptyState compact title="暂无仓位" detail="在下方下单面板开仓后，仓位会实时显示在这里。" />
      </section>
    );
  }
  return (
    <section className="panel rounded-2xl p-4">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="font-display text-sm">当前仓位</h3>
        <span className="text-[11px] text-slate-500">{positions.length} 个 · 数据来源 Paper 合约账户</span>
      </div>
      <div className="scrollbar overflow-x-auto">
        <table className="w-full min-w-[980px] text-left text-xs">
          <thead className="text-[11px] uppercase tracking-[0.12em] text-slate-500">
            <tr>
              <th className="pb-2">合约</th>
              <th className="pb-2">方向</th>
              <th className="pb-2 text-right">数量</th>
              <th className="pb-2 text-right">开仓均价</th>
              <th className="pb-2 text-right">标记价格</th>
              <th className="pb-2 text-right">杠杆</th>
              <th className="pb-2 text-right">持仓保证金</th>
              <th className="pb-2 text-right">未实现盈亏</th>
              <th className="pb-2 text-right">收益率</th>
              <th className="pb-2 text-right">预估强平价</th>
              <th className="pb-2 text-center">风险</th>
              <th className="pb-2 text-right">操作</th>
            </tr>
          </thead>
          <tbody className="font-mono tabular-nums">
            {positions.map((position) => {
              const quantity = Number(position.quantity || 0);
              const upnl = Number(position.unrealized_pnl || 0);
              const margin = Number(position.isolated_margin || 0);
              const roi = margin > 0 ? (upnl / margin) * 100 : 0;
              const isLong = position.side === "long";
              const closing = closingIds?.has(position.symbol);
              return (
                <tr key={position.symbol} className="border-t border-white/5">
                  <td className="py-2.5 text-slate-200">{position.symbol}</td>
                  <td className="py-2.5">
                    <span className={isLong ? "text-emerald-300" : "text-rose-300"}>
                      {positionSideLabel(position.side)}
                    </span>
                  </td>
                  <td className="py-2.5 text-right text-slate-200">{fmt(quantity, quantityDigits)}</td>
                  <td className="py-2.5 text-right text-slate-300">{fmt(position.entry_price, priceDigits)}</td>
                  <td className="py-2.5 text-right text-slate-300">{fmt(position.mark_price, priceDigits)}</td>
                  <td className="py-2.5 text-right text-slate-300">{position.leverage}x</td>
                  <td className="py-2.5 text-right text-slate-300">{fmt(margin, 2)}</td>
                  <td className={`py-2.5 text-right ${sideColor(upnl >= 0 ? "buy" : "sell")}`}>{fmt(upnl, 2)}</td>
                  <td className={`py-2.5 text-right ${sideColor(roi >= 0 ? "buy" : "sell")}`}>{fmtPct(roi, 2)}</td>
                  <td className="py-2.5 text-right text-amber-200/90">{fmt(position.liquidation_price, priceDigits)}</td>
                  <td className="py-2.5 text-center">
                    <StatusPill tone={riskStatusTone(position.risk_status)} title={position.margin_buffer ? `保证金缓冲 ${fmt(position.margin_buffer, 2)}` : undefined}>
                      {riskStatusLabel(position.risk_status)}
                    </StatusPill>
                  </td>
                  <td className="py-2.5 text-right">
                    <button
                      type="button"
                      disabled={closing}
                      onClick={() => onClose?.(position)}
                      className="rounded-lg bg-amber-400/12 px-2.5 py-1 text-[11px] text-amber-100 hover:bg-amber-400/20 disabled:opacity-50"
                    >
                      {closing ? "平仓中..." : "市价平仓"}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
