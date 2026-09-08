import { lazy, Suspense } from "react";
import { Navigate, Route, Routes, useLocation, useParams } from "react-router-dom";
import { useAppStore } from "./store/useAppStore";
import { AppShell } from "./components/AppShell";

const AdminPage = lazy(() => import("./pages/AdminPage").then((module) => ({ default: module.AdminPage })));
function LegacyBotsRedirect() { const { symbol } = useParams(); return <Navigate replace to={`/admin?section=maker_config${symbol ? `&market=${encodeURIComponent(symbol)}` : ""}`} />; }
const LoginPage = lazy(() => import("./pages/LoginPage").then((module) => ({ default: module.LoginPage })));
const OrderbookMonitorPage = lazy(() => import("./pages/OrderbookMonitorPage").then((module) => ({ default: module.OrderbookMonitorPage })));
const PaperAccountPage = lazy(() => import("./pages/PaperAccountPage").then((module) => ({ default: module.PaperAccountPage })));
const PaperAdminPage = lazy(() => import("./pages/PaperAdminPage").then((module) => ({ default: module.PaperAdminPage })));
const PaperAssetsPage = lazy(() => import("./pages/PaperAssetsPage").then((module) => ({ default: module.PaperAssetsPage })));
const PaperAuthPage = lazy(() => import("./pages/PaperAuthPage").then((module) => ({ default: module.PaperAuthPage })));
const PaperOrdersPage = lazy(() => import("./pages/PaperOrdersPage").then((module) => ({ default: module.PaperOrdersPage })));
const RuntimePage = lazy(() => import("./pages/RuntimePage").then((module) => ({ default: module.RuntimePage })));
const TradePage = lazy(() => import("./pages/TradePage").then((module) => ({ default: module.TradePage })));

const LiquidityMapPage = lazy(() => import("./pages/LiquidityMapPage").then((module) => ({ default: module.LiquidityMapPage })));
const ContractLadderPage = lazy(() => import("./pages/ContractLadderPage").then((module) => ({ default: module.ContractLadderPage })));

