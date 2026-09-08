export const fmt = (value?: string | number | null, digits = 4) => {
  if (value === undefined || value === null || value === "") return "-";
  const num = Number(value);
  if (Number.isNaN(num)) return String(value);
  const precision = Math.max(0, Math.min(20, Math.trunc(digits)));
  return num.toLocaleString("zh-CN", { minimumFractionDigits: precision, maximumFractionDigits: precision });
};

export const fmtQuantity = (value?: string | number | null, digits = 4, maxDigits = 8) => {
  if (value === undefined || value === null || value === "") return "-";
  const num = Number(value);
  if (Number.isNaN(num)) return String(value);
  const precision = Math.max(0, Math.min(20, Math.trunc(digits)));
  const formatted = num.toLocaleString("zh-CN", { minimumFractionDigits: precision, maximumFractionDigits: precision });
  if (num !== 0 && Number(formatted.replace(/,/g, "")) === 0) {
    const expandedPrecision = Math.max(precision + 1, Math.min(20, Math.trunc(maxDigits)));
    return num.toLocaleString("zh-CN", {
      minimumFractionDigits: Math.min(expandedPrecision, 20),
      maximumFractionDigits: Math.min(expandedPrecision, 20),
    });
  }
  return formatted;
};

export const stepDigits = (value?: string | number | null): number | undefined => {
  if (value === undefined || value === null || value === "") return undefined;
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
  if (value === undefined || value === null || value === "") return "-";
  const num = Number(value);
  if (Number.isNaN(num)) return String(value);
  return `${num.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits })}%`;
};

export const bjTime = (value?: number | string | null) => {
  if (value === undefined || value === null || value === "") return "-";
  return bjTimeFormatter.format(new Date(Number(value)));
};

export const bjDateTime = (value?: number | string | null) => {
  if (value === undefined || value === null || value === "") return "-";
  return bjDateTimeFormatter.format(new Date(Number(value)));
};

export const bjDateShort = (value?: number | string | null) => {
  if (value === undefined || value === null || value === "") return "-";
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

export const fmtCompact = (value?: string | number | null, digits = 2) => {
  if (value === undefined || value === null || value === "") return "-";
  const num = Number(value);
  if (Number.isNaN(num)) return String(value);
  const abs = Math.abs(num);
  const precision = Math.max(0, Math.min(20, Math.trunc(digits)));
  if (abs >= 1e9) return `${(num / 1e9).toLocaleString("zh-CN", { minimumFractionDigits: precision, maximumFractionDigits: precision })}B`;
  if (abs >= 1e6) return `${(num / 1e6).toLocaleString("zh-CN", { minimumFractionDigits: precision, maximumFractionDigits: precision })}M`;
  if (abs >= 1e4) return `${(num / 1e3).toLocaleString("zh-CN", { minimumFractionDigits: precision, maximumFractionDigits: precision })}K`;
  return num.toLocaleString("zh-CN", { minimumFractionDigits: Math.min(precision, Math.trunc(digits)), maximumFractionDigits: Math.min(precision, Math.trunc(digits)) });
};

export const signedNumber = (value?: string | number | null, digits = 2) => {
  if (value === undefined || value === null || value === "") return "-";
  const num = Number(value);
  if (Number.isNaN(num)) return String(value);
  const text = num.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return num > 0 ? `+${text}` : text;
};
