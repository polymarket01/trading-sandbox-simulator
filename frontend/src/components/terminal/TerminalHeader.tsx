import { useLiveMakerSymbols } from "../../hooks/useLiveMakerSymbols";
import { fmt, fmtCompact, fmtPct, signedNumber } from "../../lib/format";
import { marketStatusLabel } from "../../lib/paper";
import type { MarketTicker } from "../../types";
import { ConnectionBadge } from "../ConnectionBadge";
import { FlashPrice, type Tone } from "../ui";
import type { StreamStatus, TerminalMarketInfo } from "./types";

const showNumber = (value: string | number | null | undefined, digits: number) => {
  if (value === undefined || value === null || value === "" || !Number.isFinite(Number(value))) return "-";
  return fmt(value, digits);
};

const showPct = (value: string | number | null | undefined, digits: number) => {
  if (value === undefined || value === null || value === "" || !Number.isFinite(Number(value))) return "-";
  return fmtPct(value, digits);
};

const countdown = (timestamp?: number | null) => {
  if (!timestamp) return "-";
  const seconds = Math.max(0, Math.round((timestamp - Date.now()) / 1000));
  const hours = Math.floor(seconds / 3600).toString().padStart(2, "0");
  const minutes = Math.floor((seconds % 3600) / 60).toString().padStart(2, "0");
  const rest = (seconds % 60).toString().padStart(2, "0");
  return `${hours}:${minutes}:${rest}`;
};

