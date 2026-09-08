import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ColorType, createChart, HistogramSeries, CandlestickSeries, type IChartApi, type ISeriesApi, type LogicalRange, type MouseEventParams, type TickMarkType, type Time } from "lightweight-charts";
import { fmt } from "../lib/format";
import { SOURCE_ORDER, sourceLabel, sourcePillClass } from "../lib/source";
import type { KlineItem } from "../types";

const beijingDateTime = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

const beijingAxisTime = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

const formatBeijingTime = (value: number, withSeconds = false) => {
  const formatter = withSeconds ? beijingDateTime : beijingAxisTime;
  return formatter.format(new Date(value * 1000));
};

type KlineDensity = "dense" | "normal" | "loose";
type KlineTimeMode = "compact" | "continuous";

const densityItems: { key: KlineDensity; label: string }[] = [
  { key: "dense", label: "密" },
  { key: "normal", label: "常" },
  { key: "loose", label: "疏" },
];

const timeModeItems: { key: KlineTimeMode; label: string }[] = [
  { key: "compact", label: "压缩空档" },
  { key: "continuous", label: "真实时间" },
];

const intervalMsByKey: Record<string, number> = {
  "1s": 1000,
  "5s": 5000,
  "15s": 15000,
  "1m": 60000,
  "5m": 300000,
  "15m": 900000,
  "1h": 3600000,
  "4h": 14400000,
  "1d": 86400000,
};

const MAX_EMPTY_KLINES_PER_RENDER = 20000;
const KLINE_TIME_MODE_STORAGE_KEY = "kline_time_mode_v2";

const baseVisibleBars: Record<string, number> = {
  "1s": 180,
  "5s": 150,
  "15s": 130,
  "1m": 112,
  "5m": 120,
  "15m": 120,
  "1h": 120,
  "1d": 88,
};

const densityMultiplier: Record<KlineDensity, number> = {
  dense: 1.35,
  normal: 1,
  loose: 0.72,
};

const densityBarSpacing: Record<KlineDensity, number> = {
  dense: 4.8,
  normal: 7,
  loose: 10,
};

const getVisibleBars = (interval: string, density: KlineDensity) =>
  Math.max(30, Math.round((baseVisibleBars[interval] ?? 112) * densityMultiplier[density]));

const getRightOffset = (interval: string) => (interval.endsWith("s") ? 8 : 6);
const getGapBreakMs = (interval: string) => {
  const intervalMs = intervalMsByKey[interval];
  return intervalMs ? intervalMs * 3 : 180000;
};
const PRICE_PANE_INDEX = 0;
const CORE_DISPLAY_SOURCES = ["bot", "flow", "virtual_volume"];

const emptyKline = (previous: KlineItem, openTime: number, intervalMs: number): KlineItem => ({
  open_time: openTime,
  close_time: openTime + intervalMs - 1,
  open: previous.close,
  high: previous.close,
  low: previous.close,
  close: previous.close,
  volume: "0",
  quote_volume: "0",
  trade_count: 0,
  source: "no_trade",
  source_counts: {},
  source_volumes: {},
  source_quote_volumes: {},
  is_closed: true,
});

const fillKlineTimeGaps = (items: KlineItem[], interval: string) => {
  const intervalMs = intervalMsByKey[interval];
  if (!intervalMs || items.length < 2) return items;
  // Bound the displayed time window, never compress gaps inside it.
  const cutoff = items[items.length - 1].open_time - MAX_EMPTY_KLINES_PER_RENDER * intervalMs;
  const visible = items.filter((item) => item.open_time >= cutoff);
  const filled: KlineItem[] = [];
  for (const item of visible) {
    const previous = filled[filled.length - 1];
    if (previous) {
      for (let time = previous.open_time + intervalMs; time < item.open_time; time += intervalMs) {
        filled.push(emptyKline(previous, time, intervalMs));
      }
    }
    filled.push(item);
  }
  return filled;
};

const getTailSegmentStartIndex = (items: KlineItem[], interval: string) => {
  if (items.length <= 1) return 0;
  const gapBreakMs = getGapBreakMs(interval);
  for (let index = items.length - 1; index > 0; index -= 1) {
    if (items[index].open_time - items[index - 1].open_time > gapBreakMs) return index;
  }
  return 0;
};

