import { useEffect, useState } from "react";
import { api } from "../api/client";
import { AppShell } from "../components/AppShell";
import { config } from "../lib/config";
import { bjTime } from "../lib/format";

type BotItem = {
  username: string;
  api_status: string;
  latest_order_latency_ms?: number;
  latest_cancel_latency_ms?: number;
  open_order_count: number;
  inventory: { asset: string; available: string; frozen: string }[];
  recent_trade_id?: string;
  last_heartbeat?: number;
};

type LiquidityItem = {
  symbol: string;
  regime: string;
  local_mid?: string | null;
  external_mid?: string | null;
  inventory_delta_btc?: string;
  health?: {
    spread_bps?: string;
    local_vs_external_bps?: string;
    action_queue_size?: number;
    reason?: string;
  };
  recovery?: {
    inner_scale?: string;
    mid_scale?: string;
    outer_scale?: string;
    repair_active?: boolean;
    repair_side?: string | null;
    repair_strength?: string;
    repair_reason?: string;
  };
  targets?: Record<string, string>;
  depth?: {
    buy?: Record<string, string>;
    sell?: Record<string, string>;
  };
  makers?: Record<
    string,
    {
      username: string;
      open_order_count: number;
      open_quantity: string;
    }
  >;
  desired?: Record<
    string,
    {
      order_count: number;
      quantity: string;
    }
  >;
};

export function BotsPage() {
  const [items, setItems] = useState<BotItem[]>([]);
  const [liquidity, setLiquidity] = useState<LiquidityItem[]>([]);

  useEffect(() => {
    let timer = 0;
    const load = async () => {
      const data = await api.get<{ items: BotItem[]; liquidity?: LiquidityItem[] }>("/ops/bots", config.adminApiKey);
      setItems(data.items);
      setLiquidity(data.liquidity ?? []);
    };
    void load();
    timer = window.setInterval(() => void load(), 3000);
    return () => clearInterval(timer);
  }, []);

  return (
    <AppShell>
      {liquidity.length > 0 && (
        <div className="mb-6 grid gap-4">
          {liquidity.map((item) => (
            <section key={item.symbol} className="panel rounded-3xl p-5">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h2 className="font-display text-2xl">{item.symbol} V2</h2>
                  <p className="mt-1 text-sm text-slate-400">
                    Regime {item.regime} · spread {item.health?.spread_bps ?? "--"} bps · 偏离 {item.health?.local_vs_external_bps ?? "--"} bps
                  </p>
                </div>
                <div className="rounded-full bg-cyan-400/12 px-3 py-1 text-xs text-cyan-100">
                  队列 {item.health?.action_queue_size ?? "--"} · 恢复 {item.recovery?.inner_scale ?? "--"}/{item.recovery?.mid_scale ?? "--"}/{item.recovery?.outer_scale ?? "--"}
                </div>
              </div>
              <div className="mt-4 grid gap-3 md:grid-cols-4">
                <Metric label="本地中价" value={item.local_mid ?? "--"} />
                <Metric label="外部中价" value={item.external_mid ?? "--"} />
                <Metric label="库存偏移" value={item.inventory_delta_btc ?? "--"} />
                <Metric
                  label="修复状态"
                  value={
                    item.recovery?.repair_active
                      ? `${item.recovery?.repair_side ?? "--"} · ${item.recovery?.repair_strength ?? "--"}`
                      : item.health?.reason ?? "--"
                  }
                />
              </div>
              <div className="mt-4 grid gap-3 xl:grid-cols-5">
                {Object.entries(item.targets ?? {}).map(([label, target]) => (
                  <div key={label} className="rounded-2xl bg-white/5 px-3 py-3 text-sm text-slate-300">
                    <div className="text-xs uppercase tracking-[0.18em] text-slate-500">{label}</div>
                    <div className="mt-2 flex items-center justify-between">
                      <span>Bid</span>
                      <span>{item.depth?.buy?.[label] ?? "--"}</span>
                    </div>
                    <div className="mt-1 flex items-center justify-between">
                      <span>Ask</span>
                      <span>{item.depth?.sell?.[label] ?? "--"}</span>
                    </div>
                    <div className="mt-1 flex items-center justify-between text-cyan-100">
                      <span>Target</span>
                      <span>{target}</span>
                    </div>
                  </div>
                ))}
              </div>
              <div className="mt-4 grid gap-3 md:grid-cols-3">
                {Object.entries(item.makers ?? {}).map(([maker, makerState]) => (
                  <div key={maker} className="rounded-2xl bg-white/5 px-3 py-3 text-sm text-slate-300">
                    <div className="flex items-center justify-between">
                      <span className="font-medium text-white">{makerState.username}</span>
                      <span>{makerState.open_order_count} live</span>
                    </div>
                    <div className="mt-2 flex items-center justify-between">
                      <span>挂单量</span>
                      <span>{makerState.open_quantity}</span>
                    </div>
                    <div className="mt-1 flex items-center justify-between text-cyan-100">
                      <span>目标</span>
                      <span>{item.desired?.[maker]?.quantity ?? "--"}</span>
                    </div>
                  </div>
                ))}
              </div>
            </section>
          ))}
        </div>
      )}
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {items.map((item) => (
          <section key={item.username} className="panel rounded-3xl p-4">
            <div className="flex items-start justify-between">
              <div>
                <h2 className="font-display text-xl">{item.username}</h2>
                <p className="mt-1 text-sm text-slate-400">API {item.api_status} · 最近成交 {item.recent_trade_id ?? "--"}</p>
              </div>
              <div className="rounded-full bg-cyan-400/12 px-3 py-1 text-xs text-cyan-100">{item.open_order_count} 挂单</div>
            </div>
            <div className="mt-4 grid gap-3 md:grid-cols-2">
              <Metric label="下单耗时" value={item.latest_order_latency_ms ? `${item.latest_order_latency_ms} ms` : "--"} />
              <Metric label="撤单耗时" value={item.latest_cancel_latency_ms ? `${item.latest_cancel_latency_ms} ms` : "--"} />
            </div>
            <div className="mt-4 space-y-2">
              {item.inventory.map((balance) => (
                <div key={balance.asset} className="rounded-2xl bg-white/5 px-3 py-3 text-sm text-slate-300">
                  <div className="flex items-center justify-between">
                    <span>{balance.asset}</span>
                    <span>{balance.available}</span>
                  </div>
                  <div className="mt-1 text-xs text-slate-500">冻结 {balance.frozen}</div>
                </div>
              ))}
            </div>
            <div className="mt-4 text-xs text-slate-500">最后心跳 {item.last_heartbeat ? bjTime(item.last_heartbeat) : "--"}</div>
          </section>
        ))}
      </div>
    </AppShell>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl bg-white/5 px-3 py-3">
      <div className="text-xs uppercase tracking-[0.18em] text-slate-500">{label}</div>
      <div className="mt-1 text-sm text-white">{value}</div>
    </div>
  );
}
