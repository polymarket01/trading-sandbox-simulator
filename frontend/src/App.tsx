import { Navigate, Route, Routes } from "react-router-dom";
import { AdminPage } from "./pages/AdminPage";
import { BotsPage } from "./pages/BotsPage";
import { TradePage } from "./pages/TradePage";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/trade/BTCUSDT" replace />} />
      <Route path="/trade/:symbol" element={<TradePage />} />
      <Route path="/admin" element={<AdminPage />} />
      <Route path="/ops/bots" element={<BotsPage />} />
    </Routes>
  );
}
