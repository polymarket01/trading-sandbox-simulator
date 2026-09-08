import type { PaperMarket } from "../types";

export const MARKET_STATUS_LABEL: Record<string, string> = {
  DRAFT: "草稿",
  VALIDATING: "校验中",
  PRE_OPEN: "即将开放",
  TRADING: "交易中",
  PAUSED: "已暂停",
  CANCEL_ONLY: "只可撤单",
  REDUCE_ONLY: "只减仓",
  DELISTING: "下架中",
  DELISTED: "已下架",
};

export const MARKET_STATUS_TONE: Record<string, "buy" | "sell" | "warn" | "neutral" | "info"> = {
  DRAFT: "neutral",
  VALIDATING: "neutral",
  PRE_OPEN: "info",
  TRADING: "buy",
  PAUSED: "warn",
  CANCEL_ONLY: "warn",
  REDUCE_ONLY: "warn",
  DELISTING: "sell",
  DELISTED: "neutral",
};

export const ORDER_STATUS_LABEL: Record<string, string> = {
  new: "新订单",
  partially_filled: "部分成交",
  filled: "已成交",
  canceled: "已撤销",
  rejected: "已拒绝",
  expired: "已过期",
};

export const ORDER_STATUS_TONE: Record<string, "buy" | "sell" | "warn" | "neutral" | "info"> = {
  new: "info",
  partially_filled: "warn",
  filled: "buy",
  canceled: "neutral",
  rejected: "sell",
  expired: "neutral",
};

export const POSITION_SIDE_LABEL: Record<string, string> = {
  long: "做多",
  short: "做空",
  flat: "无仓位",
};

export const RISK_STATUS_LABEL: Record<string, string> = {
  flat: "无仓位",
  ok: "正常",
  watch: "关注",
  warning: "风险预警",
  liquidation_due: "接近强平",
};

export const RISK_STATUS_TONE: Record<string, "buy" | "sell" | "warn" | "neutral" | "info"> = {
  flat: "neutral",
  ok: "buy",
  watch: "info",
  warning: "warn",
  liquidation_due: "sell",
};

export const marketStatusLabel = (status?: string | null) =>
  (status ? MARKET_STATUS_LABEL[status] : undefined) ?? status ?? "—";

export const marketStatusTone = (status?: string | null) =>
  (status ? MARKET_STATUS_TONE[status] : undefined) ?? "neutral";

export const orderStatusLabel = (status?: string | null) =>
  (status ? ORDER_STATUS_LABEL[status] : undefined) ?? status ?? "—";

export const orderStatusTone = (status?: string | null) =>
  (status ? ORDER_STATUS_TONE[status] : undefined) ?? "neutral";

export const positionSideLabel = (side?: string | null) =>
  (side ? POSITION_SIDE_LABEL[side] : undefined) ?? side ?? "—";

export const riskStatusLabel = (status?: string | null) =>
  (status ? RISK_STATUS_LABEL[status] : undefined) ?? status ?? "—";

export const riskStatusTone = (status?: string | null) =>
  (status ? RISK_STATUS_TONE[status] : undefined) ?? "neutral";

export const tifLabel = (tif?: string | null) => {
  if (tif === "gtc") return "GTC";
  if (tif === "ioc") return "IOC";
  if (tif === "post_only") return "Post-only";
  return tif ?? "—";
};

export const typeLabel = (type?: string | null) => {
  if (type === "limit") return "限价";
  if (type === "market") return "市价";
  return type ?? "—";
};

export const isOpenOrder = (status?: string | null) => status === "new" || status === "partially_filled";

export const positionCloseSide = (side?: string | null) => {
  if (side === "long") return "sell";
  if (side === "short") return "buy";
  return undefined;
};

export const derivePositionMargin = (positions: Array<{ isolated_margin?: string }>, usedMargin: number) => {
  const positionMargin = positions.reduce((sum, item) => sum + Number(item.isolated_margin ?? 0), 0);
  const orderMargin = Math.max(0, Number(usedMargin || 0) - positionMargin);
  return { positionMargin, orderMargin };
};

export const marketLeverageOptions = (market?: { max_leverage?: string }) => {
  const max = Math.max(1, Number(market?.max_leverage ?? 20));
  const preset = [1, 2, 3, 5, 10, 20, 50, 100].filter((value) => value <= max);
  if (preset.length === 0) preset.push(max);
  if (preset[preset.length - 1] !== max) preset.push(max);
  return preset;
};

// Never round exposure to display precision: any positive long/short remains visible.
export const isActiveContractPosition = (position: { side: string; quantity: string | number }): boolean =>
  (position.side === "long" || position.side === "short") &&
  Number.isFinite(Number(position.quantity)) && Number(position.quantity) > 0;