const hasKlineActivity = (item: KlineItem) =>
  !["no_trade", "carried_forward"].includes(item.source ?? "") && (Number(item.volume) > 0 || Number(item.quote_volume) > 0 || Number(item.trade_count) > 0);

const getLatestActivityStartIndex = (items: KlineItem[], interval: string) => {
  if (items.length <= 1) return 0;
  const gapBreakMs = getGapBreakMs(interval);
  let lastActiveIndex = -1;
  for (let index = items.length - 1; index >= 0; index -= 1) {
    if (hasKlineActivity(items[index])) {
      lastActiveIndex = index;
      break;
    }
  }
  if (lastActiveIndex < 0) return getTailSegmentStartIndex(items, interval);

  for (let index = lastActiveIndex; index > 0; index -= 1) {
    if (!hasKlineActivity(items[index - 1])) return index;
    if (items[index].open_time - items[index - 1].open_time > gapBreakMs) return index;
  }
  return 0;
};

const initialTimeMode = (): KlineTimeMode => {
  if (typeof window === "undefined") return "continuous";
  return window.localStorage.getItem(KLINE_TIME_MODE_STORAGE_KEY) === "compact" ? "compact" : "continuous";
};

const orderedSourceKeys = (maps: Array<Record<string, unknown> | undefined>) => {
  const seen = new Set<string>();
  SOURCE_ORDER.forEach((source) => {
    if (maps.some((map) => map && Object.prototype.hasOwnProperty.call(map, source))) seen.add(source);
  });
  maps.forEach((map) => {
    Object.keys(map ?? {}).forEach((source) => seen.add(source));
  });
  return Array.from(seen);
};

const sourceNumber = (values: Record<string, string | number> | undefined, source: string) => Number(values?.[source] ?? 0);

