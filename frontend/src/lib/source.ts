export const SOURCE_ORDER = ["bot", "flow", "virtual_volume", "user", "bootstrap_seed", "no_trade", "mixed", "unknown"];

export const sourceLabel = (source?: string | null) => {
  if (source === "bot") return "MM 被动";
  if (source === "flow") return "FLOW 主动";
  if (source === "synthetic_flow") return "FLOW 展示";
  if (source === "virtual_volume") return "虚拟成交";
  if (source === "bootstrap_seed") return "初始化";
  if (source === "no_trade") return "无成交";
  if (source === "user") return "用户";
  if (source === "mixed") return "混合";
  if (source === "unknown" || !source) return "未知";
  return source;
};

export const sourceChartColor = (source?: string | null) => {
  if (source === "bot") return "rgba(34,197,155,0.42)";
  if (source === "flow") return "rgba(80,141,255,0.46)";
  if (source === "synthetic_flow") return "rgba(56,189,248,0.44)";
  if (source === "virtual_volume") return "rgba(168,85,247,0.42)";
  if (source === "user") return "rgba(250,204,21,0.42)";
  if (source === "bootstrap_seed") return "rgba(148,163,184,0.30)";
  if (source === "no_trade") return "rgba(100,116,139,0.22)";
  return "rgba(148,163,184,0.32)";
};

export const sourcePillClass = (source?: string | null) => {
  if (source === "bot") return "border-emerald-300/20 bg-emerald-400/10 text-emerald-100";
  if (source === "flow") return "border-blue-300/20 bg-blue-400/10 text-blue-100";
  if (source === "synthetic_flow") return "border-sky-300/20 bg-sky-400/10 text-sky-100";
  if (source === "virtual_volume") return "border-violet-300/20 bg-violet-400/10 text-violet-100";
  if (source === "user") return "border-amber-300/20 bg-amber-400/10 text-amber-100";
  if (source === "bootstrap_seed") return "border-slate-300/15 bg-slate-400/8 text-slate-300";
  if (source === "no_trade") return "border-slate-300/12 bg-slate-400/6 text-slate-400";
  return "border-slate-300/15 bg-slate-400/8 text-slate-300";
};
