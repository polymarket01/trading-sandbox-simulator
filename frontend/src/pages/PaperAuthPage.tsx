import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { useAppStore } from "../store/useAppStore";
import type { AuthSession } from "../lib/session";

type BrandResponse = { brand: { exchange_name: string; paper_notice: string; primary_color: string; registration_enabled?: boolean } };

export function PaperAuthPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const setAuthSession = useAppStore((state) => state.setAuthSession);
  const authSession = useAppStore((state) => state.authSession);
  const registerMode = location.pathname.endsWith("/register");
  const search = new URLSearchParams(location.search);
  const nextPath = search.get("next");
  const adminRequired = search.get("admin_required") === "1";
  const [brand, setBrand] = useState<BrandResponse["brand"]>();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [remember, setRemember] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (authSession) navigate("/trade/BTCUSDT", { replace: true });
  }, [authSession, navigate]);

  useEffect(() => {
    void api.get<BrandResponse>("/paper/brand").then((response) => setBrand(response.brand)).catch(() => undefined);
  }, []);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    if (registerMode && password !== confirmPassword) {
      setError("两次输入的密码不一致");
      return;
    }
    setBusy(true);
    try {
      const response = registerMode
        ? await api.post<{ user: AuthSession }>("/auth/register", { username, password, confirm_password: confirmPassword })
        : await api.post<{ user: AuthSession }>("/auth/login", { username, password, remember });
      setAuthSession(response.user);
      navigate(nextPath && !registerMode ? nextPath : "/trade/BTCUSDT", { replace: true });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "操作失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-page paper-auth-page text-slate-100">
      <div className="auth-layout mx-auto grid max-w-[980px] gap-6 lg:grid-cols-[1.1fr_0.9fr] lg:py-12">
        <section className="auth-hero flex flex-col justify-center p-7">
          <div className="text-xs uppercase tracking-[0.28em] text-cyan-200/80">{brand?.exchange_name ?? "Paper Exchange"}</div>
          <h1 className="mt-5 font-display text-4xl leading-tight text-white">轻量模拟交易所</h1>
          <p className="mt-4 max-w-lg text-sm leading-7 text-slate-300">{brand?.paper_notice ?? "所有资产均为模拟资金，不涉及真实资金。"}</p>
          <div className="mt-8 grid gap-3 text-sm text-slate-300 sm:grid-cols-3">
            <Feature title="现货" text="模拟币对" />
            <Feature title="永续" text="USDT 本位" />
            <Feature title="账户" text="可随时复位" />
          </div>
          <div className="mt-6 rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-xs leading-6 text-slate-400">
            账户具有独立的现货与合约测试资金（各 1 亿 USDT）。本页面不连接任何真实交易所，不使用真实密钥。
          </div>
        </section>
        <form onSubmit={submit} className="auth-card p-6">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-xs text-cyan-200/70">{brand?.exchange_name ?? "Paper Exchange"}</div>
              <h2 className="mt-2 font-display text-2xl">{registerMode ? "创建模拟账户" : "登录模拟账户"}</h2>
            </div>
            <span className="rounded-full bg-cyan-300/10 px-3 py-1 text-xs text-cyan-100">PAPER</span>
          </div>
          <div className="mt-6 space-y-4">
            {adminRequired ? (
              <div className="rounded-2xl border border-amber-300/20 bg-amber-400/10 px-3 py-2 text-xs leading-5 text-amber-100">
                该页面需要管理员账号登录。请使用 paper_admin 登录后再访问。
              </div>
            ) : null}
            <Field label="用户名">
              <input
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                className="paper-input"
                autoComplete="username"
                placeholder="3-24 位字母、数字或下划线"
                required
              />
            </Field>
            <Field label="密码">
              <input
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                className="paper-input"
                autoComplete={registerMode ? "new-password" : "current-password"}
                required
              />
            </Field>
            {registerMode ? (
              <Field label="确认密码">
                <input
                  type="password"
                  value={confirmPassword}
                  onChange={(event) => setConfirmPassword(event.target.value)}
                  className="paper-input"
                  autoComplete="new-password"
                  required
                />
              </Field>
            ) : (
              <label className="flex items-center gap-2 text-xs text-slate-400">
                <input type="checkbox" checked={remember} onChange={(event) => setRemember(event.target.checked)} className="accent-cyan-300" />
                保持登录（关闭浏览器后仍保留会话）
              </label>
            )}
          </div>
          {error ? <div className="mt-4 rounded-2xl border border-rose-300/20 bg-rose-400/10 px-3 py-2 text-sm text-rose-200">{error}</div> : null}
          <button type="submit" disabled={busy || (registerMode && !brand?.registration_enabled)} className="mt-6 w-full rounded-2xl bg-cyan-300 px-4 py-3 font-semibold text-slate-950 transition hover:bg-cyan-200 disabled:opacity-50">
            {busy ? "处理中..." : registerMode ? "创建并进入交易" : "登录"}
          </button>
          <div className="mt-5 text-center text-sm text-slate-400">
            {registerMode ? (
              <>
                已有账户？<Link className="ml-1 text-cyan-200 hover:text-white" to="/paper/login">返回登录</Link>
              </>
            ) : brand?.registration_enabled ? (
              <>
                还没有账户？<Link className="ml-1 text-cyan-200 hover:text-white" to="/paper/register">免费创建</Link>
              </>
            ) : <span>请使用管理员提供的账户登录</span>}
          </div>
        </form>
      </div>
    </div>
  );
}

function Feature({ title, text }: { title: string; text: string }) {
  return <div className="rounded-2xl border border-white/10 bg-white/5 p-3"><div className="text-cyan-100">{title}</div><div className="mt-1 text-xs text-slate-500">{text}</div></div>;
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return <label className="block"><span className="mb-1.5 block text-xs text-slate-400">{label}</span>{children}</label>;
}
