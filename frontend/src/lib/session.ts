export type AuthSession = {
  user_id: number;
  username: string;
  role: "user" | "manual_user" | "mm_bot" | "admin";
  api_key: string;
  api_secret: string;
  account_epoch?: number;
  account_run_id?: string | null;
};

const SESSION_KEY = "spot-mm-sandbox-session";

export function readSession(): AuthSession | undefined {
  if (typeof window === "undefined") return undefined;
  const raw = window.localStorage.getItem(SESSION_KEY);
  if (!raw) return undefined;
  try {
    const parsed = JSON.parse(raw) as AuthSession;
    if (!parsed.username) return undefined;
    if (parsed.role !== "user" && (!parsed.api_key || !parsed.api_secret)) return undefined;
    return parsed;
  } catch {
    return undefined;
  }
}

export function writeSession(session: AuthSession) {
  window.localStorage.setItem(SESSION_KEY, JSON.stringify(session));
}

export function clearSession() {
  window.localStorage.removeItem(SESSION_KEY);
}
