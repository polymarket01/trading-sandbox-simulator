import { useEffect } from "react";
import { useAppStore } from "../store/useAppStore";

export function ToastViewport() {
  const toasts = useAppStore((state) => state.toasts);
  const dismiss = useAppStore((state) => state.dismissToast);

  useEffect(() => {
    const timers = toasts.map((toast) => window.setTimeout(() => dismiss(toast.id), 2800));
    return () => timers.forEach((timer) => clearTimeout(timer));
  }, [dismiss, toasts]);

  return (
    <div className="pointer-events-none fixed right-4 top-4 z-50 space-y-2">
      {toasts.map((toast) => (
        <div key={toast.id} className={`rounded-2xl px-4 py-3 text-sm shadow-glow ${toast.kind === "success" ? "bg-emerald-500 text-slate-950" : "bg-rose-500 text-white"}`}>
          {toast.text}
        </div>
      ))}
    </div>
  );
}
