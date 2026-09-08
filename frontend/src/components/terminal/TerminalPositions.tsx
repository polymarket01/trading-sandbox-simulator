import { fmt, fmtPct, sideColor } from "../../lib/format";
import { positionSideLabel, riskStatusLabel, riskStatusTone } from "../../lib/paper";
import { StatusPill } from "../ui";
import type { TerminalPosition } from "./types";

export function TerminalPositions({
  positions,
  priceDigits,
  quantityDigits,
  closingIds,
  onClose,
}: {
  positions: TerminalPosition[];
  priceDigits: number;
  quantityDigits: number;
  closingIds?: Set<string>;
  onClose: (position: TerminalPosition) => void;
}) {
  return (
    <section className="panel flex min-h-0 min-w-0 flex-col rounded-2xl p-2">
      <div className="mb-1.5 flex shrink-0 items-center justify-between px-1">
        <h3 className="font-display text-xs text-slate-200">当前仓位</h3>
        <span className="text-[10px] text-slate-500">{positions.length} 个</span>
      </div>
      {positions.length === 0 ? (
        <div className="flex flex-1 items-center justify-center rounded-lg border border-dashed border-white/10 bg-slate-950/25 px-3 py-6 text-xs text-slate-500">
          暂无仓位 · 开仓后实时显示
        </div>
      ) : (
        <div className="scrollbar min-h-0 flex-1 overflow-x-auto">
          <table className="w-full min-w-[760px] text-left text-[11px]">
            <thead className="text-[9px] uppercase tracking-[0.12em] text-slate-600">
              <tr>
                <th className="pb-1">合约</th>
                <th className="pb-1">方向</th>
                <th className="pb-1 text-right">数量</th>
                <th className="pb-1 text-right">开仓均价</th>
                <th className="pb-1 text-right">标记价</th>
                <th className="pb-1 text-right">杠杆</th>
                <th className="pb-1 text-right">未实现盈亏</th>
                <th className="pb-1 text-right">收益率</th>
                <th className="pb-1 text-right">强平价</th>
                <th className="pb-1 text-center">风险</th>
                <th className="pb-1 text-right">操作</th>
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
                    <td className="py-1.5 text-slate-200">{position.symbol}</td>
                    <td className={`py-1.5 ${isLong ? "text-emerald-300" : "text-rose-300"}`}>{positionSideLabel(position.side)}</td>
                    <td className="py-1.5 text-right text-slate-200">{fmt(quantity, quantityDigits)}</td>
                    <td className="py-1.5 text-right text-slate-300">{fmt(position.entry_price, priceDigits)}</td>
                    <td className="py-1.5 text-right text-slate-300">{fmt(position.mark_price, priceDigits)}</td>
                    <td className="py-1.5 text-right text-slate-300">{position.leverage}x</td>
                    <td className={`py-1.5 text-right ${sideColor(upnl >= 0 ? "buy" : "sell")}`}>{fmt(upnl, 2)}</td>
                    <td className={`py-1.5 text-right ${sideColor(roi >= 0 ? "buy" : "sell")}`}>{fmtPct(roi, 2)}</td>
                    <td className="py-1.5 text-right text-amber-200/90">{fmt(position.liquidation_price, priceDigits)}</td>
                    <td className="py-1.5 text-center">
                      <StatusPill tone={riskStatusTone(position.risk_status)} title={position.margin_buffer ? `保证金缓冲 ${fmt(position.margin_buffer, 2)}` : undefined}>
                        {riskStatusLabel(position.risk_status)}
                      </StatusPill>
                    </td>
                    <td className="py-1.5 text-right">
                      <button
                        type="button"
                        disabled={closing}
                        onClick={() => onClose(position)}
                        className="rounded bg-amber-400/12 px-2 py-0.5 text-[10px] text-amber-100 hover:bg-amber-400/20 disabled:opacity-50"
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
      )}
    </section>
  );
}
