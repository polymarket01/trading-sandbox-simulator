import { useEffect, useRef, useState, type ReactNode } from "react";
import { useAppStore } from "../store/useAppStore";

export type Tone = "buy" | "sell" | "warn" | "info" | "neutral";

export const toneTextClass: Record<Tone, string> = {
  buy: "text-emerald-300",
  sell: "text-rose-300",
  warn: "text-amber-200",
  info: "text-cyan-200",
  neutral: "text-slate-300",
};

export const toneBgClass: Record<Tone, string> = {
  buy: "bg-emerald-400/12 text-emerald-200",
  sell: "bg-rose-500/12 text-rose-200",
  warn: "bg-amber-400/12 text-amber-100",
  info: "bg-cyan-400/12 text-cyan-100",
  neutral: "bg-white/6 text-slate-300",
};

export const toneBorderClass: Record<Tone, string> = {
  buy: "border-emerald-300/20",
  sell: "border-rose-300/20",
  warn: "border-amber-300/20",
  info: "border-cyan-300/20",
  neutral: "border-white/10",
};

export function StatusPill({ tone = "neutral", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span title={title} className={`ui-status-pill inline-flex shrink-0 items-center gap-1 rounded-lg px-2 py-1 text-xs ${toneBgClass[tone]}`}>
      {children}
    </span>
  );
}

export function Dot({ tone = "neutral" }: { tone?: Tone }) {
  const color = tone === "buy" ? "bg-emerald-400" : tone === "sell" ? "bg-rose-400" : tone === "warn" ? "bg-amber-300" : tone === "info" ? "bg-cyan-300" : "bg-slate-400";
  return <span className={`inline-block h-1.5 w-1.5 rounded-full ${color}`} />;
}

export function SkeletonBlock({ className = "h-4" }: { className?: string }) {
  return <div className={`animate-pulse rounded-lg bg-white/8 ${className}`} />;
}

export function SkeletonRows({ rows = 6, className = "" }: { rows?: number; className?: string }) {
  return (
    <div className={`space-y-3 ${className}`}>
      {Array.from({ length: rows }).map((_, index) => (
        <div key={index} className="flex items-center gap-3">
          <SkeletonBlock className="h-3.5 w-16" />
          <SkeletonBlock className="h-3.5 flex-1" />
          <SkeletonBlock className="h-3.5 w-20" />
        </div>
      ))}
    </div>
  );
}

export function EmptyState({ title, detail, action, compact = false }: { title: string; detail?: string; action?: ReactNode; compact?: boolean }) {
  return (
    <div className={`ui-empty-state flex h-full min-h-0 items-center justify-center rounded-xl border border-dashed border-white/10 bg-slate-950/25 px-4 text-center ${compact ? "py-4" : "py-10"}`}>
      <div>
        <div className="text-sm font-medium text-slate-300">{title}</div>
        {detail ? <div className="mx-auto mt-1.5 max-w-md text-xs leading-5 text-slate-500">{detail}</div> : null}
        {action ? <div className="mt-3 flex justify-center">{action}</div> : null}
      </div>
    </div>
  );
}

export function ErrorState({ message, onRetry, detail }: { message: string; onRetry?: () => void; detail?: string }) {
  return (
    <div className="ui-error-state flex h-full min-h-0 flex-col items-center justify-center rounded-xl border border-rose-300/15 bg-rose-500/5 px-4 py-6 text-center">
      <div className="text-sm font-medium text-rose-200">{message}</div>
      {detail ? <div className="mx-auto mt-1.5 max-w-md break-all text-xs leading-5 text-rose-300/70">{detail}</div> : null}
      {onRetry ? (
        <button type="button" onClick={onRetry} className="ui-retry-button mt-3 rounded-xl bg-rose-400/15 px-3 py-1.5 text-xs text-rose-100 hover:bg-rose-400/25">
          重试
        </button>
      ) : null}
    </div>
  );
}

export function TabBar<T extends string>({ items, value, onChange, className = "" }: { items: { key: T; label: ReactNode; count?: number }[]; value: T; onChange: (key: T) => void; className?: string }) {
  return (
    <div className={`ui-tab-bar flex flex-wrap items-center gap-1 ${className}`}>
      {items.map((item) => (
        <button
          key={item.key}
          type="button"
          onClick={() => onChange(item.key)}
          className={`ui-tab rounded-xl px-3 py-1.5 text-xs transition ${value === item.key ? "bg-cyan-400/15 text-cyan-100" : "text-slate-400 hover:bg-white/8 hover:text-slate-200"}`}
        >
          {item.label}
          {typeof item.count === "number" ? <span className="ml-1.5 font-mono text-[11px] text-slate-400">{item.count}</span> : null}
        </button>
      ))}
    </div>
  );
}

