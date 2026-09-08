import { useState } from "react";
import { config } from "../lib/config";
import { fmt } from "../lib/format";
import type { AuthSession } from "../lib/session";
import type { BalanceItem, FeeProfileItem, MarketTicker } from "../types";

export function AccountCard({
  balances,
  ticker,
  username,
  fee,
  session,
}: {
  balances: BalanceItem[];
  ticker?: MarketTicker;
  username?: string;
  fee?: FeeProfileItem;
  session?: AuthSession;
}) {
  const [showSecret, setShowSecret] = useState(false);
  const lookup = Object.fromEntries(balances.map((item) => [item.asset, item]));
  const mid = Number(ticker?.mid_price ?? 0);
  const currentBaseAsset = ticker?.symbol.replace("USDT", "");
  const feeSource = fee?.source === "market_default" ? "market default" : (fee?.source ?? "-");
  const showApiAccess = session?.role === "admin";
  const connectivityUrl = `${config.apiBaseUrl}/account/connectivity`;
  const connectivityCurl = session ? `curl -H "X-API-Key: ${session.api_key}" ${connectivityUrl}` : "";
  const visibleBalances = [...balances].sort((left, right) => {
    const rank = (asset: string) => {
      if (asset === "USDT") return 0;
      if (asset === "BTC") return 1;
      return 2;
    };
    return rank(left.asset) - rank(right.asset) || left.asset.localeCompare(right.asset);
  });
  const estimate =
    Number(lookup.USDT?.available ?? 0) +
    Number(lookup.USDT?.frozen ?? 0) +
    (currentBaseAsset
      ? (Number(lookup[currentBaseAsset]?.available ?? 0) + Number(lookup[currentBaseAsset]?.frozen ?? 0)) * mid
      : 0);

  return (
    <section className="panel rounded-2xl p-4">
      <div className="mb-4 flex items-center justify-between">
        <h3 className="font-display text-lg">账户概览</h3>
        <span className="text-xs text-slate-500">{username ?? "-"}</span>
      </div>
      <div className="space-y-3">
        {visibleBalances.map((item) => {
          const asset = item.asset;
          return (
            <div key={asset} className="rounded-2xl bg-white/5 px-3 py-3">
              <div className="mb-1 flex items-center justify-between text-sm text-slate-400">
                <span>{asset}</span>
                <span>{fmt(Number(item?.available ?? 0) + Number(item?.frozen ?? 0), 4)}</span>
              </div>
              <div className="flex items-center justify-between text-sm">
                <span className="text-slate-500">可用 {fmt(item?.available, 4)}</span>
                <span className="text-slate-500">冻结 {fmt(item?.frozen, 4)}</span>
              </div>
            </div>
          );
        })}
      </div>
      <div className="mt-4 rounded-2xl bg-cyan-400/10 px-3 py-3">
        <div className="text-xs uppercase tracking-[0.18em] text-cyan-200/70">当前页可估资产</div>
        <div className="mt-2 font-display text-2xl text-white">{fmt(estimate, 2)} USDT</div>
      </div>
      <div className="mt-3 rounded-2xl border border-white/10 bg-white/5 px-3 py-3">
        <div className="mb-2 flex items-center justify-between gap-2">
          <span className="text-xs uppercase tracking-[0.18em] text-slate-500">当前交易对费率</span>
          <span className="truncate text-xs text-slate-500">{fee?.symbol ?? ticker?.symbol ?? "-"}</span>
        </div>
        <div className="grid grid-cols-3 gap-2 text-sm">
          <div>
            <div className="text-xs text-slate-500">maker</div>
            <div className="mt-1 truncate font-mono text-slate-100">{fee?.maker_fee_rate ?? "-"}</div>
          </div>
          <div>
            <div className="text-xs text-slate-500">taker</div>
            <div className="mt-1 truncate font-mono text-slate-100">{fee?.taker_fee_rate ?? "-"}</div>
          </div>
          <div>
            <div className="text-xs text-slate-500">source</div>
            <div className="mt-1 truncate font-mono text-slate-100">{feeSource}</div>
          </div>
        </div>
      </div>
      {session && showApiAccess && (
        <div className="mt-3 rounded-2xl border border-white/10 bg-white/5 px-3 py-3">
          <div className="mb-3 flex items-center justify-between gap-2">
            <span className="text-xs uppercase tracking-[0.18em] text-slate-500">API 接入</span>
            <span className="truncate text-xs text-slate-500">{session.role}</span>
          </div>
          <div className="space-y-2 text-xs">
            <CredentialRow label="REST" value={config.apiBaseUrl} />
            <CredentialRow label="Public WS" value={config.publicWsUrl} />
            <CredentialRow label="Private WS" value={config.privateWsUrl} />
            <CredentialRow label="API Key" value={session.api_key} />
            <CredentialRow label="Config JSON" value={connectivityUrl} />
            <CredentialRow label="cURL" value={connectivityCurl} />
            <div className="rounded-xl bg-slate-950/35 px-3 py-2">
              <div className="mb-1 flex items-center justify-between gap-2">
                <span className="text-slate-500">API Secret</span>
                <button onClick={() => setShowSecret((current) => !current)} className="shrink-0 text-cyan-200 hover:text-cyan-100">
                  {showSecret ? "隐藏" : "显示"}
                </button>
              </div>
              <div className="break-all font-mono text-slate-100">{showSecret ? session.api_secret : "••••••••••••••••"}</div>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

function CredentialRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl bg-slate-950/35 px-3 py-2">
      <div className="mb-1 text-slate-500">{label}</div>
      <div className="break-all font-mono text-slate-100">{value}</div>
    </div>
  );
}
