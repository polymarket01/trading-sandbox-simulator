import { FormEvent, useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { ToastViewport } from "../components/ToastViewport";
import { useAppStore } from "../store/useAppStore";
import type { AuthSession } from "../lib/session";

export function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const authSession = useAppStore((state) => state.authSession);
  const setAuthSession = useAppStore((state) => state.setAuthSession);
  const pushToast = useAppStore((state) => state.pushToast);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const nextPath = useMemo(() => {
    const value = new URLSearchParams(location.search).get("next");
    if (!value || !value.startsWith("/") || value.startsWith("//")) return undefined;
    return value;
  }, [location.search]);

  const targetFor = (session: AuthSession) => {
    const adminOnlyTarget = nextPath?.startsWith("/admin") || nextPath?.startsWith("/ops");
    if (session.role === "admin") return nextPath ?? "/admin";
    if (adminOnlyTarget) return "/trade/BTCUSDT";
    return nextPath ?? "/trade/BTCUSDT";
  };

  useEffect(() => {
    if (!authSession) return;
    navigate(targetFor(authSession), { replace: true });
  }, [authSession, navigate, nextPath]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      setSubmitting(true);
      const response = await api.post<{ user: AuthSession }>("/auth/login", { username, password });
      setAuthSession(response.user);
      pushToast("success", `已登录 ${response.user.username}`);
      navigate(targetFor(response.user), { replace: true });
    } catch (error) {
      pushToast("error", error instanceof Error ? error.message : "登录失败");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="auth-page text-slate-100">
      <ToastViewport />
      <div className="auth-layout mx-auto grid max-w-[980px] gap-6 lg:grid-cols-[1fr_0.9fr]">
        <section className="auth-hero flex flex-col justify-center p-7">
          <div className="text-sm uppercase tracking-[0.24em] text-cyan-200/80">CEX Sandbox</div>
          <h1 className="mt-4 font-display text-4xl text-white">现货模拟交易环境</h1>
          <p className="mt-4 max-w-xl text-sm leading-6 text-slate-300">
            登录后进入对应账户的交易台。机器人仍通过 API Key 独立接入，网页登录只用于人工交易与后台管理。
          </p>
        </section>
        <form onSubmit={submit} className="auth-card panel p-6">
          <h2 className="font-display text-2xl">账户登录</h2>
          <div className="mt-5 space-y-4">
            <label className="block">
              <span className="mb-1 block text-sm text-slate-400">UID / 用户名</span>
              <input
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                className="paper-input"
                autoComplete="username"
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-sm text-slate-400">密码</span>
              <input
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                className="paper-input"
                autoComplete="current-password"
              />
            </label>
          </div>
          <button
            type="submit"
            disabled={submitting}
            className="mt-6 w-full rounded-2xl bg-cyan-300 px-4 py-3 text-sm font-semibold text-slate-950 disabled:opacity-60"
          >
            {submitting ? "登录中..." : "登录"}
          </button>
          <div className="mt-4 rounded-2xl bg-white/5 px-4 py-3 text-xs leading-6 text-slate-400">
            账户由服务端管理员创建。API Key 只用于机器人或程序化接入，网页登录使用 UID / 用户名和密码。
          </div>
        </form>
      </div>
    </div>
  );
}
