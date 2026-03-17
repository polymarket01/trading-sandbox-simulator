import { useEffect, useState } from "react";
import { api } from "../api/client";
import { AppShell } from "../components/AppShell";
import { ToastViewport } from "../components/ToastViewport";
import { config } from "../lib/config";
import { useAppStore } from "../store/useAppStore";
import type { AdminMarketItem } from "../types";

type AdminUser = {
  id: number;
  username: string;
  role: string;
  api_key: string;
  api_secret: string;
  balances: { asset: string; available: string; frozen: string }[];
};

export function AdminPage() {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [markets, setMarkets] = useState<Record<string, AdminMarketItem>>({});
  const [marketSymbols, setMarketSymbols] = useState<string[]>([]);
  const pushToast = useAppStore((state) => state.pushToast);

  const load = async () => {
    const [data, marketData] = await Promise.all([
      api.get<{ items: AdminUser[] }>("/admin/users", config.adminApiKey),
      api.get<{ items: AdminMarketItem[] }>("/admin/markets", config.adminApiKey),
    ]);
    setUsers(data.items);
    setMarketSymbols(marketData.items.map((item) => item.symbol));
    setMarkets(Object.fromEntries(marketData.items.map((item) => [item.symbol, item])));
  };

  useEffect(() => {
    void load();
  }, []);

  const resetUser = async (userId: number) => {
    await api.post(`/admin/users/${userId}/reset-balances`, undefined, config.adminApiKey);
    await load();
  };

  const resetAllTestUsers = async () => {
    await api.post("/admin/reset-test-users", undefined, config.adminApiKey);
    await load();
  };

  const adjustUser = async (userId: number, asset: string) => {
    await api.post(
      `/admin/users/${userId}/adjust-balance`,
      { asset, amount: "1000", reason: "admin quick adjust" },
      config.adminApiKey,
    );
    await load();
  };

  const toggleMarket = async (symbol: string) => {
    const active = !markets[symbol]?.is_active;
    await api.put(`/admin/markets/${symbol}`, { is_active: active }, config.adminApiKey);
    await load();
  };

  const updateMarketField = (symbol: string, field: keyof AdminMarketItem, value: string | number | boolean) => {
    setMarkets((current) => ({
      ...current,
      [symbol]: {
        ...current[symbol],
        [field]: value,
      },
    }));
  };

  const saveMarketConfig = async (symbol: string) => {
    const market = markets[symbol];
    if (!market) return;
    await api.put(
      `/admin/markets/${symbol}`,
      {
        price_tick: market.price_tick,
        qty_step: market.qty_step,
        min_qty: market.min_qty,
        min_notional: market.min_notional,
        price_precision: Number(market.price_precision),
        qty_precision: Number(market.qty_precision),
      },
      config.adminApiKey,
    );
    await load();
    pushToast("success", `${symbol} 市场参数已更新`);
  };

  const resetMarket = async (symbol: string) => {
    await api.post(`/admin/markets/${symbol}/reset`, undefined, config.adminApiKey);
    await load();
  };

  const wipeMarketData = async (symbol: string) => {
    await api.post(`/admin/markets/${symbol}/wipe-data`, undefined, config.adminApiKey);
    await load();
  };

  return (
    <AppShell>
      <ToastViewport />
      <div className="grid gap-4 xl:grid-cols-[1.15fr_0.85fr]">
        <section className="panel rounded-3xl p-4">
          <div className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
            <div>
              <h2 className="font-display text-xl">用户与测试资金</h2>
              <p className="mt-1 text-sm text-slate-400">默认重置资金：每个测试账户 1 亿 USDT、1 亿 BTC、1 亿 XXX、1 亿 YYY、1 亿 ZZZ。</p>
            </div>
            <button onClick={resetAllTestUsers} className="rounded-2xl bg-cyan-400/18 px-4 py-3 text-sm font-medium text-cyan-100">
              一键重置全部测试账户
            </button>
          </div>
          <div className="space-y-3">
            {users.map((user) => (
              <div key={user.id} className="rounded-3xl border border-white/8 bg-white/5 p-4">
                <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
                  <div>
                    <div className="font-display text-lg">{user.username}</div>
                    <div className="mt-1 text-sm text-slate-400">{user.role} · key {user.api_key} · secret {user.api_secret}</div>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <button onClick={() => resetUser(user.id)} className="rounded-full bg-cyan-400/16 px-4 py-2 text-sm text-cyan-100">重置资金</button>
                    {user.balances.map((item) => (
                      <button key={`${user.id}-${item.asset}`} onClick={() => adjustUser(user.id, item.asset)} className="rounded-full bg-white/6 px-4 py-2 text-sm text-slate-200">
                        +1000 {item.asset}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="mt-3 grid gap-2 md:grid-cols-3">
                  {user.balances.map((item) => (
                    <div key={item.asset} className="rounded-2xl bg-slate-950/35 px-3 py-3 text-sm text-slate-300">
                      <div className="text-slate-500">{item.asset}</div>
                      <div className="mt-1">可用 {item.available}</div>
                      <div className="text-slate-500">冻结 {item.frozen}</div>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </section>
        <section className="space-y-4">
          {marketSymbols.map((symbol) => (
            <div key={symbol} className="panel rounded-3xl p-4">
              <h3 className="font-display text-lg">{symbol}</h3>
              <p className="mt-2 text-sm text-slate-400">
                当前状态：{markets[symbol]?.is_active ? "已启用，可继续接收新订单" : "已暂停，新的下单请求会被直接拒绝"}。
              </p>
              <div className="mt-4 flex flex-wrap gap-2">
                <button onClick={() => toggleMarket(symbol)} className="rounded-2xl bg-amber-400/16 px-4 py-2 text-sm text-amber-100">
                  {markets[symbol]?.is_active ? "暂停新下单" : "恢复市场"}
                </button>
                <button onClick={() => saveMarketConfig(symbol)} className="rounded-2xl bg-cyan-400/16 px-4 py-2 text-sm text-cyan-100">
                  保存市场参数
                </button>
                <button onClick={() => resetMarket(symbol)} className="rounded-2xl bg-rose-500/16 px-4 py-2 text-sm text-rose-100">
                  撤销全部挂单
                </button>
                <button onClick={() => wipeMarketData(symbol)} className="rounded-2xl bg-white/8 px-4 py-2 text-sm text-slate-100">
                  清空该市场全部历史数据
                </button>
              </div>
              <div className="mt-4 grid gap-3 md:grid-cols-2">
                <label className="block">
                  <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">price_tick</span>
                  <input
                    value={markets[symbol]?.price_tick ?? ""}
                    onChange={(event) => updateMarketField(symbol, "price_tick", event.target.value)}
                    className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none"
                  />
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">qty_step</span>
                  <input
                    value={markets[symbol]?.qty_step ?? ""}
                    onChange={(event) => updateMarketField(symbol, "qty_step", event.target.value)}
                    className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none"
                  />
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">min_qty</span>
                  <input
                    value={markets[symbol]?.min_qty ?? ""}
                    onChange={(event) => updateMarketField(symbol, "min_qty", event.target.value)}
                    className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none"
                  />
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">min_notional</span>
                  <input
                    value={markets[symbol]?.min_notional ?? ""}
                    onChange={(event) => updateMarketField(symbol, "min_notional", event.target.value)}
                    className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none"
                  />
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">price_precision</span>
                  <input
                    type="number"
                    value={markets[symbol]?.price_precision ?? 0}
                    onChange={(event) => updateMarketField(symbol, "price_precision", Number(event.target.value))}
                    className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none"
                  />
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs uppercase tracking-[0.16em] text-slate-500">qty_precision</span>
                  <input
                    type="number"
                    value={markets[symbol]?.qty_precision ?? 0}
                    onChange={(event) => updateMarketField(symbol, "qty_precision", Number(event.target.value))}
                    className="w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2.5 text-sm outline-none"
                  />
                </label>
              </div>
              <div className="mt-3 text-xs text-slate-500">
                保存后新请求会立即按新的 tick、step 和精度校验，不需要重启服务。清空历史数据会删除该市场的订单、成交、K 线与相关账本记录，并按剩余账本重建余额。
              </div>
            </div>
          ))}
        </section>
      </div>
    </AppShell>
  );
}
