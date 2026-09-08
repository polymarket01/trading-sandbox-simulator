import { useState } from "react";
import { fmt } from "../../lib/format";
import { marketLeverageOptions } from "../../lib/paper";
import type { BalanceItem, PaperPerpAccount } from "../../types";
import { StatusPill, type Tone } from "../ui";
import type { TerminalMarketInfo, TerminalOrderPanelState } from "./types";

export function TerminalOrderPanel({
  market,
  perpAccount,
  balances,
  state,
  onChange,
  latestPrice,
  bestBid,
  bestAsk,
  priceDigits,
  quantityDigits,
  marketStatus,
  maxLeverage,
  submitting,
  submitError,
  submitNotice,
  onSubmit,
}: {
  market?: TerminalMarketInfo;
  perpAccount?: PaperPerpAccount;
  balances: BalanceItem[];
  state: TerminalOrderPanelState;
  onChange: (patch: Partial<TerminalOrderPanelState>) => void;
  latestPrice: string;
  bestBid: string;
  bestAsk: string;
  priceDigits: number;
  quantityDigits: number;
  marketStatus: string;
  maxLeverage?: string;
  submitting: boolean;
  submitError?: string;
  submitNotice?: string;
  onSubmit: () => void;
}) {
  const [quickPercent, setQuickPercent] = useState(0);
  const isPerp = market?.product_type === "PERP";
  const balanceMap = Object.fromEntries(balances.map((item) => [item.asset, item]));
  const baseAsset = market?.base_asset ?? "-";
  const quoteAsset = market?.quote_asset ?? "USDT";
  const referencePrice = Number(latestPrice || 0) || Number(market?.reference_price ?? 0);
  const limitPrice = Number(state.price || 0);
  const effectivePrice = state.orderType === "limit" && limitPrice > 0 ? limitPrice : referencePrice;
  const quantity = Number(state.quantity || 0);
  const amount = quantity * effectivePrice;
  const takerFee = Number(market?.taker_fee_rate ?? "0") * 100;
  const estimatedFee = amount * (takerFee / 100);
  const availableQuote = Number(balanceMap[quoteAsset]?.available ?? 0);
  const availableBase = Number(balanceMap[baseAsset]?.available ?? 0);
  const leverage = Math.max(1, Number(state.leverage || 1));
  const estimatedMargin = isPerp ? amount / leverage : 0;
  const available = isPerp ? Number(perpAccount?.available_margin ?? 0) : state.side === "buy" ? availableQuote : availableBase;
  const minNotional = 5;
  const availableText = isPerp
    ? `${perpAccount?.available_margin === undefined ? "-" : fmt(perpAccount.available_margin, 2)} USDT`
    : `${fmt(available, state.side === "buy" ? 2 : quantityDigits)} ${state.side === "buy" ? quoteAsset : baseAsset}`;

  let validation: { tone: Tone; text: string } | null = null;
  if (quantity <= 0) validation = { tone: "warn", text: "Enter size" };
  else if (state.orderType === "limit" && limitPrice <= 0) validation = { tone: "warn", text: "Limit price is required" };
  else if (amount > 0 && amount < minNotional) validation = { tone: "warn", text: `Below minimum value ${minNotional} USDT` };
  else if (!isPerp && state.side === "buy" && amount > availableQuote) validation = { tone: "sell", text: "Insufficient balance" };
  else if (!isPerp && state.side === "sell" && quantity > availableBase) validation = { tone: "sell", text: `Insufficient ${baseAsset}` };
  else if (isPerp && state.positionAction === "open" && estimatedMargin > available) validation = { tone: "sell", text: "Insufficient margin" };

  const buyActive = state.side === "buy";
  const statusBlocked = !["TRADING", "REDUCE_ONLY"].includes(marketStatus);
  const reduceOnly = marketStatus === "REDUCE_ONLY";
  const canSubmit = !submitting && !statusBlocked && (!reduceOnly || (isPerp && state.positionAction === "close"));
  const submitLabel = submitting
    ? "Submitting..."
    : isPerp
      ? `提交${state.positionAction === "open" ? (state.side === "buy" ? "开多" : "开空") : (state.side === "buy" ? "平空" : "平多")}单`
      : `${state.side === "buy" ? "Buy" : "Sell"} ${market?.base_asset ?? ""}`;

  const setPercent = (percent: number) => {
    setQuickPercent(percent);
    const base = isPerp
      ? (available * percent) / 100 * leverage
      : state.side === "buy"
        ? (availableQuote * percent) / 100
        : (availableBase * percent) / 100;
    const nextQuantity = referencePrice > 0 ? base / referencePrice : 0;
    onChange({ quantity: nextQuantity > 0 ? nextQuantity.toFixed(quantityDigits) : "" });
  };

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape") onChange({ quantity: "" });
      }}
      className="panel hl-order-panel flex h-full min-h-0 flex-col rounded-2xl p-2.5"
    >
      <div className="hl-order-fields min-h-0 flex-1 overflow-y-auto pr-1">
      <div className="mb-2 flex shrink-0 items-center justify-between px-0.5">
        <div className="flex items-center gap-2">
          <span className="font-display text-xs text-slate-100">Order</span>
          <span className="text-[10px] text-slate-500">{market?.symbol ?? "-"}</span>
        </div>
        {isPerp ? <span className="rounded bg-white/6 px-1.5 py-0.5 text-[10px] text-slate-400">Cross</span> : <span className="rounded bg-white/6 px-1.5 py-0.5 text-[10px] text-slate-400">Spot</span>}
      </div>

      <div className="mb-2 grid shrink-0 grid-cols-2 gap-1">
        <button type="button" onClick={() => onChange({ side: "buy" })} className={`rounded-xl py-2 text-xs font-semibold transition ${buyActive ? "bg-emerald-400 text-slate-950" : "bg-white/5 text-slate-400 hover:bg-white/10"}`}>Buy / Long</button>
        <button type="button" onClick={() => onChange({ side: "sell" })} className={`rounded-xl py-2 text-xs font-semibold transition ${!buyActive ? "bg-rose-400 text-slate-950" : "bg-white/5 text-slate-400 hover:bg-white/10"}`}>Sell / Short</button>
      </div>

      <div className="mb-2 grid shrink-0 grid-cols-2 border-b border-white/8">
        <button type="button" onClick={() => onChange({ orderType: "market" })} className={`border-b-2 px-2 py-1.5 text-xs ${state.orderType === "market" ? "border-emerald-300 text-slate-100" : "border-transparent text-slate-500"}`}>Market</button>
        <button type="button" onClick={() => onChange({ orderType: "limit" })} className={`border-b-2 px-2 py-1.5 text-xs ${state.orderType === "limit" ? "border-emerald-300 text-slate-100" : "border-transparent text-slate-500"}`}>Limit</button>
      </div>

      {isPerp ? (
        <div className="mb-2 grid shrink-0 grid-cols-2 gap-1">
          <button type="button" onClick={() => onChange({ positionAction: "open" })} className={`rounded px-2 py-1.5 text-[10px] ${state.positionAction === "open" ? "bg-cyan-400/15 text-cyan-100" : "bg-white/5 text-slate-500"}`} title="选择开仓模式；点击底部提交按钮下单">开仓模式</button>
          <button type="button" onClick={() => onChange({ positionAction: "close" })} className={`rounded px-2 py-1.5 text-[10px] ${state.positionAction === "close" ? "bg-amber-400/15 text-amber-100" : "bg-white/5 text-slate-500"}`} title="选择仅减仓模式；点击底部提交按钮下单">平仓模式</button>
        </div>
      ) : null}

      {state.orderType === "limit" ? (
        <label className="mb-1.5 block shrink-0">
          <span className="text-[10px] text-slate-500">Price <span className="float-right font-mono text-slate-600">{bestAsk ? `Ask ${fmt(bestAsk, priceDigits)}` : "-"}</span></span>
          <input value={state.price} onChange={(event) => onChange({ price: event.target.value })} className="paper-input mt-0.5 px-2.5 py-2 text-sm font-mono tabular-nums" inputMode="decimal" placeholder={bestAsk ? fmt(bestAsk, priceDigits) : "-"} autoComplete="off" aria-label="Limit price" />
        </label>
      ) : null}

      <label className="mb-1.5 block shrink-0">
        <span className="text-[10px] text-slate-500">Size <span className="float-right font-mono text-slate-500">{baseAsset}</span></span>
        <input value={state.quantity} onChange={(event) => { setQuickPercent(0); onChange({ quantity: event.target.value }); }} className="paper-input mt-0.5 px-2.5 py-2 text-sm font-mono tabular-nums" inputMode="decimal" placeholder="-" autoComplete="off" aria-label="Order size" />
      </label>

      <div className="mb-2 shrink-0 rounded border border-white/8 bg-white/[0.025] px-2 py-1.5">
        <div className="flex items-center justify-between text-[10px] text-slate-500"><span>Available to Trade</span><span className="font-mono text-slate-300">{availableText}</span></div>
        <input type="range" min="0" max="100" step="1" value={quickPercent} onChange={(event) => setPercent(Number(event.target.value))} className="mt-1 h-1 w-full accent-emerald-300" aria-label="Order size percentage" />
        <div className="mt-1 grid grid-cols-5 gap-1 text-[9px] text-slate-500">
          {[0, 25, 50, 75, 100].map((percent) => <button key={percent} type="button" onClick={() => setPercent(percent)} className={`rounded py-1 ${quickPercent === percent ? "bg-emerald-400/15 text-emerald-200" : "hover:bg-white/8"}`}>{percent}%</button>)}
        </div>
      </div>

      {isPerp ? (
        <div className="mb-2 shrink-0 border-b border-white/8 pb-2">
          <div className="flex items-center justify-between text-[10px] text-slate-500"><span>Leverage</span><span>Max {maxLeverage ?? "-"}x</span></div>
          <div className="mt-1 flex flex-wrap gap-1">
            {marketLeverageOptions({ max_leverage: maxLeverage ?? "20" }).map((value) => <button key={value} type="button" onClick={() => onChange({ leverage: String(value) })} className={`rounded px-2 py-0.5 text-[10px] ${Number(state.leverage) === value ? "bg-cyan-400/15 text-cyan-100" : "bg-white/6 text-slate-400 hover:bg-white/10"}`}>{value}x</button>)}
          </div>
        </div>
      ) : null}

      {state.orderType === "limit" ? (
        <div className="mb-2 grid shrink-0 grid-cols-3 gap-1">
          {(["gtc", "ioc", "post_only"] as const).map((tif) => <button key={tif} type="button" onClick={() => onChange({ tif })} className={`rounded px-1 py-1.5 text-[10px] ${state.tif === tif ? "bg-cyan-400/15 text-cyan-100" : "bg-white/5 text-slate-500 hover:bg-white/10"}`}>{tif === "gtc" ? "GTC" : tif === "ioc" ? "IOC" : "Post-only"}</button>)}
        </div>
      ) : <div className="mb-2 shrink-0 rounded bg-white/5 px-2 py-1.5 text-[10px] text-slate-500">Market orders use IOC execution</div>}

      <div className="mb-2 shrink-0 space-y-1 border-y border-white/8 py-2 text-[10px] text-slate-400">
        <InfoRow label="Reference Price" value={referencePrice > 0 ? fmt(referencePrice, priceDigits) : "-"} />
        <InfoRow label="Order Value" value={amount > 0 ? `${fmt(amount, 2)} USDT` : "-"} />
        {isPerp ? <InfoRow label="Margin Required" value={estimatedMargin > 0 ? `${fmt(estimatedMargin, 2)} USDT` : "-"} /> : null}
        <InfoRow label="Fees" value={market ? `${(Number(market.maker_fee_rate ?? 0) * 100).toFixed(4)}% / ${takerFee.toFixed(4)}%` : "-"} />
        <InfoRow label="Est. Fee" value={estimatedFee > 0 ? `${fmt(estimatedFee, 4)} USDT` : "-"} />
      </div>

      </div>

      {statusBlocked ? <div className="mb-2 shrink-0 rounded bg-rose-400/10 px-2 py-1.5 text-[10px] text-rose-200">Market status: {marketStatus}. New orders are disabled.</div> : reduceOnly ? <div className="mb-2 shrink-0 rounded bg-amber-400/10 px-2 py-1.5 text-[10px] text-amber-100">Reduce-only market: close positions only.</div> : null}
      {validation && !submitting ? <div className="mb-2 shrink-0"><StatusPill tone={validation.tone}>{validation.text}</StatusPill></div> : null}
      {submitError ? <div className="mb-2 shrink-0 rounded bg-rose-400/10 px-2 py-1.5 text-[10px] text-rose-200">{submitError}</div> : null}
      {submitNotice ? <div className="mb-2 shrink-0 rounded bg-emerald-400/10 px-2 py-1.5 text-[10px] text-emerald-200">{submitNotice}</div> : null}

      <button type="submit" disabled={!canSubmit} className={`hl-submit mt-auto w-full shrink-0 rounded-xl py-2.5 text-sm font-semibold text-slate-950 transition disabled:cursor-not-allowed disabled:opacity-40 ${buyActive ? "bg-emerald-300 hover:bg-emerald-200" : "bg-rose-300 hover:bg-rose-200"}`}>{submitLabel}</button>
    </form>
  );
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return <div className="flex items-center justify-between gap-2"><span>{label}</span><span className="truncate font-mono text-slate-200">{value}</span></div>;
}
