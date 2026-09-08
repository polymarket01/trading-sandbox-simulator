import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { fmt } from "../lib/format";
import { useAppStore } from "../store/useAppStore";
import type { ContractAccount, ContractPriceState, ContractSetting, Level, MarketDefinition, MarketTicker } from "../types";

type ContractIntent = "open_long" | "open_short" | "close_long" | "close_short";
type OrderType = "limit" | "market";

const intentMeta: Record<ContractIntent, { label: string; side: "buy" | "sell"; positionAction: "open" | "close"; reduceOnly: boolean; tone: string }> = {
  open_long: { label: "开多", side: "buy", positionAction: "open", reduceOnly: false, tone: "border-emerald-300/35 bg-emerald-500/22 text-emerald-50" },
  open_short: { label: "开空", side: "sell", positionAction: "open", reduceOnly: false, tone: "border-rose-300/35 bg-rose-500/22 text-rose-50" },
  close_long: { label: "平多", side: "sell", positionAction: "close", reduceOnly: true, tone: "border-cyan-300/35 bg-cyan-400/18 text-cyan-50" },
  close_short: { label: "平空", side: "buy", positionAction: "close", reduceOnly: true, tone: "border-amber-300/35 bg-amber-400/18 text-amber-50" },
};
const tradingModeLabel: Record<string, string> = {
  normal: "正常",
  reduce_only: "只减仓",
  paused: "暂停",
};

function estimateFill(side: "buy" | "sell", type: OrderType, quantity: string, price: string, bids: Level[], asks: Level[]) {
  const requested = Number(quantity || 0);
  if (!requested || requested <= 0) return { filled: 0, quote: 0, avg: 0, status: "等待数量" };
  const limitPrice = Number(price || 0);
  if (type === "limit" && (!limitPrice || limitPrice <= 0)) return { filled: 0, quote: 0, avg: 0, status: "等待价格" };
  const levels = side === "buy" ? asks : bids;
  let filled = 0;
  let quote = 0;
  for (const [levelPriceText, levelQtyText] of levels) {
    const levelPrice = Number(levelPriceText);
    const levelQty = Number(levelQtyText);
    if (!levelPrice || !levelQty) continue;
    if (type === "limit") {
      if (side === "buy" && levelPrice > limitPrice) break;
      if (side === "sell" && levelPrice < limitPrice) break;
    }
    const take = Math.min(levelQty, requested - filled);
    if (take <= 0) break;
    filled += take;
    quote += take * levelPrice;
    if (filled >= requested) break;
  }
  const avg = filled > 0 ? quote / filled : 0;
  const status = filled <= 0 ? "预计挂盘" : filled >= requested ? "预计全成" : "预计部分成交";
  return { filled, quote, avg, status };
}

