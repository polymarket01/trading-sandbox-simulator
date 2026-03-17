import { Link, NavLink } from "react-router-dom";

export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen px-4 py-4 text-slate-100 md:px-6">
      <div className="mx-auto max-w-[1680px]">
        <header className="mb-4 flex flex-col gap-4 rounded-3xl border border-white/10 bg-slate-900/55 px-5 py-4 shadow-glow backdrop-blur md:flex-row md:items-center md:justify-between">
          <div>
            <Link to="/trade/BTCUSDT" className="font-display text-2xl tracking-tight text-white">
              Spot MM Sandbox
            </Link>
            <p className="mt-1 text-sm text-slate-400">现货做市算法测试沙盒 V1</p>
          </div>
          <nav className="flex flex-wrap gap-2 text-sm">
            {[
              ["/trade/BTCUSDT", "交易台"],
              ["/ops/bots", "机器人监控"],
              ["/admin", "管理页"],
            ].map(([path, label]) => (
              <NavLink
                key={path}
                to={path}
                className={({ isActive }) =>
                  `rounded-full px-4 py-2 transition ${isActive ? "bg-cyan-400/18 text-cyan-200" : "bg-white/5 text-slate-300 hover:bg-white/10"}`
                }
              >
                {label}
              </NavLink>
            ))}
          </nav>
        </header>
        {children}
      </div>
    </div>
  );
}
