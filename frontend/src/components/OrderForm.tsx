import { useEffect, useState } from "react";
import { api } from "../api/client";
import { config } from "../lib/config";
import { fmt } from "../lib/format";
import { useAppStore } from "../store/useAppStore";
import type { BalanceItem, MarketTicker } from "../types";

type FormType = "limit" | "market" | "market_protected";

const pctButtons = [25, 50, 75, 100];

export function OrderForm({
  symbol,
  ticker,
  balances,
  selectedPrice,
  priceDigits,
  quantityDigits,
  onSubmitted,
}: {
  symbol: string;
  ticker?: MarketTicker;
  balances: BalanceItem[];
  selectedPrice?: string;
  priceDigits: number;
  quantityDigits: number;
  onSubmitted: () => Promise<void>;
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
      const response = await api.post<{ order?: { status?: string; reject_reason?: string | null } }>("/orders", payload, config.manualApiKey);
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

  return (
    <section className="panel rounded-3xl p-4">
      <div className="mb-4 flex items-center justify-between">
        <h3 className="font-display text-lg">下单面板</h3>
        <span className="text-xs text-slate-500">{symbol}</span>
      </div>
      <div className="mb-3 rounded-3xl border border-white/8 bg-slate-950/35 p-1.5">
        <div className="grid grid-cols-2 gap-2">
        {(["buy", "sell"] as const).map((item) => (
          <button
            key={item}
            onClick={() => setSide(item)}
            className={`relative rounded-2xl border px-4 py-3 text-sm font-semibold transition ${
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
      <div className="mb-4 rounded-3xl border border-white/8 bg-slate-950/35 p-1.5">
        <div className="grid grid-cols-3 gap-2 text-sm">
        {[
          ["limit", "Limit"],
          ["market", "Market"],
          ["market_protected", "Protected"],
        ].map(([value, label]) => (
          <button
            key={value}
            onClick={() => setType(value as FormType)}
            className={`rounded-2xl border px-3 py-2.5 font-medium transition ${
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
            <input value={price} onChange={(event) => setPrice(event.target.value)} className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-3 outline-none" />
          </label>
        )}
        <label className="block">
          <span className="mb-1 block text-sm text-slate-400">数量</span>
          <input value={quantity} onChange={(event) => setQuantity(event.target.value)} className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-3 outline-none" />
        </label>
        {type === "market_protected" && (
          <label className="block">
            <span className="mb-1 block text-sm text-slate-400">protection_bps</span>
            <input value={protectionBps} onChange={(event) => setProtectionBps(event.target.value)} className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-3 outline-none" />
          </label>
        )}
      </div>
      <div className="mt-3 grid grid-cols-4 gap-2">
        {pctButtons.map((value) => (
          <button key={value} onClick={() => applyPercent(value)} className="rounded-xl bg-white/5 px-3 py-2 text-xs text-slate-300 hover:bg-white/10">
            {value}%
          </button>
        ))}
      </div>
      <div className="mt-4 rounded-2xl bg-white/5 px-3 py-3 text-sm text-slate-300">
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
      <button
        onClick={submit}
        className={`mt-4 w-full rounded-2xl px-4 py-3 text-sm font-semibold ${side === "buy" ? "bg-emerald-500 text-slate-950" : "bg-rose-500 text-white"}`}
      >
        {type === "market_protected" ? "提交保护价市价单" : type === "market" ? "提交真实市价单" : "提交限价单"}
      </button>
      {type === "market_protected" && <p className="mt-2 text-xs text-amber-300">剩余未成交部分会立即取消，不会挂盘。</p>}
    </section>
  );
}