export function TerminalHeader({
  symbol,
  markets,
  currentMarket,
  isPerp,
  ticker,
  priceDigits,
  bestBid,
  bestAsk,
  spreadPct,
  marketStatus,
  fundingState,
  maxLeverage,
  publicStatus,
  privateStatus,
  tickerUpdatedAt,
  onSelectSymbol,
  extraRight,
}: {
  symbol: string;
  markets: TerminalMarketInfo[];
  currentMarket?: TerminalMarketInfo;
  isPerp: boolean;
  ticker?: MarketTicker;
  priceDigits: number;
  bestBid: string;
  bestAsk: string;
  spreadPct: string;
  marketStatus: string;
  fundingState?: ContractPriceStateLike;
  maxLeverage?: string;
  publicStatus: StreamStatus;
  privateStatus: StreamStatus;
  tickerUpdatedAt?: number;
  onSelectSymbol: (symbol: string) => void;
  extraRight?: React.ReactNode;
}) {
  const liveMakers = useLiveMakerSymbols();
  const changePct = Number(ticker?.change_24h_pct ?? 0);
  const changeTone: Tone = changePct > 0 ? "buy" : changePct < 0 ? "sell" : "neutral";
  const lastPrice = ticker?.last_price ?? ticker?.mid_price ?? ticker?.best_bid ?? ticker?.best_ask ?? currentMarket?.last_price ?? currentMarket?.reference_price;
  const markPrice = fundingState?.mark_price ?? ticker?.mid_price ?? lastPrice;
  const oraclePrice = fundingState?.index_price ?? currentMarket?.reference_price ?? ticker?.mid_price;
  const fundingRate = fundingState?.funding_rate === undefined ? undefined : Number(fundingState.funding_rate) * 100;
  const blocked = marketStatus !== "TRADING" && marketStatus !== "REDUCE_ONLY" && marketStatus !== "PRE_OPEN";

  return (
    <header className="panel hl-marketbar">
      <div className="flex min-w-0 flex-wrap items-center gap-x-5 gap-y-2">
        <div className="flex min-w-[260px] items-center gap-2.5">
          <span className="grid h-7 w-7 shrink-0 place-items-center rounded-full bg-[#35d6b2] text-[10px] font-black text-[#071312]">◆</span>
          <select
            value={symbol}
            onChange={(event) => onSelectSymbol(event.target.value)}
            className="h-9 max-w-[220px] rounded-xl border border-white/10 bg-slate-950/70 px-2.5 text-sm font-semibold text-white outline-none focus:border-cyan-300/40"
            title="切换市场"
          >
            {markets.map((market) => (
              <option key={market.symbol} value={market.symbol}>
                {market.symbol.replace("USDT", "-USDT")} {market.product_type === "PERP" ? "Perp" : "Spot"}{liveMakers.has(market.symbol) ? " · LIVE" : ""}
              </option>
            ))}
          </select>
          <span className="rounded-md bg-cyan-400/12 px-1.5 py-0.5 text-[10px] text-cyan-100">{isPerp ? `${maxLeverage ?? "-"}x` : "Spot"}</span>
          {marketStatus !== "TRADING" ? (
            <span className={`rounded-md px-1.5 py-0.5 text-[10px] ${blocked ? "bg-rose-400/12 text-rose-200" : "bg-amber-400/12 text-amber-100"}`} title="当前市场状态">
              {marketStatusLabel(marketStatus)}
            </span>
          ) : null}
        </div>

        <div className="flex min-w-[138px] items-baseline gap-2.5">
          <FlashPrice tone={changeTone} className={`font-display text-2xl leading-8 tabular-nums ${changeTone === "buy" ? "text-emerald-300" : changeTone === "sell" ? "text-rose-300" : "text-slate-100"}`}>
            {showNumber(lastPrice, priceDigits)}
          </FlashPrice>
          <span className={`text-xs font-semibold tabular-nums ${changeTone === "buy" ? "text-emerald-300" : changeTone === "sell" ? "text-rose-300" : "text-slate-400"}`}>
            {ticker ? `${signedNumber(changePct, 2)}%` : "-"}
          </span>
        </div>

        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-5 gap-y-1.5">
          <HeaderStat label="Mark" value={showNumber(markPrice, priceDigits)} />
          <HeaderStat label="Oracle" value={showNumber(oraclePrice, priceDigits)} />
          <HeaderStat label="24h Change" value={ticker ? `${signedNumber(changePct, 2)}%` : "-"} tone={changeTone} />
          <HeaderStat label="24h Volume" value={ticker ? `$${fmtCompact(ticker.quote_volume_24h ?? ticker.volume_24h, 2)}` : "-"} />
          <HeaderStat label="Open Interest" value="-" />
          <HeaderStat
            label="Funding / Countdown"
            value={isPerp && fundingRate !== undefined ? `${fundingRate > 0 ? "+" : ""}${showPct(fundingRate, 4)}  ${countdown(fundingState?.next_funding_time)}` : "-"}
            tone={fundingRate && fundingRate < 0 ? "buy" : fundingRate && fundingRate > 0 ? "sell" : "neutral"}
          />
          <span className="hidden text-[10px] text-slate-500 xl:inline">BBO {showNumber(bestBid, priceDigits)} / {showNumber(bestAsk, priceDigits)} · {showPct(spreadPct, 3)}</span>
        </div>

        <div className="flex shrink-0 flex-wrap items-center gap-1.5">
          <ConnectionBadge label="行情" status={publicStatus} lastMessageAt={tickerUpdatedAt} />
          <ConnectionBadge label="账户" status={privateStatus} />
          {extraRight}
        </div>
      </div>
    </header>
  );
}

function HeaderStat({ label, value, tone = "neutral" }: { label: string; value: React.ReactNode; tone?: Tone }) {
  return (
    <div className="min-w-[82px]" title={`${label}: ${String(value)}`}>
      <div className="text-[10px] text-slate-500">{label}</div>
      <div className={`mt-0.5 truncate font-mono text-xs tabular-nums ${tone === "buy" ? "text-emerald-300" : tone === "sell" ? "text-rose-300" : "text-slate-200"}`}>{value}</div>
    </div>
  );
}

export type ContractPriceStateLike = {
  mark_price?: string;
  index_price?: string;
  funding_rate?: string | number;
  next_funding_time?: number | null;
};
