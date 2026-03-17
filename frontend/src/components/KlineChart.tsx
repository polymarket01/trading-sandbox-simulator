import { useEffect, useMemo, useRef, useState } from "react";
import { ColorType, createChart, HistogramSeries, CandlestickSeries, type IChartApi, type ISeriesApi, type MouseEventParams, type TickMarkType, type Time } from "lightweight-charts";
import { fmt } from "../lib/format";
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

export function KlineChart({
  data,
  interval,
  symbol,
  pricePrecision,
  quantityPrecision,
  priceTick,
}: {
  data: KlineItem[];
  interval: string;
  symbol: string;
  pricePrecision: number;
  quantityPrecision: number;
  priceTick: string;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const hasFittedRef = useRef(false);
  const [hoveredTime, setHoveredTime] = useState<number | null>(null);

  const klineMap = useMemo(
    () => new Map(data.map((item) => [Math.floor(item.open_time / 1000), item])),
    [data],
  );
  const activeKline = (hoveredTime !== null ? klineMap.get(hoveredTime) : undefined) ?? data[data.length - 1];

  useEffect(() => {
    hasFittedRef.current = false;
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "#091521" },
        textColor: "#8da6bb",
      },
      grid: {
        vertLines: { color: "rgba(255,255,255,0.04)" },
        horzLines: { color: "rgba(255,255,255,0.04)" },
      },
      localization: {
        timeFormatter: (time: Time) => formatBeijingTime(Number(time), true),
      },
      rightPriceScale: { borderColor: "rgba(255,255,255,0.08)" },
      timeScale: {
        borderColor: "rgba(255,255,255,0.08)",
        timeVisible: true,
        secondsVisible: interval.endsWith("s"),
        tickMarkFormatter: (time: Time, _tickMarkType: TickMarkType) => formatBeijingTime(Number(time)),
      },
      crosshair: { mode: 1 },
    });
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
      priceScaleId: "",
      color: "rgba(80, 141, 255, 0.35)",
    });
    chart.priceScale("right").applyOptions({ scaleMargins: { top: 0.06, bottom: 0.28 } });
    chart.priceScale("").applyOptions({ scaleMargins: { top: 0.76, bottom: 0.04 } });
    const onCrosshairMove = (param: MouseEventParams<Time>) => {
      if (!param.time) {
        setHoveredTime(null);
        return;
      }
      setHoveredTime(Number(param.time));
    };
    chart.subscribeCrosshairMove(onCrosshairMove);
    chartRef.current = chart;
    return () => {
      chart.unsubscribeCrosshairMove(onCrosshairMove);
      chart.remove();
    };
  }, [symbol, interval, pricePrecision, priceTick]);

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
    if (!candleRef.current || !volumeRef.current) return;
    candleRef.current.setData(
      data.map((item) => ({
        time: Math.floor(item.open_time / 1000) as Time,
        open: Number(item.open),
        high: Number(item.high),
        low: Number(item.low),
        close: Number(item.close),
      })),
    );
    volumeRef.current.setData(
      data.map((item) => ({
        time: Math.floor(item.open_time / 1000) as Time,
        value: Number(item.volume),
        color: Number(item.close) >= Number(item.open) ? "rgba(34,197,155,0.35)" : "rgba(255,109,115,0.35)",
      })),
    );
    if (!hasFittedRef.current && data.length > 0) {
      chartRef.current?.timeScale().fitContent();
      hasFittedRef.current = true;
    }
  }, [data]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-2xl bg-white/5 px-3 py-2 font-mono tabular-nums text-xs text-slate-300">
        <span className="text-slate-500">{activeKline ? formatBeijingTime(Math.floor(activeKline.open_time / 1000), !interval.endsWith("d")) : "--"}</span>
        <span>O {fmt(activeKline?.open, pricePrecision)}</span>
        <span>H {fmt(activeKline?.high, pricePrecision)}</span>
        <span>L {fmt(activeKline?.low, pricePrecision)}</span>
        <span>C {fmt(activeKline?.close, pricePrecision)}</span>
        <span>V {fmt(activeKline?.volume, quantityPrecision)}</span>
        <span>Amt {fmt(activeKline?.quote_volume, 2)}</span>
      </div>
      <div ref={containerRef} className="h-[600px] w-full flex-1" />
    </div>
  );
}