export function KlineChart({
  data,
  interval,
  symbol,
  pricePrecision,
  quantityPrecision,
  priceTick,
  compact = false,
}: {
  data: KlineItem[];
  interval: string;
  symbol: string;
  pricePrecision: number;
  quantityPrecision: number;
  priceTick: string;
  compact?: boolean;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const hasFittedRef = useRef(false);
  const seriesKeyRef = useRef("");
  const dataLengthRef = useRef(0);
  const tailSegmentStartRef = useRef(0);
  const followLatestRef = useRef(true);
  const lastRangeRef = useRef<LogicalRange | null>(null);
  const lastBarTimeRef = useRef<number | null>(null);
  const programmaticRangeRef = useRef(false);
  const programmaticTimerRef = useRef<number | null>(null);
  const [hoveredTime, setHoveredTime] = useState<number | null>(null);
  const [followLatest, setFollowLatest] = useState(true);
  const [density, setDensity] = useState<KlineDensity>("normal");
  const [timeMode, setTimeMode] = useState<KlineTimeMode>(initialTimeMode);

  const chartData = useMemo(
    () => (timeMode === "continuous" ? fillKlineTimeGaps(data, interval) : data),
    [data, interval, timeMode],
  );

  const klineMap = useMemo(
    () => new Map(chartData.map((item) => [Math.floor(item.open_time / 1000), item])),
    [chartData],
  );
  const activeKline = (hoveredTime !== null ? klineMap.get(hoveredTime) : undefined) ?? chartData[chartData.length - 1];
  const baseAsset = useMemo(() => {
    const marketCode = symbol.split("-")[0];
    return marketCode.endsWith("USDT") ? marketCode.slice(0, -4) : "";
  }, [symbol]);

  const candleData = useMemo(
    () =>
      chartData.map((item) => !hasKlineActivity(item) ? { time: Math.floor(item.open_time / 1000) as Time } : ({
        time: Math.floor(item.open_time / 1000) as Time,
        open: Number(item.open),
        high: Number(item.high),
        low: Number(item.low),
        close: Number(item.close),
      })),
    [chartData],
  );
  const volumeData = useMemo(
    () =>
      chartData.map((item) => !hasKlineActivity(item) ? { time: Math.floor(item.open_time / 1000) as Time } : ({
        time: Math.floor(item.open_time / 1000) as Time,
        value: Number(item.volume),
        color: Number(item.volume) <= 0
          ? "rgba(100,116,139,0.20)"
          : Number(item.close) >= Number(item.open)
            ? "rgba(34,197,155,0.46)"
            : "rgba(255,109,115,0.48)",
      })),
    [chartData],
  );
  const activeSourceEntries = useMemo(() => {
    if (!activeKline) return [];
    const keys = [
      ...CORE_DISPLAY_SOURCES,
      ...orderedSourceKeys([activeKline.source_volumes, activeKline.source_quote_volumes, activeKline.source_counts]).filter(
        (source) => !CORE_DISPLAY_SOURCES.includes(source),
      ),
    ];
    const volumeTotal = keys.reduce((sum, source) => sum + sourceNumber(activeKline.source_volumes, source), 0);
    const quoteTotal = keys.reduce((sum, source) => sum + sourceNumber(activeKline.source_quote_volumes, source), 0);
    const countTotal = keys.reduce((sum, source) => sum + sourceNumber(activeKline.source_counts, source), 0);
    const denominator = quoteTotal || volumeTotal || countTotal || 0;
    return keys
      .map((source) => {
        const volume = sourceNumber(activeKline.source_volumes, source);
        const quoteVolume = sourceNumber(activeKline.source_quote_volumes, source);
        const count = sourceNumber(activeKline.source_counts, source);
        const value = quoteVolume || volume || count;
        return {
          source,
          volume,
          quoteVolume,
          count,
          pct: denominator > 0 ? (value / denominator) * 100 : 0,
        };
      })
      .filter((entry) => CORE_DISPLAY_SOURCES.includes(entry.source) || entry.volume > 0 || entry.quoteVolume > 0 || entry.count > 0)
      .sort((a, b) => {
        const orderA = SOURCE_ORDER.indexOf(a.source);
        const orderB = SOURCE_ORDER.indexOf(b.source);
        return (orderA < 0 ? 99 : orderA) - (orderB < 0 ? 99 : orderB);
      });
  }, [activeKline]);

  const setFollowLatestMode = useCallback((value: boolean) => {
    followLatestRef.current = value;
    setFollowLatest(value);
  }, []);

  const markProgrammaticRange = useCallback(() => {
    programmaticRangeRef.current = true;
    if (programmaticTimerRef.current !== null) window.clearTimeout(programmaticTimerRef.current);
    programmaticTimerRef.current = window.setTimeout(() => {
      programmaticRangeRef.current = false;
      programmaticTimerRef.current = null;
    }, 0);
  }, []);

  const applyLatestWindow = useCallback(
    (densityOverride?: KlineDensity) => {
      const chart = chartRef.current;
      const itemCount = dataLengthRef.current;
      if (!chart || itemCount <= 0) return;

      const nextDensity = densityOverride ?? density;
      const visibleBars = getVisibleBars(interval, nextDensity);
      const rightOffset = getRightOffset(interval);
      const lastIndex = itemCount - 1;
      const tailStartIndex = Math.min(Math.max(tailSegmentStartRef.current, 0), lastIndex);
      const tailBars = lastIndex - tailStartIndex + 1;
      const latestWindowBars = Math.min(
        visibleBars,
        Math.max(Math.min(visibleBars, 18), tailBars + rightOffset * 2),
      );
      const fromIndex = Math.max(tailStartIndex, Math.max(0, lastIndex - latestWindowBars + 1));

      markProgrammaticRange();
      chart.timeScale().applyOptions({
        rightOffset,
        barSpacing: densityBarSpacing[nextDensity],
        minBarSpacing: 2,
        maxBarSpacing: 22,
        rightBarStaysOnScroll: true,
        shiftVisibleRangeOnNewBar: true,
        lockVisibleTimeRangeOnResize: true,
      });
      chart.timeScale().setVisibleLogicalRange({
        from: fromIndex,
        to: lastIndex + rightOffset,
      });
    },
    [density, interval, markProgrammaticRange],
  );

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      autoSize: true,
      layout: {
          background: { type: ColorType.Solid, color: "#0b1517" },
          textColor: "#849a96",
        panes: {
          enableResize: false,
          separatorColor: "rgba(181,222,214,0.12)",
          separatorHoverColor: "rgba(53,214,178,0.22)",
        },
      },
      grid: {
        vertLines: { color: "rgba(181,222,214,0.045)" },
        horzLines: { color: "rgba(181,222,214,0.045)" },
      },
      localization: {
        timeFormatter: (time: Time) => formatBeijingTime(Number(time), true),
      },
      rightPriceScale: { borderColor: "rgba(255,255,255,0.08)" },
      timeScale: {
        borderColor: "rgba(255,255,255,0.08)",
        timeVisible: true,
        secondsVisible: interval.endsWith("s"),
        rightOffset: getRightOffset(interval),
        barSpacing: densityBarSpacing.normal,
        minBarSpacing: 2,
        maxBarSpacing: 22,
        rightBarStaysOnScroll: true,
        shiftVisibleRangeOnNewBar: true,
        lockVisibleTimeRangeOnResize: true,
        tickMarkFormatter: (time: Time, _tickMarkType: TickMarkType) => formatBeijingTime(Number(time)),
      },
      crosshair: { mode: 1 },
    });
    const pricePane = chart.panes()[PRICE_PANE_INDEX];
    const volumePane = chart.addPane();
    pricePane?.setStretchFactor(4);
    volumePane.setStretchFactor(1.35);

    candleRef.current = chart.addSeries(CandlestickSeries, {
      upColor: "#22c59b",
      borderUpColor: "#22c59b",
      wickUpColor: "#22c59b",
      downColor: "#ff6d73",
      borderDownColor: "#ff6d73",
      wickDownColor: "#ff6d73",
      priceFormat: {
        type: "price",
        precision: pricePrecision,
        minMove: Number(priceTick),
      },
    });
    volumeRef.current = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      color: "rgba(80, 141, 255, 0.35)",
      priceLineVisible: false,
      lastValueVisible: true,
    }, volumePane.paneIndex());
    chart.priceScale("right", PRICE_PANE_INDEX).applyOptions({ scaleMargins: { top: 0.06, bottom: 0.08 } });
    chart.priceScale("right", volumePane.paneIndex()).applyOptions({
      scaleMargins: { top: 0.08, bottom: 0.08 },
      borderColor: "rgba(255,255,255,0.08)",
    });
    const onCrosshairMove = (param: MouseEventParams<Time>) => {
      if (!param.time) {
        setHoveredTime(null);
        return;
      }
      setHoveredTime(Number(param.time));
    };
    const onVisibleLogicalRangeChange = (range: LogicalRange | null) => {
      if (!range) return;
      const previous = lastRangeRef.current;
      lastRangeRef.current = range;
      if (programmaticRangeRef.current || dataLengthRef.current <= 0) return;
      if (previous && Math.abs(previous.from - range.from) < 0.001 && Math.abs(previous.to - range.to) < 0.001) return;
      const zooming = previous && Math.abs((range.to - range.from) - (previous.to - previous.from)) > 0.1;
      // Zoom changes bar spacing; only horizontal navigation away from the
      // latest candle enters history mode. Dragging back resumes following.
      if (zooming && followLatestRef.current) return;
      setFollowLatestMode(range.to >= dataLengthRef.current - 1.5);
    };
    chart.subscribeCrosshairMove(onCrosshairMove);
    chart.timeScale().subscribeVisibleLogicalRangeChange(onVisibleLogicalRangeChange);
    chartRef.current = chart;
    return () => {
      chart.unsubscribeCrosshairMove(onCrosshairMove);
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(onVisibleLogicalRangeChange);
      if (programmaticTimerRef.current !== null) window.clearTimeout(programmaticTimerRef.current);
      chart.remove();
    };
  }, [setFollowLatestMode]);

  useEffect(() => {
    chartRef.current?.applyOptions({
      timeScale: {
        secondsVisible: interval.endsWith("s"),
        tickMarkFormatter: (time: Time, _tickMarkType: TickMarkType) => formatBeijingTime(Number(time)),
      },
    });
    chartRef.current?.timeScale().applyOptions({
      shiftVisibleRangeOnNewBar: followLatest,
    });
  }, [followLatest, interval]);

  useEffect(() => {
    candleRef.current?.applyOptions({
      priceFormat: {
        type: "price",
        precision: pricePrecision,
        minMove: Number(priceTick),
      },
    });
  }, [pricePrecision, priceTick]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !candleRef.current || !volumeRef.current) return;
    const nextKey = `${symbol}:${interval}:${timeMode}`;
    const resetSeries = seriesKeyRef.current !== nextKey;
    const previousRange = chart.timeScale().getVisibleLogicalRange();
    const previousLastIndex = dataLengthRef.current - 1;
    const previousLastTime = lastBarTimeRef.current;
    dataLengthRef.current = chartData.length;
    tailSegmentStartRef.current = timeMode === "continuous" ? 0 : getLatestActivityStartIndex(chartData, interval);
    if (resetSeries) {
      seriesKeyRef.current = nextKey;
      hasFittedRef.current = false;
      setHoveredTime(null);
      setFollowLatestMode(true);
    }

    markProgrammaticRange();
    candleRef.current.setData(candleData);
    volumeRef.current.setData(volumeData);

    if (chartData.length === 0) {
      hasFittedRef.current = false;
      return;
    }

    if (resetSeries || !hasFittedRef.current) {
      applyLatestWindow();
      hasFittedRef.current = true;
    } else if (previousRange) {
      // Preserve the user's zoom and right margin, even with a rolling history
      // window whose length remains fixed while old bars are removed.
      const newLastIndex = chartData.length - 1;
      const retainedIndex = previousLastTime === null ? -1 : chartData.findIndex((item) => item.open_time === previousLastTime);
      const shift = followLatestRef.current ? newLastIndex + Math.max(0, previousRange.to - previousLastIndex) - previousRange.to : retainedIndex >= 0 ? retainedIndex - previousLastIndex : 0;
      markProgrammaticRange();
      chart.timeScale().setVisibleLogicalRange({ from: previousRange.from + shift, to: previousRange.to + shift });
    }
    lastBarTimeRef.current = chartData[chartData.length - 1]?.open_time ?? null;
  }, [applyLatestWindow, candleData, chartData.length, interval, markProgrammaticRange, setFollowLatestMode, symbol, timeMode, volumeData]);

  const goLatest = () => {
    setFollowLatestMode(true);
    const chart = chartRef.current;
    const range = chart?.timeScale().getVisibleLogicalRange();
    if (chart && range) {
      const to = dataLengthRef.current - 1 + getRightOffset(interval);
      markProgrammaticRange();
      chart.timeScale().setVisibleLogicalRange({ from: to - (range.to - range.from), to });
      chart.priceScale("right", PRICE_PANE_INDEX).applyOptions({ autoScale: true });
    } else applyLatestWindow();
  };

  const resetWindow = () => {
    setDensity("normal");
    setFollowLatestMode(true);
    applyLatestWindow("normal");
    chartRef.current?.priceScale("right", PRICE_PANE_INDEX).applyOptions({ autoScale: true });
  };

  const changeDensity = (nextDensity: KlineDensity) => {
    setDensity(nextDensity);
    setFollowLatestMode(true);
    applyLatestWindow(nextDensity);
  };

  const changeTimeMode = (nextMode: KlineTimeMode) => {
    setTimeMode(nextMode);
    window.localStorage.setItem(KLINE_TIME_MODE_STORAGE_KEY, nextMode);
    setFollowLatestMode(true);
  };

  return (
    <div className="hl-kline-root flex h-full min-h-0 flex-col">
      <div className={`hl-kline-meta mb-2 flex flex-wrap items-start justify-between gap-2 ${compact ? "text-[11px]" : "text-xs"}`}>
        <div className="hl-kline-ohlc grid min-h-[36px] min-w-0 flex-1 grid-cols-2 items-center gap-x-3 gap-y-1 rounded-xl bg-white/5 px-3 py-2 font-mono tabular-nums text-slate-300 sm:grid-cols-4 2xl:grid-cols-7">
          <span className="truncate text-slate-500">{activeKline ? formatBeijingTime(Math.floor(activeKline.open_time / 1000), !interval.endsWith("d")) : "-"}</span>
          <span className="truncate">O {fmt(activeKline && !hasKlineActivity(activeKline) ? undefined : activeKline?.open, pricePrecision)}</span>
          <span className="truncate">H {fmt(activeKline && !hasKlineActivity(activeKline) ? undefined : activeKline?.high, pricePrecision)}</span>
          <span className="truncate">L {fmt(activeKline && !hasKlineActivity(activeKline) ? undefined : activeKline?.low, pricePrecision)}</span>
          <span className="truncate">C {fmt(activeKline && !hasKlineActivity(activeKline) ? undefined : activeKline?.close, pricePrecision)}</span>
          <span className="truncate" title="基础币成交量">
            成交量 {fmt(activeKline?.volume, quantityPrecision)}{baseAsset ? ` ${baseAsset}` : ""}
          </span>
          <span className="truncate" title="USDT 成交额">
            成交额 {fmt(activeKline?.quote_volume, 2)} USDT
          </span>
          {activeSourceEntries.length > 0 && (
            <div className="col-span-2 flex min-w-0 flex-wrap items-center gap-1.5 pt-1 sm:col-span-4 2xl:col-span-7">
              {activeSourceEntries.map((entry) => (
                <span
                  key={entry.source}
                  className={`inline-flex max-w-full items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] leading-4 ${sourcePillClass(entry.source)}`}
                  title={`${sourceLabel(entry.source)} · ${fmt(String(entry.quoteVolume || 0), 2)} USDT`}
                >
                  <span className="truncate">{sourceLabel(entry.source)}</span>
                  <span className="font-mono tabular-nums">{fmt(String(entry.volume || 0), quantityPrecision)}</span>
                  <span className="font-mono tabular-nums text-slate-400">{entry.pct.toFixed(0)}%</span>
                </span>
              ))}
            </div>
          )}
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-1.5">
          <span className={`rounded-lg px-2 py-1 ${followLatest ? "bg-emerald-400/12 text-emerald-100" : "bg-amber-400/14 text-amber-100"}`}>
            {followLatest ? "跟随最新" : "查看历史"}
          </span>
          <button type="button" onClick={goLatest} className="rounded-lg bg-white/6 px-2.5 py-1 text-slate-200 hover:bg-white/10">
            最新
          </button>
          <button type="button" onClick={resetWindow} className="rounded-lg bg-white/6 px-2.5 py-1 text-slate-200 hover:bg-white/10">
            重置
          </button>
          <div className="flex overflow-hidden rounded-lg border border-white/8 bg-slate-950/30">
            {densityItems.map((item) => (
              <button
                type="button"
                key={item.key}
                onClick={() => changeDensity(item.key)}
                className={`px-2.5 py-1 ${density === item.key ? "bg-cyan-400/18 text-cyan-100" : "text-slate-400 hover:bg-white/6 hover:text-slate-100"}`}
              >
                {item.label}
              </button>
            ))}
          </div>
          <div className="flex overflow-hidden rounded-lg border border-white/8 bg-slate-950/30">
            {timeModeItems.map((item) => (
              <button
                type="button"
                key={item.key}
                onClick={() => changeTimeMode(item.key)}
                aria-pressed={timeMode === item.key}
                title={item.key === "continuous" ? "无成交时段留空，不绘制虚假价格；最多展示最近 20000 个周期" : "省略缺失周期，时间轴会压缩"}
                className={`px-2.5 py-1 ${timeMode === item.key ? "bg-cyan-400/18 text-cyan-100" : "text-slate-400 hover:bg-white/6 hover:text-slate-100"}`}
              >
                {item.label}
              </button>
            ))}
          </div>
        </div>
      </div>
      <div className="flex shrink-0 items-center justify-end gap-2 px-3 py-1 text-[11px]" aria-label="K线实时显示控制">
        <span className={followLatest ? "text-emerald-300" : "text-amber-300"}>{followLatest ? "跟随最新" : "查看历史 · 数据仍在更新"}</span>
        <button type="button" onClick={goLatest} className="rounded bg-white/5 px-2 py-1 text-slate-200">回到最新</button>
        <button type="button" onClick={() => chartRef.current?.priceScale("right", PRICE_PANE_INDEX).applyOptions({ autoScale: true })} className="rounded bg-white/5 px-2 py-1 text-slate-200" title="恢复价格轴自动适配，不改变时间缩放">自动价格</button>
      </div>
      <p className="px-3 text-[10px] text-slate-500">{timeMode === "continuous" ? "真实时间轴 · 无成交周期留空 · 最多显示最近 20,000 个周期，查看更早数据请切换较大周期" : "已压缩空档 · 横轴不代表等长的实际时间"}</p>
      <div ref={containerRef} className="min-h-0 w-full flex-1" />
    </div>
  );
}