export default function App() {
  return (
    <Suspense fallback={<PageLoading />}>
      <Routes>
        <Route path="/ops/liquidity-makers" element={<Navigate to="/admin?section=maker_config" replace />} />
        <Route path="/ops/liquidity-flow" element={<Navigate to="/admin?section=flow_config" replace />} />
        <Route path="/ops/liquidity-map" element={<RequireAuth><AppShell><LiquidityMapPage /></AppShell></RequireAuth>} />
        <Route path="/ops/contract-ladder" element={<RequireAdmin><AppShell><ContractLadderPage /></AppShell></RequireAdmin>} />
        <Route path="/" element={<Navigate to="/login" replace />} />
        <Route path="/login" element={<LoginPage />} />
        <Route path="/paper" element={<Navigate to="/trade/BTCUSDT" replace />} />
        <Route path="/paper/login" element={<PaperAuthPage />} />
        <Route path="/paper/register" element={<PaperAuthPage />} />
        <Route path="/paper/trade" element={<Navigate to="/trade/BTCUSDT" replace />} />
        <Route path="/paper/assets" element={<RequirePaperAuth><AppShell><PaperAssetsPage /></AppShell></RequirePaperAuth>} />
        <Route path="/paper/orders" element={<RequirePaperAuth><AppShell><PaperOrdersPage /></AppShell></RequirePaperAuth>} />
        <Route path="/paper/account" element={<RequirePaperAuth><AppShell><PaperAccountPage /></AppShell></RequirePaperAuth>} />
        <Route path="/paper/admin" element={<RequirePaperAdmin><AppShell><PaperAdminPage /></AppShell></RequirePaperAdmin>} />
        <Route path="/trade" element={<Navigate to="/trade/BTCUSDT" replace />} />
        <Route path="/trade/:symbol" element={<RequireAuth><TradePage /></RequireAuth>} />
        <Route path="/spot/trade" element={<Navigate to="/trade/BTCUSDT" replace />} />
        <Route path="/spot/trade/:symbol" element={<RequireAuth><TradePage /></RequireAuth>} />
        <Route path="/trade-terminal" element={<Navigate to="/trade/BTCUSDT" replace />} />
        <Route path="/trade-terminal/:symbol" element={<Navigate to="/trade/BTCUSDT" replace />} />
        <Route path="/spot/terminal" element={<Navigate to="/trade/BTCUSDT" replace />} />
        <Route path="/spot/terminal/:symbol" element={<Navigate to="/trade/BTCUSDT" replace />} />
        <Route path="/contracts/markets" element={<Navigate to="/trade/BTCUSDT-PERP" replace />} />
        <Route path="/contracts/markets/:symbol" element={<Navigate to="/trade/BTCUSDT-PERP" replace />} />
        <Route path="/contracts/trade" element={<Navigate to="/trade/BTCUSDT-PERP" replace />} />
        <Route path="/contracts/trade/:symbol" element={<RequireAuth><TradePage /></RequireAuth>} />
        <Route path="/contracts/terminal" element={<Navigate to="/trade/BTCUSDT-PERP" replace />} />
        <Route path="/contracts/terminal/:symbol" element={<Navigate to="/trade/BTCUSDT-PERP" replace />} />
        <Route path="/admin" element={<RequireAdmin><AdminPage /></RequireAdmin>} />
        <Route path="/ops/bots" element={<RequireAdmin><LegacyBotsRedirect /></RequireAdmin>} />
        <Route path="/ops/bots/:symbol" element={<RequireAdmin><LegacyBotsRedirect /></RequireAdmin>} />
        <Route path="/ops/orderbook" element={<RequireAdmin><OrderbookMonitorPage /></RequireAdmin>} />
        <Route path="/ops/orderbook/:symbol" element={<RequireAdmin><OrderbookMonitorPage /></RequireAdmin>} />
        <Route path="/ops/runtime" element={<RequireAdmin><RuntimePage /></RequireAdmin>} />
        <Route path="/spot/orderbook" element={<RequireAdmin><OrderbookMonitorPage /></RequireAdmin>} />
        <Route path="/spot/orderbook/:symbol" element={<RequireAdmin><OrderbookMonitorPage /></RequireAdmin>} />
        <Route path="/contracts/orderbook" element={<RequireAdmin><Navigate to="/contracts/orderbook/BTCUSDT-PERP" replace /></RequireAdmin>} />
        <Route path="/contracts/orderbook/:symbol" element={<RequireAdmin><OrderbookMonitorPage /></RequireAdmin>} />
      </Routes>
    </Suspense>
  );
}

function PageLoading() {
  return (
    <div className="min-h-screen px-3 py-3 text-slate-100 md:px-5">
      <div className="mx-auto max-w-[1680px]">
        <div className="panel rounded-xl px-5 py-4">
          <div className="h-6 w-48 animate-pulse rounded-full bg-white/10" />
          <div className="mt-3 h-4 w-72 animate-pulse rounded-full bg-white/5" />
        </div>
      </div>
    </div>
  );
}

function RequireAuth({ children }: { children: React.ReactNode }) {
  const authSession = useAppStore((state) => state.authSession);
  const location = useLocation();
  if (!authSession) {
    return <Navigate to={`/login?next=${encodeURIComponent(location.pathname + location.search)}`} replace />;
  }
  return children;
}

function RequireAdmin({ children }: { children: React.ReactNode }) {
  const authSession = useAppStore((state) => state.authSession);
  const location = useLocation();
  if (!authSession) {
    return <Navigate to={`/login?next=${encodeURIComponent(location.pathname + location.search)}`} replace />;
  }
  if (authSession.role !== "admin") {
    return <Navigate to="/trade/BTCUSDT" replace />;
  }
  return children;
}

function RequirePaperAuth({ children }: { children: React.ReactNode }) {
  const authSession = useAppStore((state) => state.authSession);
  const location = useLocation();
  if (!authSession) {
    return <Navigate to={`/paper/login?next=${encodeURIComponent(location.pathname + location.search)}`} replace />;
  }
  return children;
}

function RequirePaperAdmin({ children }: { children: React.ReactNode }) {
  const authSession = useAppStore((state) => state.authSession);
  const location = useLocation();
  if (!authSession) {
    return <Navigate to={`/paper/login?next=${encodeURIComponent(location.pathname + location.search)}`} replace />;
  }
  if (authSession.role !== "admin") {
    return <Navigate to={`/paper/login?next=${encodeURIComponent(location.pathname + location.search)}&admin_required=1`} replace />;
  }
  return children;
}
