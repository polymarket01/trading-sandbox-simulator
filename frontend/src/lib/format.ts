export const fmt = (value?: string | number | null, digits = 4) => {
  if (value === undefined || value === null || value === "") return "--";
  const num = Number(value);
  if (Number.isNaN(num)) return String(value);
  return num.toLocaleString("zh-CN", { maximumFractionDigits: digits });
};

export const stepDigits = (value?: string | number | null) => {
  if (value === undefined || value === null || value === "") return 0;
  const text = String(value);
  if (!text.includes(".")) return 0;
  return text.replace(/0+$/, "").split(".")[1]?.length ?? 0;
};

const bjDateTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

const bjTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

const bjDateFormatter = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

export const fmtPct = (value?: string | number | null, digits = 4) => {
  if (value === undefined || value === null || value === "") return "--";
  const num = Number(value);
  if (Number.isNaN(num)) return String(value);
  return `${num.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits })}%`;
};

export const bjTime = (value?: number | string | null) => {
  if (value === undefined || value === null || value === "") return "--";
  return bjTimeFormatter.format(new Date(Number(value)));
};

export const bjDateTime = (value?: number | string | null) => {
  if (value === undefined || value === null || value === "") return "--";
  return bjDateTimeFormatter.format(new Date(Number(value)));
};

export const bjDateShort = (value?: number | string | null) => {
  if (value === undefined || value === null || value === "") return "--";
  return bjDateFormatter.format(new Date(Number(value)));
};

export const sideColor = (side?: string) => {
  if (side === "buy") return "text-emerald-400";
  if (side === "sell") return "text-rose-400";
  return "text-slate-200";
};

export const sideBg = (side?: string) => {
  if (side === "buy") return "bg-emerald-500/12";
  if (side === "sell") return "bg-rose-500/12";
  return "bg-slate-500/12";
};
