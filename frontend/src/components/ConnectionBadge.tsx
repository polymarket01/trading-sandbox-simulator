import type { StreamStatus } from "../store/useAppStore";

export const STREAM_LABEL: Record<StreamStatus, string> = {
  idle: "未连接",
  connecting: "连接中",
  open: "已连接",
  reconnecting: "重连中",
  fallback: "REST 保底",
  offline: "已断开",
};

export const STREAM_TONE: Record<StreamStatus, "buy" | "sell" | "warn" | "info" | "neutral"> = {
  idle: "neutral",
  connecting: "info",
  open: "buy",
  reconnecting: "warn",
  fallback: "warn",
  offline: "sell",
};

export function ConnectionBadge({ label, status, lastMessageAt }: { label: string; status: StreamStatus; lastMessageAt?: number }) {
  const tone = STREAM_TONE[status];
  const color =
    tone === "buy" ? "bg-emerald-400/12 text-emerald-200"
    : tone === "sell" ? "bg-rose-500/12 text-rose-200"
    : tone === "warn" ? "bg-amber-400/12 text-amber-100"
    : tone === "info" ? "bg-cyan-400/12 text-cyan-100"
    : "bg-white/6 text-slate-400";
  const dotColor =
    tone === "buy" ? "bg-emerald-400"
    : tone === "sell" ? "bg-rose-400"
    : tone === "warn" ? "bg-amber-300 animate-pulse"
    : tone === "info" ? "bg-cyan-300 animate-pulse"
    : "bg-slate-400";
  const age = lastMessageAt ? Math.max(0, Date.now() - lastMessageAt) : null;
  return (
    <span title={lastMessageAt ? `最近数据帧 ${age === null ? "-" : `${Math.round(age / 1000)}s 前`}` : undefined} className={`ui-connection-badge inline-flex items-center gap-1.5 rounded-lg px-2 py-1 text-[11px] ${color}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${dotColor}`} />
      {label} {STREAM_LABEL[status]}
      {age !== null && status !== "open" ? <span className="font-mono text-[10px] opacity-70">{Math.round(age / 1000)}s</span> : null}
    </span>
  );
}