export function SectionHeader({ title, detail, right }: { title: ReactNode; detail?: ReactNode; right?: ReactNode }) {
  return (
    <div className="ui-section-header flex flex-wrap items-center justify-between gap-2">
      <div className="min-w-0">
        <h3 className="font-display text-sm text-slate-100">{title}</h3>
        {detail ? <p className="mt-0.5 text-[11px] text-slate-500">{detail}</p> : null}
      </div>
      {right ? <div className="flex shrink-0 flex-wrap items-center gap-1.5">{right}</div> : null}
    </div>
  );
}

export function ConfirmDialog({
  open,
  title,
  message,
  confirmText = "确认",
  cancelText = "取消",
  danger = false,
  busy = false,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  message: ReactNode;
  confirmText?: string;
  cancelText?: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onCancel]);
  if (!open) return null;
  return (
    <div className="ui-dialog fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm" role="dialog" aria-modal="true">
      <div className="ui-dialog-card w-full max-w-md rounded-3xl border border-white/10 bg-slate-900 p-5 shadow-glow">
        <h3 className="font-display text-base text-white">{title}</h3>
        <div className="mt-2 text-sm leading-6 text-slate-400">{message}</div>
        <div className="mt-5 flex justify-end gap-2">
          <button type="button" onClick={onCancel} disabled={busy} className="ui-dialog-cancel rounded-xl bg-white/6 px-4 py-2 text-sm text-slate-300 hover:bg-white/10 disabled:opacity-50">
            {cancelText}
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={busy}
            className={`ui-dialog-confirm rounded-xl px-4 py-2 text-sm font-semibold disabled:opacity-50 ${danger ? "bg-rose-500 text-white hover:bg-rose-400" : "bg-cyan-300 text-slate-950 hover:bg-cyan-200"}`}
          >
            {busy ? "处理中..." : confirmText}
          </button>
        </div>
      </div>
    </div>
  );
}

export function PriceStat({ label, value, tone = "neutral", large = false, title }: { label: string; value: ReactNode; tone?: Tone; large?: boolean; title?: string }) {
  return (
    <div className="ui-price-stat min-w-0" title={title}>
      <div className="text-[11px] text-slate-500">{label}</div>
      <div className={`mt-0.5 truncate font-mono tabular-nums ${large ? "text-2xl leading-7" : "text-sm"} ${toneTextClass[tone]}`}>{value}</div>
    </div>
  );
}

export function FlashPrice({ tone, className = "", children }: { tone: Tone; className?: string; children: ReactNode }) {
  const previousTone = useRef<Tone | null>(null);
  const [flashClass, setFlashClass] = useState<string>();
  useEffect(() => {
    if (previousTone.current !== null && previousTone.current !== tone) {
      setFlashClass(tone === "buy" ? "bg-emerald-400/12" : tone === "sell" ? "bg-rose-500/12" : undefined);
      const timer = window.setTimeout(() => setFlashClass(undefined), 420);
      return () => window.clearTimeout(timer);
    }
    previousTone.current = tone;
  }, [tone]);
  return <span className={`ui-flash-price rounded-lg px-1.5 py-0.5 transition-colors duration-300 ${flashClass ?? ""} ${className}`}>{children}</span>;
}

export function RelativeTime({ ts, prefix = "" }: { ts?: number; prefix?: string }) {
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  if (!ts) return <span>-</span>;
  const ageMs = Math.max(0, nowMs - ts);
  const label = ageMs < 1000 ? `${(ageMs / 1000).toFixed(1)}s` : `${Math.round(ageMs / 1000)}s`;
  return (
    <span title={new Date(ts).toLocaleString("zh-CN")}>
      {prefix}
      {label}
    </span>
  );
}

export function RefreshButton({ spinning, onClick }: { spinning?: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={spinning}
      className="ui-refresh-button rounded-lg bg-white/6 px-2.5 py-1 text-xs text-slate-300 hover:bg-white/10 disabled:opacity-50"
    >
      {spinning ? "刷新中..." : "刷新"}
    </button>
  );
}

export function useToast() {
  const pushToast = useAppStore((state) => state.pushToast);
  return { pushToast };
}
