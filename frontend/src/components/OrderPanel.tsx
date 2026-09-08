import { useMemo } from "react";
import { fmt } from "../lib/format";
import { marketLeverageOptions, marketStatusLabel } from "../lib/paper";
import type { BalanceItem, PaperMarket, PaperPerpAccount } from "../types";
import { StatusPill, type Tone } from "./ui";

export type OrderSide = "buy" | "sell";
export type OrderType = "limit" | "market";
export type PaperTif = "gtc" | "ioc" | "post_only";

const TIF_OPTIONS: { key: PaperTif; label: string; hint: string }[] = [
  { key: "gtc", label: "GTC", hint: "一直有效直至成交或撤单" },
  { key: "ioc", label: "IOC", hint: "无法立即全部成交的部分将取消" },
  { key: "post_only", label: "Post-only", hint: "仅挂单，会立即成交则拒绝" },
];

export function OrderPanel({
  market,
  perpAccount,
  balances,
  side,
  setSide,
  orderType,
  setOrderType,
  tif,
  setTif,
  quantity,
  setQuantity,
  price,
  setPrice,
  positionAction,
  setPositionAction,
  leverage,
  setLeverage,
  latestPrice,
  bestBid,
  bestAsk,
  priceDigits,
  quantityDigits,
  busy,
  error,
  notice,
  canSubmit,
  status,
  closeSideHint,
  onApplyPercent,
  onSubmit,
}: {
  market?: PaperMarket;
  perpAccount?: PaperPerpAccount;
  balances: BalanceItem[];
  side: OrderSide;
  setSide: (value: OrderSide) => void;
  orderType: OrderType;
  setOrderType: (value: OrderType) => void;
  tif: PaperTif;
  setTif: (value: PaperTif) => void;
  quantity: string;
  setQuantity: (value: string) => void;
  price: string;
  setPrice: (value: string) => void;
  positionAction: "open" | "close";
  setPositionAction: (value: "open" | "close") => void;
  leverage: string;
  setLeverage: (value: string) => void;
  latestPrice: string;
  bestBid: string;
  bestAsk: string;
  priceDigits: number;
  quantityDigits: number;
  busy: boolean;
  error: string;
  notice: string;
  canSubmit: boolean;
  status: string;
  closeSideHint?: string;
  onApplyPercent: (value: number) => void;
  onSubmit: () => void;
}) {
  const isPerp = market?.product_type === "PERP";
  const balanceMap = useMemo(() => Object.fromEntries(balances.map((item) => [item.asset, item])), [balances]);
  const referencePrice = Number(latestPrice || 0) || Number(market?.reference_price ?? 0);
  const limitPrice = Number(price || 0);
  const effectivePrice = orderType === "limit" && limitPrice > 0 ? limitPrice : referencePrice;
  const quantityNumber = Number(quantity || 0);
  const amount = quantityNumber * effectivePrice;
  const makerFee = Number(market?.maker_fee_rate ?? "0") * 100;
  const takerFee = Number(market?.taker_fee_rate ?? "0") * 100;
  const estimatedFee = amount * (Number(market?.taker_fee_rate ?? "0"));
  const quoteAsset = market?.quote_asset ?? "USDT";
  const baseAsset = market?.base_asset ?? "";
  const availableQuote = Number(balanceMap[quoteAsset]?.available ?? 0);
  const availableBase = Number(balanceMap[baseAsset]?.available ?? 0);
  const leverageNumber = Math.max(1, Number(leverage || 1));
  const maxLeverage = Math.max(1, Number(market?.max_leverage ?? 20));
  const estimatedMargin = isPerp ? amount / leverageNumber : 0;
  const minNotional = Number(market?.min_notional ?? 5);
  const priceProtectionPct = Number(market?.price_protection_pct ?? 0.05);
  const protectionLow = referencePrice * (1 - priceProtectionPct);
  const protectionHigh = referencePrice * (1 + priceProtectionPct);
  const protectionViolated = orderType === "limit" && limitPrice > 0 && (limitPrice < protectionLow || limitPrice > protectionHigh);
  const belowMinNotional = quantityNumber > 0 && amount > 0 && amount < minNotional;
  const insufficientQuote = !isPerp && side === "buy" && amount > availableQuote;
  const insufficientBase = !isPerp && side === "sell" && quantityNumber > availableBase;
  const insufficientMargin = isPerp && positionAction === "open" && amount / leverageNumber > Number(perpAccount?.available_margin ?? 0);
  const availableLabel = isPerp
    ? `可用保证金 ${fmt(perpAccount?.available_margin, 2)} USDT`
    : side === "buy"
      ? `可用 ${quoteAsset} ${fmt(availableQuote, 2)}`
      : `可用 ${baseAsset} ${fmt(availableBase, quantityDigits)}`;

  const validation = useMemo(() => {
    if (quantityNumber <= 0) return { tone: "warn" as Tone, text: "请输入有效数量" };
    if (orderType === "limit" && limitPrice <= 0) return { tone: "warn" as Tone, text: "限价单必须填写价格" };
    if (protectionViolated) return { tone: "warn" as Tone, text: `价格超出保护带（±${(priceProtectionPct * 100).toFixed(2)}%），可能被拒` };
    if (belowMinNotional) return { tone: "warn" as Tone, text: `订单金额低于最小名义金额 ${fmt(minNotional, 2)} USDT` };
    if (insufficientQuote) return { tone: "sell" as Tone, text: "可用 USDT 不足" };
    if (insufficientBase) return { tone: "sell" as Tone, text: `可用 ${baseAsset} 不足` };
    if (insufficientMargin) return { tone: "sell" as Tone, text: "可用保证金不足" };
    return null;
  }, [baseAsset, belowMinNotional, insufficientBase, insufficientMargin, insufficientQuote, limitPrice, orderType, priceProtectionPct, protectionViolated, quantityNumber]);

  const sideTone = side === "buy" ? "buy" : "sell";
  const submitLabel = busy
    ? "提交中..."
    : isPerp
      ? `${positionAction === "open" ? "开仓" : "平仓"} ${side === "buy" ? "买入" : "卖出"}`
      : `${side === "buy" ? "买入" : "卖出"} ${market?.symbol ?? ""}`;

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
      className="panel flex h-full min-h-0 flex-col rounded-2xl p-4"
    >
      <div className="flex items-center justify-between">
        <h2 className="font-display text-sm text-slate-100">下单面板</h2>
        <span className="text-[11px] text-slate-500">
          {market?.symbol ?? "—"} · {isPerp ? "USDT 永续" : "现货"}
        </span>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-2">
        <button
          type="button"
          onClick={() => setSide("buy")}
          className={`rounded-xl py-2.5 text-sm font-semibold transition ${side === "buy" ? "bg-emerald-400 text-slate-950" : "bg-white/5 text-slate-400 hover:bg-white/10"}`}
        >
          买入
        </button>
        <button
          type="button"
          onClick={() => setSide("sell")}
          className={`rounded-xl py-2.5 text-sm font-semibold transition ${side === "sell" ? "bg-rose-400 text-slate-950" : "bg-white/5 text-slate-400 hover:bg-white/10"}`}
        >
          卖出
        </button>
      </div>

      <div className="mt-2 grid grid-cols-2 gap-2">
        <button
          type="button"
          onClick={() => setOrderType("limit")}
          className={`rounded-xl px-3 py-2 text-xs ${orderType === "limit" ? "bg-cyan-400/15 text-cyan-100" : "bg-white/5 text-slate-400 hover:bg-white/10"}`}
        >
          限价
        </button>
        <button
          type="button"
          onClick={() => setOrderType("market")}
          className={`rounded-xl px-3 py-2 text-xs ${orderType === "market" ? "bg-cyan-400/15 text-cyan-100" : "bg-white/5 text-slate-400 hover:bg-white/10"}`}
        >
          市价
        </button>
      </div>

      {isPerp ? (
        <div className="mt-2 grid grid-cols-2 gap-2">
          <button
            type="button"
            onClick={() => setPositionAction("open")}
            className={`rounded-xl px-3 py-2 text-xs ${positionAction === "open" ? "bg-cyan-400/15 text-cyan-100" : "bg-white/5 text-slate-400 hover:bg-white/10"}`}
          >
            开仓
          </button>
          <button
            type="button"
            onClick={() => setPositionAction("close")}
            className={`rounded-xl px-3 py-2 text-xs ${positionAction === "close" ? "bg-amber-400/15 text-amber-100" : "bg-white/5 text-slate-400 hover:bg-white/10"}`}
          >
            平仓{closeSideHint ? ` · ${closeSideHint}` : ""}
          </button>
        </div>
      ) : null}

      {orderType === "limit" ? (
        <label className="mt-3 block">
          <span className="text-[11px] text-slate-400">价格（点击盘口可填入）</span>
          <input
            value={price}
            onChange={(event) => setPrice(event.target.value)}
            className="paper-input mt-1 font-mono tabular-nums"
            inputMode="decimal"
            placeholder={bestAsk ? `卖一 ${bestAsk}` : "0.00"}
            autoComplete="off"
          />
        </label>
      ) : (
        <div className="mt-3 flex items-center justify-between rounded-xl bg-white/5 px-3 py-2 text-[11px] text-slate-400">
          <span>市价单使用保护价，最大滑点约 {(priceProtectionPct * 100).toFixed(2)}%</span>
        </div>
      )}

      <label className="mt-2 block">
        <span className="text-[11px] text-slate-400">数量（{baseAsset}）</span>
        <input
          value={quantity}
          onChange={(event) => setQuantity(event.target.value)}
          className="paper-input mt-1 font-mono tabular-nums"
          inputMode="decimal"
          placeholder={`最小 ${market?.min_qty ?? "0"}`}
          autoComplete="off"
        />
      </label>

      <div className="mt-2 grid grid-cols-4 gap-2">
        {[25, 50, 75, 100].map((item) => (
          <button key={item} type="button" onClick={() => onApplyPercent(item)} className="rounded-xl bg-white/6 px-2 py-2 text-[11px] text-slate-300 hover:bg-white/12">
            {item}%
          </button>
        ))}
      </div>

      {isPerp ? (
        <div className="mt-2">
          <div className="flex items-center justify-between">
            <span className="text-[11px] text-slate-400">杠杆</span>
            <span className="text-[11px] text-slate-500">最大 {maxLeverage}x</span>
          </div>
          <div className="mt-1 flex flex-wrap gap-1.5">
            {marketLeverageOptions(market).map((value) => (
              <button
                key={value}
                type="button"
                onClick={() => setLeverage(String(value))}
                className={`rounded-lg px-2.5 py-1 text-[11px] ${Number(leverage) === value ? "bg-cyan-400/15 text-cyan-100" : "bg-white/6 text-slate-400 hover:bg-white/10"}`}
              >
                {value}x
              </button>
            ))}
          </div>
        </div>
      ) : null}

      {orderType === "limit" ? (
        <div className="mt-2 grid grid-cols-3 gap-2">
          {TIF_OPTIONS.map((item) => (
            <button
              key={item.key}
              type="button"
              title={item.hint}
              onClick={() => setTif(item.key)}
              className={`rounded-xl px-2 py-2 text-[11px] ${tif === item.key ? "bg-cyan-400/15 text-cyan-100" : "bg-white/5 text-slate-500 hover:bg-white/10"}`}
            >
              {item.label}
            </button>
          ))}
        </div>
      ) : (
        <div className="mt-2 rounded-xl bg-white/5 px-3 py-2 text-[11px] text-slate-500">市价单按 IOC 执行</div>
      )}

      <div className="mt-3 space-y-1.5 rounded-xl bg-white/5 px-3 py-2.5 text-[11px] text-slate-400">
        <div className="flex justify-between">
          <span>参考价格</span>
          <span className="font-mono text-slate-200">{fmt(referencePrice, priceDigits)}</span>
        </div>
        {orderType === "limit" ? (
          <div className="flex justify-between">
            <span>买一 / 卖一</span>
            <span className="font-mono text-slate-200">
              {fmt(bestBid, priceDigits)} / {fmt(bestAsk, priceDigits)}
            </span>
          </div>
        ) : null}
        <div className="flex justify-between">
          <span>预计金额</span>
          <span className="font-mono text-slate-200">{fmt(amount, 2)} USDT</span>
        </div>
        {isPerp ? (
          <div className="flex justify-between">
            <span>预计保证金</span>
            <span className="font-mono text-slate-200">{fmt(estimatedMargin, 2)} USDT</span>
          </div>
        ) : null}
        <div className="flex justify-between">
          <span>预计手续费（taker {takerFee.toFixed(4)}%）</span>
          <span className="font-mono text-slate-200">{fmt(estimatedFee, 4)} USDT</span>
        </div>
        <div className="flex justify-between">
          <span>可用</span>
          <span className="truncate pl-2 font-mono text-slate-300">{availableLabel}</span>
        </div>
      </div>

      {isPerp ? (
        <div className="mt-2 rounded-xl bg-white/4 px-3 py-2 text-[11px] leading-5 text-slate-500">
          单向逐仓 · 开仓使用 {leverageNumber}x 杠杆 · {positionAction === "close" ? "平仓单为 reduce-only，不会反向开仓" : `保证金约 ${fmt(estimatedMargin, 2)} USDT`}
        </div>
      ) : null}

      {status !== "TRADING" ? (
        <div className={`mt-2 rounded-xl px-3 py-2 text-[11px] ${status === "REDUCE_ONLY" ? "bg-amber-400/10 text-amber-100" : "bg-rose-400/10 text-rose-200"}`}>
          {status === "REDUCE_ONLY" ? "当前市场只允许平仓（reduce-only）" : `当前市场状态：${marketStatusLabel(status)}，暂不接受新订单`}
        </div>
      ) : null}

      {validation && !busy ? (
        <div className="mt-2">
          <StatusPill tone={validation.tone}>{validation.text}</StatusPill>
        </div>
      ) : null}
      {error ? <div className="mt-2 rounded-xl bg-rose-400/10 px-3 py-2 text-[11px] text-rose-200">{error}</div> : null}
      {notice ? <div className="mt-2 rounded-xl bg-emerald-400/10 px-3 py-2 text-[11px] text-emerald-200">{notice}</div> : null}

      <button
        type="submit"
        disabled={busy || !canSubmit}
        className={`mt-3 w-full rounded-2xl py-3 text-sm font-semibold text-slate-950 transition disabled:cursor-not-allowed disabled:opacity-40 ${sideTone === "buy" ? "bg-emerald-300 hover:bg-emerald-200" : "bg-rose-300 hover:bg-rose-200"}`}
      >
        {submitLabel}
      </button>
    </form>
  );
}
