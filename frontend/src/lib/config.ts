const pageUrl = typeof window !== "undefined" ? new URL(window.location.href) : null;
const pageHost = pageUrl?.hostname ?? "localhost";
const httpProtocol = pageUrl?.protocol === "https:" ? "https:" : "http:";
const wsProtocol = pageUrl?.protocol === "https:" ? "wss:" : "ws:";

export const config = {
  apiBaseUrl: import.meta.env.VITE_API_BASE_URL ?? `${httpProtocol}//${pageHost}:5174/api/v1`,
  publicWsUrl: import.meta.env.VITE_PUBLIC_WS_URL ?? `${wsProtocol}//${pageHost}:5174/ws/public`,
  privateWsUrl: import.meta.env.VITE_PRIVATE_WS_URL ?? `${wsProtocol}//${pageHost}:5174/ws/private`,
  manualApiKey: import.meta.env.VITE_MANUAL_API_KEY ?? "manual-demo-key",
  manualApiSecret: import.meta.env.VITE_MANUAL_API_SECRET ?? "manual-demo-secret",
  adminApiKey: import.meta.env.VITE_ADMIN_API_KEY ?? "admin-demo-key",
};
