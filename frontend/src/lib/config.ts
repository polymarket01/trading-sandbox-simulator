const pageUrl = typeof window !== "undefined" ? new URL(window.location.href) : null;
const pageHost = pageUrl?.hostname ?? "localhost";
const httpProtocol = pageUrl?.protocol === "https:" ? "https:" : "http:";
const wsProtocol = pageUrl?.protocol === "https:" ? "wss:" : "ws:";
const sameOriginBase = pageUrl ? `${httpProtocol}//${pageHost}${pageUrl.port ? `:${pageUrl.port}` : ""}` : "http://localhost:5174";

export const appBasePath = (import.meta.env.BASE_URL || "/").replace(/\/$/, "");

const parseIntervalMs = (value: unknown, fallback: number) => {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 16 ? parsed : fallback;
};

export const config = {
  apiBaseUrl: import.meta.env.VITE_API_BASE_URL ?? `${sameOriginBase}${appBasePath}/api/v1`,
  publicWsUrl: import.meta.env.VITE_PUBLIC_WS_URL ?? `${sameOriginBase.replace(httpProtocol, wsProtocol)}${appBasePath}/ws/public`,
  privateWsUrl: import.meta.env.VITE_PRIVATE_WS_URL ?? `${sameOriginBase.replace(httpProtocol, wsProtocol)}${appBasePath}/ws/private`,
  multiprocessBaseUrl: import.meta.env.VITE_MULTIPROCESS_BASE_URL ?? `${httpProtocol}//${pageHost}:5184`,
  multiprocessWsUrl: import.meta.env.VITE_MULTIPROCESS_WS_URL ?? `${wsProtocol}//${pageHost}:5184/ws/v2/public`,
  multiprocessApiKey: import.meta.env.VITE_MULTIPROCESS_API_KEY ?? "mp-sandbox-key",
  canonicalApiKey: import.meta.env.VITE_CANONICAL_API_KEY ?? "admin-demo-key",
  orderbookUiIntervalMs: parseIntervalMs(import.meta.env.VITE_ORDERBOOK_UI_INTERVAL_MS, 25),
};
