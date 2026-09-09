import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { api } from "../api/client";
import { config, appBasePath } from "../lib/config";
import { useAppStore } from "../store/useAppStore";
import type { SandboxHealth } from "../types";

type BrandResponse = {
  brand: {
    exchange_name: string;
    primary_color: string;
    paper_notice?: string | null;
    footer_text?: string | null;
    logo_url?: string | null;
  };
};

export function AppShell({ children }: { children: ReactNode }) {
  const location = useLocation();
  const isWideSurface = (location.pathname === "/ops/orderbook" || location.pathname.startsWith("/ops/orderbook/"));
  const isTradeSurface = /(^|\/)(trade|paper\/trade)\//.test(location.pathname);
  const authSession = useAppStore((state) => state.authSession);
  const setAuthSession = useAppStore((state) => state.setAuthSession);
  const pushToast = useAppStore((state) => state.pushToast);
  const [showPasswordForm, setShowPasswordForm] = useState(false);
  const [passwordBusy, setPasswordBusy] = useState(false);
  const [passwordForm, setPasswordForm] = useState({
    currentPassword: "",
    newPassword: "",
    confirmPassword: "",
  });
  const [sandboxHealth, setSandboxHealth] = useState<SandboxHealth>();
  const [brand, setBrand] = useState<BrandResponse["brand"]>();

  useEffect(() => {
    let disposed = false;
    const refreshSandboxHealth = async () => {
      try {
        const apiOrigin = new URL(config.apiBaseUrl, window.location.origin).origin;
        const response = await fetch(`${apiOrigin}${appBasePath}/health`, { headers: { Accept: "application/json" } });
        if (!response.ok) throw new Error(`health ${response.status}`);
        const payload = (await response.json()) as SandboxHealth;
        if (!disposed) setSandboxHealth(payload);
      } catch {
        if (!disposed) setSandboxHealth(undefined);
      }
    };
    void refreshSandboxHealth();
    const timer = window.setInterval(() => void refreshSandboxHealth(), 10_000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    let disposed = false;
    void api
      .get<BrandResponse>("/paper/brand")
      .then((response) => {
        if (!disposed) setBrand(response.brand);
      })
      .catch(() => undefined);
    return () => {
      disposed = true;
    };
  }, []);

  const navItems: [string, string][] = [
    ["/trade/BTCUSDT", "交易终端"],
    ["/ops/liquidity-map", "流动性地图"],
    ...(authSession?.role === "admin" ? [["/ops/orderbook", "盘口监控"] as [string, string]] : []),
    ["/paper/assets", "资产"],
    ["/paper/orders", "订单"],
    ["/paper/account", "账户"],
    ...(authSession?.role === "admin"
      ? ([
          ["/admin", "管理后台"],
          ["/ops/runtime", "运行状态"],
        ] as [string, string][])
      : []),
  ];

  const submitPasswordChange = async (event: FormEvent) => {
    event.preventDefault();
    if (!authSession) return;
    if (passwordForm.newPassword.length < 8) {
      pushToast("error", "新密码至少 8 位");
      return;
    }
    if (passwordForm.newPassword !== passwordForm.confirmPassword) {
      pushToast("error", "两次输入的新密码不一致");
      return;
    }
    setPasswordBusy(true);
    try {
      await api.post(
        "/auth/change-password",
        {
          current_password: passwordForm.currentPassword,
          new_password: passwordForm.newPassword,
          confirm_password: passwordForm.confirmPassword,
        },
        authSession.api_key,
      );
      setPasswordForm({ currentPassword: "", newPassword: "", confirmPassword: "" });
      setShowPasswordForm(false);
      pushToast("success", "密码已更新");
    } catch (error) {
      pushToast("error", error instanceof Error ? error.message : "修改密码失败");
    } finally {
      setPasswordBusy(false);
    }
  };

  const logout = async () => {
    await api.post("/auth/logout").catch(() => undefined);
    setShowPasswordForm(false);
    setAuthSession(undefined);
  };

  const name = brand?.exchange_name ?? "Liquidity Platform";
  const primaryColor = brand?.primary_color ?? "#35d6b2";

  return (
    <div className={`app-shell min-h-screen px-3 py-3 text-slate-100 md:px-5 ${isTradeSurface ? "hl-shell" : ""}`}>
      <div className={`app-shell-inner mx-auto max-w-[1680px] ${isWideSurface ? "app-shell-wide" : ""} ${isTradeSurface ? "hl-shell-inner" : ""}`}>
        <header
          className={`app-shell-header sticky top-2 z-30 rounded-2xl border border-white/10 bg-slate-950/95 px-3 py-2.5 shadow-2xl backdrop-blur md:px-4 ${
            isTradeSurface ? "hl-app-header" : ""
          }`}
        >
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <div className="flex min-w-0 items-center gap-2.5">
              {brand?.logo_url ? (
                <img src={brand.logo_url} alt="" className="h-8 w-8 shrink-0 rounded-lg object-cover" />
              ) : (
                <div className="grid h-8 w-8 shrink-0 place-items-center rounded-lg font-black text-slate-950" style={{ backgroundColor: primaryColor }}>
                  {name.slice(0, 1).toUpperCase()}
                </div>
              )}
              <div className="min-w-0">
                <Link to="/trade/BTCUSDT" className="hl-brand-link block truncate font-display text-lg leading-5 tracking-tight text-white">
                  {name}
                </Link>
                <span className="mt-0.5 inline-block rounded-full px-1.5 py-px text-[10px] leading-4" style={{ backgroundColor: `${primaryColor}22`, color: primaryColor }}>
                  模拟交易 · 做市实验室
                </span>
              </div>
            </div>

            <nav className="order-3 flex w-full items-center gap-1 overflow-x-auto text-sm scrollbar md:order-none md:w-auto md:flex-1 md:justify-start">
              {navItems.map(([path, label]) => (
                <NavLink
                  key={path}
                  to={path}
                  className={({ isActive }) =>
                    `whitespace-nowrap rounded-xl px-3 py-1.5 text-sm transition ${
                      isActive ? "bg-cyan-400/18 font-medium text-cyan-200" : "text-slate-300 hover:bg-white/10 hover:text-slate-100"
                    }`
                  }
                >
                  {label}
                </NavLink>
              ))}
            </nav>

            <div className="flex items-center gap-2">
              {sandboxHealth ? (
                <div
                  className={`hidden items-center gap-x-2 rounded-2xl border px-3 py-1.5 text-[11px] lg:flex ${
                    sandboxHealth.storage_degraded
                      ? "border-amber-300/30 bg-amber-400/10 text-amber-100"
                      : "border-cyan-300/20 bg-cyan-400/8 text-cyan-100"
                  }`}
                  title={`run_id=${sandboxHealth.run_id}`}
                >
                  <span>{sandboxHealth.storage_degraded ? "存储需关注" : "模拟环境"}</span>
                  <span className="text-slate-400">{sandboxHealth.runtime_state_durable ? "状态已持久化" : "内存运行"}</span>
                </div>
              ) : null}
              {authSession ? (
                <div className="flex items-center gap-2 rounded-full bg-white/5 px-3 py-1.5 text-xs text-slate-300">
                  <span className="max-w-[140px] truncate">{authSession.username}</span>
                  <button type="button" onClick={() => setShowPasswordForm((current) => !current)} className="text-cyan-200 hover:text-cyan-100">
                    改密码
                  </button>
                  <button type="button" onClick={() => void logout()} className="text-cyan-200 hover:text-cyan-100">
                    退出
                  </button>
                </div>
              ) : (
                <NavLink to="/login" className="rounded-full bg-cyan-400/18 px-4 py-2 text-sm text-cyan-100">
                  登录
                </NavLink>
              )}
            </div>
          </div>
        </header>
        {authSession && showPasswordForm ? (
          <form onSubmit={submitPasswordChange} className="mb-4 rounded-3xl border border-white/10 bg-slate-900/55 p-4 shadow-glow backdrop-blur">
            <div className="grid gap-3 md:grid-cols-4">
              <label className="text-xs text-slate-400">
                当前密码
                <input
                  type="password"
                  value={passwordForm.currentPassword}
                  onChange={(event) => setPasswordForm((current) => ({ ...current, currentPassword: event.target.value }))}
                  className="mt-1 w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2 text-sm text-slate-100 outline-none"
                  autoComplete="current-password"
                />
              </label>
              <label className="text-xs text-slate-400">
                新密码
                <input
                  type="password"
                  value={passwordForm.newPassword}
                  onChange={(event) => setPasswordForm((current) => ({ ...current, newPassword: event.target.value }))}
                  className="mt-1 w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2 text-sm text-slate-100 outline-none"
                  autoComplete="new-password"
                />
              </label>
              <label className="text-xs text-slate-400">
                确认新密码
                <input
                  type="password"
                  value={passwordForm.confirmPassword}
                  onChange={(event) => setPasswordForm((current) => ({ ...current, confirmPassword: event.target.value }))}
                  className="mt-1 w-full rounded-2xl border border-white/10 bg-white/5 px-3 py-2 text-sm text-slate-100 outline-none"
                  autoComplete="new-password"
                />
              </label>
              <div className="flex items-end gap-2">
                <button type="submit" disabled={passwordBusy} className="h-9 rounded-2xl bg-cyan-400/18 px-4 text-sm text-cyan-100 disabled:cursor-not-allowed disabled:opacity-60">
                  {passwordBusy ? "保存中" : "保存"}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setPasswordForm({ currentPassword: "", newPassword: "", confirmPassword: "" });
                    setShowPasswordForm(false);
                  }}
                  className="h-9 rounded-2xl bg-white/8 px-4 text-sm text-slate-200"
                >
                  取消
                </button>
              </div>
            </div>
          </form>
        ) : null}
        <main className={`app-shell-main ${isTradeSurface ? "hl-main" : ""}`}>{children}</main>
      </div>
    </div>
  );
}
