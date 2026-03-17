import { config } from "../lib/config";

type Method = "GET" | "POST" | "PUT" | "DELETE";

async function request<T>(path: string, method: Method, apiKey?: string, body?: unknown): Promise<T> {
  const response = await fetch(`${config.apiBaseUrl}${path}`, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(apiKey ? { "X-API-Key": apiKey } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({ detail: "请求失败" }));
    throw new Error(data.detail ?? "请求失败");
  }
  return response.json();
}

export const api = {
  get: <T>(path: string, apiKey?: string) => request<T>(path, "GET", apiKey),
  post: <T>(path: string, body?: unknown, apiKey?: string) => request<T>(path, "POST", apiKey, body),
  put: <T>(path: string, body?: unknown, apiKey?: string) => request<T>(path, "PUT", apiKey, body),
  delete: <T>(path: string, apiKey?: string) => request<T>(path, "DELETE", apiKey),
};