export function ContractOrderForm({
  symbol,
  market,
  ticker,
  bids,
  asks,
  selectedPrice,
  priceDigits,
  quantityDigits,
  apiKey,
  setting,
  account,
  priceState,
  onSettingChanged,
  onSubmitted,
}: {
  symbol: string;
  market?: MarketDefinition;
  ticker?: MarketTicker;
  bids: Level[];
  asks: Level[];
  selectedPrice?: string;
  priceDigits: number;
  quantityDigits: number;
  apiKey: string;
  setting?: ContractSetting;
  account?: ContractAccount;
  priceState?: ContractPriceState;
  onSettingChanged: (setting: ContractSetting) => void;
  onSubmitted: () => Promise<void>;
}) {
  const pushToast = useAppStore((state) => state.pushToast);
  const [intent, setIntent] = useState<ContractIntent>("open_long");
  const [type, setType] = useState<OrderType>("limit");
  const [price, setPrice] = useState("");
  const [quantity, setQuantity] = useState("");
  const [leverage, setLeverage] = useState(setting?.leverage ?? market?.default_leverage ?? "5");
  const [savingLeverage, setSavingLeverage] = useState(false);
  const meta = intentMeta[intent];
  const tradingMode = market?.contract_trading_mode ?? "normal";
  const intentDisabled = tradingMode === "paused" || (tradingMode === "reduce_only" && meta.positionAction === "open");
  const mark = Number(priceState?.mark_price || ticker?.mid_price || ticker?.last_price || selectedPrice || price || 0);
  const notional = Number(quantity || 0) * (type === "limit" ? Number(price || 0) : mark);
  const initialMargin = Number(leverage || 0) > 0 ? notional / Number(leverage) : 0;
  const availableMargin = Number(account?.available_margin ?? 0);
  const marginRisk = initialMargin > 0 && availableMargin > 0 && initialMargin > availableMargin;
  const impact = useMemo(() => estimateFill(meta.side, type, quantity, price, bids, asks), [asks, bids, meta.side, price, quantity, type]);

  useEffect(() => {
    setLeverage(setting?.leverage ?? market?.default_leverage ?? "5");
  }, [market?.default_leverage, setting?.leverage]);

  useEffect(() => {
    if (selectedPrice && type === "limit") setPrice(selectedPrice);
  }, [selectedPrice, type]);

  useEffect(() => {
    if (tradingMode === "reduce_only" && intentMeta[intent].positionAction === "open") {
      setIntent("close_long");
    }
  }, [intent, tradingMode]);

  const saveLeverage = async () => {
    try {
      setSavingLeverage(true);
      const response = await api.put<ContractSetting>(
        `/contracts/settings/${symbol}`,
        { leverage, margin_mode: setting?.margin_mode ?? "isolated" },
        apiKey,
      );
      onSettingChanged(response);
      pushToast("success", `${symbol} 杠杆已设为 ${response.leverage}x`);
    } catch (error) {
      pushToast("error", error instanceof Error ? error.message : "保存杠杆失败");
    } finally {
      setSavingLeverage(false);
    }
  };

  const submit = async () => {
    if (intentDisabled) {
      pushToast("error", tradingMode === "paused" ? "合约市场已暂停" : "合约市场当前只允许平仓");
      return;
    }
    try {
      const payload: Record<string, unknown> = {
        symbol,
        side: meta.side,
        position_action: meta.positionAction,
        reduce_only: meta.reduceOnly,
        type,
        tif: type === "limit" ? "gtc" : "ioc",
        quantity,
        leverage,
        client_order_id: `perp-ui-${Date.now()}`,
      };
      if (type === "limit") payload.price = price;
      const response = await api.post<{ order?: { status?: string; reject_reason?: string | null } }>("/contracts/orders", payload, apiKey);
      if (response.order?.status === "rejected") {
        pushToast("error", response.order.reject_reason ?? "合约订单被拒绝");
        await onSubmitted();
        return;
      }
      pushToast("success", `${meta.label}订单已提交`);
      setQuantity("");
      await onSubmitted();
    } catch (error) {
      pushToast("error", error instanceof Error ? error.message : "合约下单失败");
    }
  };

  return (
    <section className="panel rounded-2xl p-4">
      <div className="mb-4 flex items-center justify-between gap-3">
        <div>
          <h3 className="font-display text-lg">合约下单</h3>
          <div className="mt-1 text-xs text-slate-500">{symbol} · {market?.margin_asset ?? "USDT"} 本位 · 逐仓</div>
        </div>
        <span className="rounded-lg bg-cyan-400/12 px-2.5 py-1 text-xs text-cyan-100">PERP</span>
      </div>
      <div className="mb-3 grid grid-cols-2 gap-2 text-xs">
        <OrderMetric label="标记价" value={fmt(priceState?.mark_price ?? mark, priceDigits)} />
        <OrderMetric label="可用保证金" value={`${fmt(account?.available_margin, 4)} ${account?.margin_asset ?? market?.margin_asset ?? "USDT"}`} />
        <OrderMetric label="当前杠杆" value={`${setting?.leverage ?? leverage}x`} />
        <OrderMetric label="风控模式" value={tradingModeLabel[tradingMode] ?? tradingMode} />
      </div>
      <div className="mb-3 grid grid-cols-4 gap-2">
        {(Object.keys(intentMeta) as ContractIntent[]).map((item) => (
          <button
            key={item}
            type="button"
            disabled={tradingMode === "paused" || (tradingMode === "reduce_only" && intentMeta[item].positionAction === "open")}
            onClick={() => setIntent(item)}
            className={`rounded-xl border px-2 py-2 text-sm font-semibold transition ${
              intent === item ? intentMeta[item].tone : "border-transparent bg-white/5 text-slate-300 hover:bg-white/8"
            } disabled:cursor-not-allowed disabled:opacity-40`}
          >
            {intentMeta[item].label}
          </button>
        ))}
      </div>
      <div className="mb-3 grid grid-cols-2 gap-2">
        {(["limit", "market"] as const).map((item) => (
          <button
            key={item}
            type="button"
            onClick={() => setType(item)}
            className={`rounded-xl border px-3 py-2 text-sm ${
              type === item ? "border-cyan-300/35 bg-cyan-400/18 text-cyan-50" : "border-transparent bg-white/5 text-slate-300 hover:bg-white/8"
            }`}
          >
            {item === "limit" ? "限价" : "市价"}
          </button>
        ))}
      </div>
      <div className="mb-3 grid gap-2 md:grid-cols-[1fr_auto]">
        <label className="block">
          <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">leverage</span>
          <input
            value={leverage}
            onChange={(event) => setLeverage(event.target.value)}
            className="w-full rounded-xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none"
          />
        </label>
        <button
          type="button"
          disabled={savingLeverage}
          onClick={saveLeverage}
          className="self-end rounded-xl bg-cyan-400/16 px-4 py-2.5 text-sm text-cyan-100 disabled:opacity-50"
        >
          保存杠杆
        </button>
      </div>
      <div className="space-y-3">
        {type === "limit" && (
          <label className="block">
            <span className="mb-1 block text-sm text-slate-400">价格</span>
            <input value={price} onChange={(event) => setPrice(event.target.value)} className="w-full rounded-xl border border-white/10 bg-white/5 px-3 py-3 outline-none" />
          </label>
        )}
        <label className="block">
          <span className="mb-1 block text-sm text-slate-400">数量</span>
          <input value={quantity} onChange={(event) => setQuantity(event.target.value)} className="w-full rounded-xl border border-white/10 bg-white/5 px-3 py-3 outline-none" />
        </label>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
        <div className="rounded-xl bg-white/5 px-3 py-2">
          <div className="text-slate-500">名义价值</div>
          <div className="mt-1 font-mono text-slate-100">{fmt(notional, 4)} {market?.quote_asset ?? "USDT"}</div>
        </div>
        <div className="rounded-xl bg-white/5 px-3 py-2">
          <div className="text-slate-500">初始保证金</div>
          <div className={`mt-1 font-mono ${marginRisk ? "text-rose-300" : "text-slate-100"}`}>{fmt(initialMargin, 4)} {market?.margin_asset ?? "USDT"}</div>
        </div>
        <div className="rounded-xl bg-white/5 px-3 py-2">
          <div className="text-slate-500">撮合预估</div>
          <div className="mt-1 font-mono text-slate-100">{impact.status}</div>
        </div>
        <div className="rounded-xl bg-white/5 px-3 py-2">
          <div className="text-slate-500">均价预估</div>
          <div className="mt-1 font-mono text-slate-100">{fmt(impact.avg, priceDigits)}</div>
        </div>
      </div>
      {marginRisk && (
        <div className="mt-3 rounded-xl border border-rose-500/16 bg-rose-500/8 px-3 py-2 text-xs leading-5 text-rose-100">
          预估初始保证金高于当前可用保证金，提交后可能被风控拒绝。
        </div>
      )}
      <button
        type="button"
        onClick={submit}
        disabled={intentDisabled}
        className={`mt-4 w-full rounded-2xl border px-4 py-3 text-sm font-semibold ${meta.tone} disabled:cursor-not-allowed disabled:opacity-45`}
      >
        提交{meta.label}
      </button>
      <div className="mt-3 text-xs leading-5 text-slate-500">
        双向持仓 · 逐仓保证金 · 风控 {tradingModeLabel[tradingMode] ?? tradingMode}
      </div>
    </section>
  );
}

function OrderMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl bg-white/5 px-3 py-2">
      <div className="text-slate-500">{label}</div>
      <div className="mt-1 truncate font-mono text-slate-100" title={value}>{value}</div>
    </div>
  );
}
