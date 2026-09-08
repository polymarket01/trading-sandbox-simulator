import { useEffect } from "react";
import { useAppStore } from "../store/useAppStore";

export function ToastViewport() {
  const toasts = useAppStore((state) => state.toasts);
  const dismiss = useAppStore((state) => state.dismissToast);

  useEffect(() => {
    const timers = toasts.map((toast) => window.setTimeout(() => dismiss(toast.id), 3400));
    return () => timers.forEach((timer) => clearTimeout(timer));
  }, [dismiss, toasts]);

  return (
    <div className="ui-toast-viewport pointer-events-none fixed right-4 top-4 z-50 flex w-[min(380px,calc(100vw-2rem))] flex-col gap-2">
      {toasts.map((toast) => (
        <button
          key={toast.id}
          type="button"
          onClick={() => dismiss(toast.id)}
          className={`ui-toast rounded-2xl px-4 py-3 text-left text-sm shadow-glow ${
            toast.kind === "success"
              ? "ui-toast-kind-success"
              : toast.kind === "error"
                ? "ui-toast-kind-error"
                : "ui-toast-kind-info"
          }`}
        >
          {toast.text}
        </button>
      ))}
    </div>
  );
}
