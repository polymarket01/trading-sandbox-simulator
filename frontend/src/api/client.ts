import { config } from "../lib/config";

type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

async function request<T>(path: string, method: Method, apiKey?: string, body?: unknown): Promise<T> {
  const response = await fetch(`${config.apiBaseUrl}${path}`, {
    method,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(apiKey ? { "X-API-Key": apiKey } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({ detail: "请求失败" }));
    const detail = data?.detail;
    const message = typeof detail === "string" ? detail : detail?.message ?? detail?.code ?? "请求失败";
    throw new Error(message);
  }
  return response.json();
}

export const api = {
  get: <T>(path: string, apiKey?: string) => request<T>(path, "GET", apiKey),
  post: <T>(path: string, body?: unknown, apiKey?: string) => request<T>(path, "POST", apiKey, body),
  put: <T>(path: string, body?: unknown, apiKey?: string) => request<T>(path, "PUT", apiKey, body),
  patch: <T>(path: string, body?: unknown, apiKey?: string) => request<T>(path, "PATCH", apiKey, body),
  delete: <T>(path: string, apiKey?: string) => request<T>(path, "DELETE", apiKey),
};
