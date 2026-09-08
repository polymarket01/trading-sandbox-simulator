import { useEffect, useState } from "react";
import { api } from "../api/client";
import { fmt } from "../lib/format";
import { useAppStore } from "../store/useAppStore";
import type { BalanceItem, Level, MarketTicker } from "../types";

type FormType = "limit" | "market" | "market_protected";

const pctButtons = [25, 50, 75, 100];

type ImpactEstimate = {
  filledQuantity: number;
  remainingQuantity: number;
  quoteAmount: number;
  avgPrice: number;
  worstPrice: number;
  slippageBps: number;
  levelsUsed: number;
  status: "empty" | "passive" | "partial" | "filled";
};

function estimateImpact({
  side,
  type,
  quantity,
  price,
  boundPrice,
  referencePrice,
  bids,
  asks,
}: {
  side: "buy" | "sell";
  type: FormType;
  quantity: string;
  price: string;
  boundPrice: number;
  referencePrice: number;
  bids: Level[];
  asks: Level[];
}): ImpactEstimate {
  const requested = Number(quantity || 0);
  if (!requested || requested <= 0) {
    return {
      filledQuantity: 0,
      remainingQuantity: 0,
      quoteAmount: 0,
      avgPrice: 0,
      worstPrice: 0,
      slippageBps: 0,
      levelsUsed: 0,
      status: "empty",
    };
  }

  const limitPrice = type === "limit" ? Number(price || 0) : type === "market_protected" ? boundPrice : 0;
  const hasPriceLimit = type === "limit" || type === "market_protected";
  if (hasPriceLimit && (!limitPrice || limitPrice <= 0)) {
    return {
      filledQuantity: 0,
      remainingQuantity: requested,
      quoteAmount: 0,
      avgPrice: 0,
      worstPrice: 0,
      slippageBps: 0,
      levelsUsed: 0,
      status: "passive",
    };
  }

  const levels = side === "buy" ? asks : bids;
  let filled = 0;
  let quote = 0;
  let worst = 0;
  let levelsUsed = 0;

  for (const [levelPriceText, levelQuantityText] of levels) {
    const levelPrice = Number(levelPriceText);
    const levelQuantity = Number(levelQuantityText);
    if (!levelPrice || !levelQuantity || levelQuantity <= 0) continue;
    if (hasPriceLimit) {
      if (side === "buy" && levelPrice > limitPrice) break;
      if (side === "sell" && levelPrice < limitPrice) break;
    }
    const take = Math.min(levelQuantity, requested - filled);
    if (take <= 0) break;
    filled += take;
    quote += take * levelPrice;
    worst = levelPrice;
    levelsUsed += 1;
    if (filled >= requested) break;
  }

  const remaining = Math.max(0, requested - filled);
  const avg = filled > 0 ? quote / filled : 0;
  const slippageBps =
    filled > 0 && referencePrice > 0
      ? side === "buy"
        ? ((avg - referencePrice) / referencePrice) * 10000
        : ((referencePrice - avg) / referencePrice) * 10000
      : 0;

  const passive = type === "limit" && filled === 0;
  return {
    filledQuantity: filled,
    remainingQuantity: remaining,
    quoteAmount: quote,
    avgPrice: avg,
    worstPrice: worst,
    slippageBps,
    levelsUsed,
    status: filled <= 0 ? (passive ? "passive" : "empty") : remaining > 0 ? "partial" : "filled",
  };
}

