import type { KlineItem } from "../types";
import { sourceLabel } from "./source";

export const KLINE_INTERVALS = ["1s", "5s", "15s", "1m", "5m", "15m", "1h", "1d"];
const CORE_VOLUME_SOURCES = ["bot", "flow", "synthetic_flow", "virtual_volume"];

export const klineSourceLabel = (source: string) => {
  return sourceLabel(source);
};

export const summarizeKlineSources = (items: KlineItem[]) => {
  if (items.length === 0) return "暂无 K 线";
  const counts = new Map<string, number>();
  items.forEach((item) => {
    const source = item.source ?? "unknown";
    counts.set(source, (counts.get(source) ?? 0) + 1);
  });
  return Array.from(counts.entries())
    .sort((a, b) => b[1] - a[1])
    .map(([source, count]) => `${klineSourceLabel(source)} ${count}`)
    .join(" / ");
};

export const summarizeKlineSourceCounts = (counts?: Record<string, number>) => {
  if (!counts) return "暂无 K 线";
  const otherSources = Object.entries(counts)
    .filter(([source, count]) => !CORE_VOLUME_SOURCES.includes(source) && Number(count) > 0)
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .map(([source]) => source);
  return [...CORE_VOLUME_SOURCES, ...otherSources]
    .map((source) => {
      const count = Number(counts[source] ?? 0);
      return `${klineSourceLabel(source)} ${count}`;
    })
    .join(" / ");
};

export const summarizeKlineSourceVolumes = (volumes?: Record<string, string>) => {
  if (!volumes) return "暂无 K 线";
  const otherSources = Object.entries(volumes)
    .filter(([source, value]) => !CORE_VOLUME_SOURCES.includes(source) && Number(value) > 0)
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .map(([source]) => source);
  return [...CORE_VOLUME_SOURCES, ...otherSources]
    .map((source) => {
      const value = Number(volumes[source] ?? 0);
      return `${klineSourceLabel(source)} ${value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })}`;
    })
    .join(" / ");
};

export const hasNonSeedSource = (item: KlineItem) => {
  const counts = item.source_counts;
  if (counts && Object.keys(counts).length > 0) {
    return Object.entries(counts).some(([source, count]) => source !== "bootstrap_seed" && Number(count) > 0);
  }
  return item.source !== "bootstrap_seed";
};

export const sanitizeKlines = (items: KlineItem[], symbol: string, includeSeed: boolean) =>
  items.filter((item) => {
    const prices = [item.open, item.high, item.low, item.close].map(Number);
    if (!prices.every((value) => Number.isFinite(value) && value > 0)) return false;
    if (Number(item.high) < Number(item.low)) return false;
    if (!includeSeed && !hasNonSeedSource(item)) return false;
    const isOldBtcSeedBand =
      symbol === "BTCUSDT" &&
      !includeSeed &&
      !hasNonSeedSource(item) &&
      prices.every((value) => value >= 61000 && value <= 63000);
    return !isOldBtcSeedBand;
  });