export function OrderForm({
  symbol,
  ticker,
  balances,
  bids,
  asks,
  selectedPrice,
  priceDigits,
  quantityDigits,
  apiKey,
  onSubmitted,
  compact = false,
  className = "",
}: {
  symbol: string;
  ticker?: MarketTicker;
  balances: BalanceItem[];
  bids: Level[];
  asks: Level[];
  selectedPrice?: string;
  priceDigits: number;
  quantityDigits: number;
  apiKey: string;
  onSubmitted: () => Promise<void>;
  compact?: boolean;
  className?: string;
}) {
  const pushToast = useAppStore((state) => state.pushToast);
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [type, setType] = useState<FormType>("limit");
  const [price, setPrice] = useState("");
  const [quantity, setQuantity] = useState("");
  const [protectionBps, setProtectionBps] = useState("50");
  const quoteAsset = "USDT";
  const baseAsset = symbol.replace("USDT", "");
  const balanceMap = Object.fromEntries(balances.map((item) => [item.asset, item]));

  useEffect(() => {
    if (selectedPrice && type === "limit") setPrice(selectedPrice);
  }, [selectedPrice, type]);

  const referencePrice = Number(ticker?.mid_price ?? 0);
  const boundPrice =
    type === "market_protected"
      ? side === "buy"
        ? referencePrice * (1 + Number(protectionBps || 0) / 10000)
        : referencePrice * (1 - Number(protectionBps || 0) / 10000)
      : referencePrice;
  const impact = estimateImpact({ side, type, quantity, price, boundPrice, referencePrice, bids, asks });
  const impactStatusLabel =
    impact.status === "filled"
      ? "预计全成"
      : impact.status === "partial"
        ? "预计部分成交"
        : impact.status === "passive"
          ? "预计挂盘"
          : "暂无可成交量";
  const impactStatusClass =
    impact.status === "filled"
      ? "bg-emerald-400/15 text-emerald-100"
      : impact.status === "partial"
        ? "bg-amber-400/16 text-amber-100"
        : impact.status === "passive"
          ? "bg-cyan-400/16 text-cyan-100"
          : "bg-rose-500/16 text-rose-100";

  const applyPercent = (value: number) => {
    if (!referencePrice) return;
    if (side === "buy") {
      const available = Number(balanceMap[quoteAsset]?.available ?? 0);
      const qty = (available * (value / 100)) / referencePrice;
      setQuantity(qty.toFixed(quantityDigits));
    } else {
      const available = Number(balanceMap[baseAsset]?.available ?? 0);
      setQuantity((available * value / 100).toFixed(quantityDigits));
    }
  };

  const submit = async () => {
    try {
      const payload: Record<string, unknown> = {
        symbol,
        side,
        type,
        tif: type === "limit" ? "gtc" : "ioc",
        quantity,
        client_order_id: `ui-${Date.now()}`,
      };
      if (type === "limit") payload.price = price;
      if (type === "market_protected") payload.protection_bps = Number(protectionBps);
      const response = await api.post<{ order?: { status?: string; reject_reason?: string | null } }>("/orders", payload, apiKey);
      if (response.order?.status === "rejected") {
        pushToast("error", response.order.reject_reason ?? "订单被拒绝");
        await onSubmitted();
        return;
      }
      pushToast("success", `${side === "buy" ? "买入" : "卖出"}订单已提交`);
      await onSubmitted();
    } catch (error) {
      pushToast("error", error instanceof Error ? error.message : "下单失败");
    }
  };

  const panelRadius = compact ? "rounded-2xl" : "rounded-3xl";
  const panelPadding = compact ? "p-3" : "p-4";
  const controlRadius = compact ? "rounded-xl" : "rounded-2xl";
  const inputPadding = compact ? "px-3 py-2.5" : "px-3 py-3";
  const blockGap = compact ? "mb-3" : "mb-4";

  return (
    <section className={`panel ${panelRadius} ${panelPadding} ${className}`}>
      <div className={`${blockGap} flex items-center justify-between`}>
        <h3 className={`font-display ${compact ? "text-base" : "text-lg"}`}>下单面板</h3>
        <span className="text-xs text-slate-500">{symbol}</span>
      </div>
      <div className={`mb-3 ${panelRadius} border border-white/8 bg-slate-950/35 p-1.5`}>
        <div className="grid grid-cols-2 gap-2">
        {(["buy", "sell"] as const).map((item) => (
          <button
            type="button"
            key={item}
            onClick={() => setSide(item)}
            className={`relative ${controlRadius} border px-4 ${compact ? "py-2.5" : "py-3"} text-sm font-semibold transition ${
              side === item
                ? item === "buy"
                  ? "border-emerald-300/35 bg-emerald-500/24 text-emerald-50 shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_0_0_1px_rgba(16,185,129,0.15)]"
                  : "border-rose-300/35 bg-rose-500/24 text-rose-50 shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_0_0_1px_rgba(244,63,94,0.15)]"
                : "border-transparent bg-white/5 text-slate-300 hover:bg-white/8"
            }`}
          >
            {side === item && (
              <span className={`absolute inset-y-2 left-2 w-1 rounded-full ${item === "buy" ? "bg-emerald-200/85" : "bg-rose-200/85"}`} />
            )}
            {item === "buy" ? "买入" : "卖出"}
          </button>
        ))}
        </div>
      </div>
      <div className={`${blockGap} ${panelRadius} border border-white/8 bg-slate-950/35 p-1.5`}>
        <div className="grid grid-cols-3 gap-2 text-sm">
        {[
          ["limit", "Limit"],
          ["market", "Market"],
          ["market_protected", "Protected"],
        ].map(([value, label]) => (
          <button
            type="button"
            key={value}
            onClick={() => setType(value as FormType)}
            className={`${controlRadius} border px-3 ${compact ? "py-2" : "py-2.5"} font-medium transition ${
              type === value
                ? "border-cyan-300/35 bg-cyan-400/18 text-cyan-50 shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_0_0_1px_rgba(34,211,238,0.14)]"
                : "border-transparent bg-transparent text-slate-300 hover:bg-white/6"
            }`}
          >
            {label}
          </button>
        ))}
        </div>
      </div>
      <div className="space-y-3">
        {type === "limit" && (
          <label className="block">
            <span className="mb-1 block text-sm text-slate-400">价格</span>
            <input value={price} onChange={(event) => setPrice(event.target.value)} className={`w-full ${controlRadius} border border-white/10 bg-white/5 ${inputPadding} outline-none`} />
          </label>
        )}
        <label className="block">
          <span className="mb-1 block text-sm text-slate-400">数量</span>
          <input value={quantity} onChange={(event) => setQuantity(event.target.value)} className={`w-full ${controlRadius} border border-white/10 bg-white/5 ${inputPadding} outline-none`} />
        </label>
        {type === "market_protected" && (
          <label className="block">
            <span className="mb-1 block text-sm text-slate-400">protection_bps</span>
            <input value={protectionBps} onChange={(event) => setProtectionBps(event.target.value)} className={`w-full ${controlRadius} border border-white/10 bg-white/5 ${inputPadding} outline-none`} />
          </label>
        )}
      </div>
      <div className="mt-3 grid grid-cols-4 gap-2">
        {pctButtons.map((value) => (
          <button type="button" key={value} onClick={() => applyPercent(value)} className="rounded-xl bg-white/5 px-3 py-2 text-xs text-slate-300 hover:bg-white/10">
            {value}%
          </button>
        ))}
      </div>
      <div className={`${compact ? "mt-3" : "mt-4"} ${controlRadius} bg-white/5 px-3 py-3 text-sm text-slate-300`}>
        <div className="flex items-center justify-between">
          <span>参考价</span>
          <span>{fmt(referencePrice, priceDigits)}</span>
        </div>
        <div className="mt-2 flex items-center justify-between">
          <span>{side === "buy" ? "最大成交价预估" : "最小成交价预估"}</span>
          <span>{fmt(boundPrice, priceDigits)}</span>
        </div>
        <div className="mt-2 flex items-center justify-between">
          <span>金额</span>
          <span>{fmt(Number(quantity || 0) * Number(type === "limit" ? price || referencePrice : referencePrice), 2)} USDT</span>
        </div>
      </div>
      <div className={`mt-3 ${controlRadius} border border-white/10 bg-slate-950/30 px-3 py-3 text-sm text-slate-300`}>
        <div className="mb-3 flex items-center justify-between gap-2">
          <span className="text-xs uppercase tracking-[0.18em] text-slate-500">即时成交预估</span>
          <span className={`shrink-0 rounded-full px-2.5 py-1 text-xs ${impactStatusClass}`}>{impactStatusLabel}</span>
        </div>
        <div className="grid grid-cols-2 gap-2">
          <ImpactStat label="可成交" value={fmt(impact.filledQuantity, quantityDigits)} />
          <ImpactStat label="剩余" value={fmt(impact.remainingQuantity, quantityDigits)} />
          <ImpactStat label="均价" value={fmt(impact.avgPrice, priceDigits)} />
          <ImpactStat label="最差价" value={fmt(impact.worstPrice, priceDigits)} />
          <ImpactStat label="滑点" value={`${fmt(impact.slippageBps, 2)} bps`} />
          <ImpactStat label="打穿档数" value={String(impact.levelsUsed)} />
          <ImpactStat label="成交额" value={`${fmt(impact.quoteAmount, 2)} USDT`} wide />
        </div>
      </div>
      <button
        type="button"
        onClick={submit}
        className={`${compact ? "mt-3" : "mt-4"} w-full ${controlRadius} px-4 py-3 text-sm font-semibold ${side === "buy" ? "bg-emerald-500 text-slate-950" : "bg-rose-500 text-white"}`}
      >
        {type === "market_protected" ? "提交保护价市价单" : type === "market" ? "提交真实市价单" : "提交限价单"}
      </button>
      {type === "market_protected" && <p className="mt-2 text-xs text-amber-300">剩余未成交部分会立即取消，不会挂盘。</p>}
    </section>
  );
}

function ImpactStat({ label, value, wide = false }: { label: string; value: string; wide?: boolean }) {
  return (
    <div className={`min-w-0 rounded-xl bg-white/5 px-3 py-2 ${wide ? "col-span-2" : ""}`}>
      <div className="truncate text-xs text-slate-500">{label}</div>
      <div className="mt-1 break-words font-mono text-sm text-slate-100">{value}</div>
    </div>
  );
}
