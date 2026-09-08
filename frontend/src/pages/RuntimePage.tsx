import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { AppShell } from "../components/AppShell";
import { config } from "../lib/config";
import { api } from "../api/client";
import type { MarketDefinition } from "../types";

type Level = [string, string];
type BookFrame = {
  market_id: string;
  stream_epoch: string;
  stream_id: string;
  seq: number;
  published_at: number;
  bids: Level[];
  asks: Level[];
  engine_version?: number;
};
type RuntimePayload = Record<string, any>;
type RuntimeSource = "multiprocess_v2" | "canonical_v1";

type RuntimeMarket = { id: string; label: string; product: string };
const DEFAULT_MARKETS: RuntimeMarket[] = [
  { id: "BTCUSDT", label: "Spot", product: "现货" },
  { id: "BTCUSDT-PERP", label: "Perp", product: "USDT 永续" },
];
const RESOURCE_ROLES = [
  ["supervisor", "Supervisor"],
  ["gateway", "Gateway"],
  ["feed", "Feed"],
  ["spot_engine", "Spot Worker"],
  ["perp_engine", "Perp Worker"],
  ["sampler", "Sampler"],
  ["metrics", "Metrics"],
] as const;

const num = (value: unknown) => {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
};

const fmtNumber = (value: unknown, digits = 2) => {
  const parsed = num(value);
  return parsed === null ? "-" : parsed.toLocaleString("en-US", { maximumFractionDigits: digits, minimumFractionDigits: 0 });
};

const fmtBytes = (value: unknown) => {
  const parsed = num(value);
  if (parsed === null) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let index = 0;
  let current = parsed;
  while (current >= 1024 && index < units.length - 1) {
    current /= 1024;
    index += 1;
  }
  return `${current.toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
};

const fmtTime = (value: unknown) => {
  const parsed = num(value);
  if (!parsed) return "-";
  return new Date(parsed).toLocaleTimeString("zh-TW", { hour12: false });
};

const fmtAge = (value: unknown) => {
  const parsed = num(value);
  return parsed === null ? "-" : `${Math.max(0, Math.round(parsed))} ms`;
};

const tone = (status: unknown) => {
  const normalized = String(status || "UNKNOWN").toUpperCase();
  if (["HEALTHY", "READY", "ALIVE", "FRESH", "ACKED", "RUNNING"].includes(normalized)) return "text-emerald-300";
  if (["DEGRADED", "STALE", "NOT_READY", "UNKNOWN"].includes(normalized)) return "text-amber-300";
  if (["HALTED", "FAILED", "STOPPED", "ERROR"].includes(normalized)) return "text-rose-300";
  return "text-slate-300";
};

const shorten = (value: unknown, length = 18) => {
  const text = String(value || "-");
  return text.length > length ? `${text.slice(0, length)}…` : text;
};

const sameContent = (left: BookFrame, right: BookFrame) =>
  JSON.stringify([left.stream_epoch, left.stream_id, left.seq, left.bids, left.asks, left.engine_version]) ===
  JSON.stringify([right.stream_epoch, right.stream_id, right.seq, right.bids, right.asks, right.engine_version]);

const applyLevels = (current: Level[], changes: Level[], reverse: boolean) => {
  const levels = new Map(current.map(([price, quantity]) => [String(price), String(quantity)]));
  changes.forEach(([price, quantity]) => {
    if (Number(quantity) === 0) levels.delete(String(price));
    else levels.set(String(price), String(quantity));
  });
  return [...levels.entries()].sort((left, right) => {
    const delta = Number(left[0]) - Number(right[0]);
    return reverse ? -delta : delta;
  }) as Level[];
};

const parseBook = (payload: any, previous?: BookFrame): BookFrame | null => {
  const market = String(payload?.market_id || "").toUpperCase();
  const epoch = String(payload?.stream_epoch || "");
  const streamId = String(payload?.stream_id || "");
  const seq = Number(payload?.seq);
  if (!market || !epoch || !streamId || !Number.isInteger(seq) || seq < 0) return null;
  const bids = Array.isArray(payload?.bids) ? payload.bids.map((item: any) => [String(item?.[0]), String(item?.[1])] as Level) : [];
  const asks = Array.isArray(payload?.asks) ? payload.asks.map((item: any) => [String(item?.[0]), String(item?.[1])] as Level) : [];
  if (!previous || previous.stream_epoch !== epoch) {
    if (payload?.type === "delta") return null;
    return { market_id: market, stream_epoch: epoch, stream_id: streamId, seq, published_at: Number(payload.published_at) || Date.now(), bids, asks, engine_version: payload.engine_version };
  }
  if (previous.stream_id !== streamId || seq < previous.seq) return null;
  if (seq === previous.seq) {
    const candidate = { ...previous, bids, asks, stream_id: streamId, stream_epoch: epoch, engine_version: payload.engine_version };
    return sameContent(previous, candidate) ? previous : null;
  }
  if (payload?.type === "delta") {
    const previousSeq = Number(payload?.previous_seq);
    if (previousSeq !== previous.seq || seq !== previous.seq + 1) return null;
    return {
      market_id: market,
      stream_epoch: epoch,
      stream_id: streamId,
      seq,
      published_at: Number(payload.published_at) || Date.now(),
      bids: applyLevels(previous.bids, bids, true),
      asks: applyLevels(previous.asks, asks, false),
      engine_version: payload.engine_version,
    };
  }
  return { market_id: market, stream_epoch: epoch, stream_id: streamId, seq, published_at: Number(payload.published_at) || Date.now(), bids, asks, engine_version: payload.engine_version };
};

const canonicalStatus = (value: unknown, fallback = "UNKNOWN") => {
  const normalized = String(value || fallback).toUpperCase();
  if (normalized === "RUNNING" || normalized === "OK") return "HEALTHY";
  if (normalized === "FRESH") return "FRESH";
  if (normalized === "WARN") return "DEGRADED";
  return normalized;
};

const buildCanonicalRuntime = (runtimeHealth: RuntimePayload, systemStatus: RuntimePayload, dumps: RuntimePayload[], markets: RuntimeMarket[]) => {
  const now = Date.now();
  const dumpByMarket = Object.fromEntries(dumps.map((item) => [String(item?.symbol || "").toUpperCase(), item]));
  const statusBooks = Array.isArray(systemStatus?.books) ? systemStatus.books : [];
  const wsMetrics = systemStatus?.websocket?.metrics || systemStatus?.ws || {};
  const exchange = systemStatus?.exchange || {};
  const history = runtimeHealth?.history_storage || runtimeHealth?.storage || {};
  const storageState = canonicalStatus(history.status, "HEALTHY");
  const wsDegraded = Number(wsMetrics.send_timeouts || 0) > 0 || Number(wsMetrics.send_failures || 0) > 0 || Number(wsMetrics.dropped_sockets || 0) > 0;
  const warnings = Array.isArray(systemStatus?.warnings) ? systemStatus.warnings : [];
  const marketDataHealth: RuntimePayload = {};
  const bookFreshness: RuntimePayload = {};
  const engineHealth: RuntimePayload = {};
  const binanceAge: RuntimePayload = {};
  const klineFinalized: RuntimePayload = {};

  markets.forEach((market) => {
    const dump = dumpByMarket[market.id] || {};
    const reference = dump?.liquidity_metrics || {};
    const isSpot = market.product === "现货";
    const price = reference.external_mid ?? reference.binance_mid_price ?? reference.fair_price;
    const makerReference = dump?.reference_price || {};
    const referencePrice = price ?? makerReference.mid;
    const age = reference.reference_age_ms ?? reference.binance_bbo_age_ms ?? reference.external_stale_ms ?? makerReference.age_ms;
    const updatedAt = Number(reference.reference_updated_at_ms || makerReference.updated_at) || (num(age) !== null ? now - Number(age) : 0);
    const referenceState = canonicalStatus(reference.reference_status ?? makerReference.status, "UNKNOWN");
    const bookTimestamp = Number(dump?.last_metrics_ts) || Number(makerReference.updated_at) || 0;
    const bookAge = Math.max(0, now - bookTimestamp);
    const book = statusBooks.find((item: RuntimePayload) => String(item?.symbol || "").toUpperCase() === market.id);
    marketDataHealth[market.id] = {
      price: referencePrice,
      status: referenceState,
      updated_at: updatedAt,
      binance_age_ms: num(age) !== null ? Number(age) : null,
      source: reference.reference_source || reference.binance_bbo_source || makerReference.source || "binance",
    };
    binanceAge[market.id] = num(age) !== null ? Number(age) : null;
    bookFreshness[market.id] = {
      status: !bookTimestamp ? "UNKNOWN" : bookAge <= (isSpot ? 2_500 : 2_500) ? "FRESH" : "STALE",
      last_business_progress_at: bookTimestamp,
      age_ms: bookAge,
    };
    engineHealth[market.id] = {
      engine_status: Number(book?.open_orders || 0) > 0 ? "HEALTHY" : "RUNNING",
      mode: "canonical_single_process",
    };
    const commitAt = Date.parse(String(history.last_commit_at || ""));
    klineFinalized[market.id] = Number.isFinite(commitAt) ? commitAt : null;
  });

  const effectiveStatus = runtimeHealth?.status === "HEALTHY" && !wsDegraded ? "HEALTHY" : "DEGRADED";
  const processPid = systemStatus?.process?.pid;
  const processTree = {
    collector: "canonical-v1-compatibility (single process)",
    topology: "canonical_single_process",
    cpu_count: undefined,
    tree_cpu_single_core_pct: undefined,
    tree_cpu_machine_pct: undefined,
    tree_rss_bytes: undefined,
    tree_physical_footprint_bytes: undefined,
    tree_swap_bytes: undefined,
    thread_count: undefined,
    components: {
      supervisor: { status: "NOT_EXPOSED" },
      gateway: { status: "HEALTHY", alive: true, pid: processPid, rss_bytes: Number(systemStatus?.process?.rss_mb) * 1024 * 1024 },
      feed: { status: "IN_PROCESS" },
      spot_engine: { status: "IN_PROCESS" },
      perp_engine: { status: "IN_PROCESS" },
      sampler: { status: storageState === "HEALTHY" ? "IN_PROCESS" : storageState },
      metrics: { status: "NOT_EXPOSED" },
    },
  };
  const degradedReasons = warnings.map((detail: unknown, index: number) => ({
    code: `CANONICAL_WARNING_${index + 1}`,
    detail: String(detail),
    affects_readiness: false,
  }));

  return {
    health: {
      source: "canonical_v1",
      run_id: runtimeHealth?.run_id,
      uptime_seconds: systemStatus?.uptime_seconds,
      effective_status: effectiveStatus,
      liveness: { status: "ALIVE" },
      readiness: { status: runtimeHealth?.status === "HEALTHY" ? "READY" : "NOT_READY" },
      process_tree: processTree,
      market_data_health: marketDataHealth,
      book_freshness: bookFreshness,
      engine_health: engineHealth,
      sampler_health: { status: storageState, sampling_drops: Number(history.sampling_drops || 0) },
      storage_health: { status: storageState, mode: runtimeHealth?.persistence_mode || "sampled" },
      ws_health: {
        status: wsDegraded ? "DEGRADED" : "HEALTHY",
        send_timeout: Number(wsMetrics.send_timeouts || 0),
        send_failure: Number(wsMetrics.send_failures || 0),
        drop: Number(wsMetrics.dropped_sockets || 0),
      },
      binance_age_ms: binanceAge,
      kline_last_finalized_time: klineFinalized,
      ack_cache: { size: Number(exchange.ack_cache_size || 0), capacity: 2_000 },
      refresh_intervals: { ui_coalesce_ms: config.orderbookUiIntervalMs },
      command_age: { canonical: Number(exchange?.latency?.queue_age?.p99_ms || exchange.command_queue_oldest_ms || 0) },
      degraded_reasons: degradedReasons,
      alerts: [],
    },
    metrics: {
      source: "canonical_v1",
      process_tree: processTree,
      exchange,
      websocket: wsMetrics,
    },
  };
};

const DepthRow = memo(function DepthRow({ level, side, priceDigits, qtyDigits }: { level: Level; side: "bid" | "ask"; priceDigits: number; qtyDigits: number }) {
  return (
    <div className="grid grid-cols-[1fr_1fr] items-center gap-2 px-2 py-0.5 text-[11px] leading-5">
      <span className={`num-fixed num-price mono ${side === "bid" ? "text-emerald-200" : "text-rose-200"}`}>{Number(level[0]).toFixed(priceDigits)}</span>
      <span className="num-fixed num-qty mono text-slate-300">{Number(level[1]).toFixed(qtyDigits)}</span>
    </div>
  );
});

function VirtualDepth({ levels, side, priceDigits, qtyDigits }: { levels: Level[]; side: "bid" | "ask"; priceDigits: number; qtyDigits: number }) {
  const [offset, setOffset] = useState(0);
  const rowHeight = 24;
  const visible = 9;
  const start = Math.max(0, Math.min(offset, Math.max(0, levels.length - visible)));
  return (
    <div className="overflow-hidden rounded-xl border border-white/5 bg-black/10">
      <div className="grid grid-cols-[1fr_1fr] gap-2 px-2 py-1 text-[10px] text-slate-500">
        <span>价格</span><span className="text-right">数量</span>
      </div>
      <div
        className="scrollbar h-[218px] overflow-y-auto"
        onScroll={(event) => setOffset(Math.floor(event.currentTarget.scrollTop / rowHeight))}
      >
        <div style={{ height: `${levels.length * rowHeight}px`, position: "relative" }}>
          <div style={{ position: "absolute", top: `${start * rowHeight}px`, left: 0, right: 0 }}>
            {levels.slice(start, start + visible).map((level, index) => <DepthRow key={`${level[0]}-${start + index}`} level={level} side={side} priceDigits={priceDigits} qtyDigits={qtyDigits} />)}
          </div>
        </div>
      </div>
    </div>
  );
}

function Metric({ label, value, hint, className = "" }: { label: string; value: ReactNode; hint?: ReactNode; className?: string }) {
  return (
    <div className={`ui-metric ${className}`}>
      <div className="ui-metric-label">{label}</div>
      <div className="ui-metric-value truncate">{value}</div>
      {hint ? <div className="ui-metric-hint truncate">{hint}</div> : null}
    </div>
  );
}

function StatusPill({ label, status }: { label: string; status: unknown }) {
  return <span className="inline-flex items-center gap-1.5 rounded-full border border-white/10 bg-white/5 px-2.5 py-1 text-[11px]"><span className={`h-1.5 w-1.5 rounded-full bg-current ${tone(status)}`} /><span className="text-slate-400">{label}</span><span className={tone(status)}>{({ HEALTHY: "正常", READY: "就绪", RUNNING: "运行中", FRESH: "及时", STALE: "已过期", UNKNOWN: "未确认", DEGRADED: "需关注", HALTED: "已阻断", POLLING: "定时刷新", CONNECTING: "连接中", OPEN: "已连接", CLOSED: "已断开", CANONICAL_V1: "当前服务", MULTIPROCESS_V2: "实验服务" } as Record<string, string>)[String(status)] || String(status || "未确认")}</span></span>;
}

export function RuntimePage() {
  const [canonicalMarkets, setCanonicalMarkets] = useState<RuntimeMarket[]>([]);
  const [preferredSource, setPreferredSource] = useState<RuntimeSource>("canonical_v1");
  const [marketFilter, setMarketFilter] = useState("");

  const [health, setHealth] = useState<RuntimePayload>({});
  const [metrics, setMetrics] = useState<RuntimePayload>({});
  const [runtimeSource, setRuntimeSource] = useState<RuntimeSource>("canonical_v1");
  const MARKETS = runtimeSource === "canonical_v1" ? canonicalMarkets : DEFAULT_MARKETS;
  const [books, setBooks] = useState<Record<string, BookFrame>>({});
  const [error, setError] = useState("");
  const [socketState, setSocketState] = useState("CONNECTING");
  const [apiKey, setApiKey] = useState(() => window.localStorage.getItem("multiprocess-api-key") || config.multiprocessApiKey);
  const [actionBusy, setActionBusy] = useState("");
  const [actionResult, setActionResult] = useState<RuntimePayload | null>(null);
  const booksRef = useRef<Record<string, BookFrame>>({});
  const pendingRef = useRef<Record<string, BookFrame>>({});
  const commitTimerRef = useRef<number | undefined>(undefined);
  const sequenceRef = useRef<Record<string, number>>({});
  const streamRef = useRef<Record<string, string>>({});
  const progressRef = useRef<Record<string, number[]>>({});
  const protocolCounters = useRef({ rollbacks: 0, sameSeqConflicts: 0, invalid: 0, gaps: 0 });
  const probeRef = useRef<Record<string, AbortController | undefined>>({});

  const fetchRuntime = useCallback(async () => {
    const readCanonical = async (path: string) => {
      const canonicalKey = apiKey && apiKey !== config.multiprocessApiKey ? apiKey : config.canonicalApiKey;
      const response = await fetch(`${config.apiBaseUrl}${path}`, {
        headers: { Accept: "application/json", "X-API-Key": canonicalKey },
      });
      if (!response.ok) throw new Error(`canonical ${path} ${response.status}`);
      return response.json();
    };
    const readCanonicalHealth = async () => {
      const canonicalKey = apiKey && apiKey !== config.multiprocessApiKey ? apiKey : config.canonicalApiKey;
      const rootUrl = config.apiBaseUrl.replace(/\/api\/v1\/?$/, "");
      const response = await fetch(`${rootUrl}/health`, {
        headers: { Accept: "application/json", "X-API-Key": canonicalKey },
      });
      if (!response.ok) throw new Error(`canonical /health ${response.status}`);
      return response.json();
    };
    try {
      if (preferredSource === "canonical_v1") throw new Error("使用当前服务");
      const [healthResponse, metricsResponse] = await Promise.all([
        fetch(`${config.multiprocessBaseUrl}/health`, { headers: { Accept: "application/json" } }),
        fetch(`${config.multiprocessBaseUrl}/api/v2/metrics`, { headers: { Accept: "application/json", "X-API-Key": apiKey } }),
      ]);
      if (!healthResponse.ok) throw new Error(`health ${healthResponse.status}`);
      if (!metricsResponse.ok) throw new Error(`metrics ${metricsResponse.status}`);
      setHealth(await healthResponse.json());
      setMetrics(await metricsResponse.json());
      setRuntimeSource("multiprocess_v2");
      setError("");
    } catch (multiprocessError) {
      try {
        const marketResponse = await readCanonical("/markets");
        const discovered: RuntimeMarket[] = (marketResponse.items || []).map((item: MarketDefinition) => ({ id: item.symbol, label: item.product_type === "PERP" ? "Perp" : "Spot", product: item.product_type === "PERP" ? "USDT 永续" : "现货" }));
        setCanonicalMarkets((previous) => JSON.stringify(previous) === JSON.stringify(discovered) ? previous : discovered);
        const [runtimeHealth, systemStatus, ...dumps] = await Promise.all([
          readCanonicalHealth(),
          readCanonical("/admin/system-status"),
          ...discovered.map((market) => readCanonical(`/markets/${encodeURIComponent(market.id)}/maker-instance`).catch(() => ({ symbol: market.id }))),
        ]);
        const canonical = buildCanonicalRuntime(runtimeHealth, systemStatus, dumps, discovered);
        setHealth(canonical.health);
        setMetrics(canonical.metrics);
        setRuntimeSource("canonical_v1");
        setError("");
      } catch (canonicalError) {
        const multiprocessMessage = multiprocessError instanceof Error ? multiprocessError.message : "5184 API 不可用";
        const canonicalMessage = canonicalError instanceof Error ? canonicalError.message : "5174 API 不可用";
        setHealth({});
        setMetrics({});
        setError(preferredSource === "canonical_v1" ? canonicalMessage : `${multiprocessMessage}; ${canonicalMessage}`);
      }
    }
  }, [apiKey, preferredSource]);

  const commitPending = useCallback(() => {
    commitTimerRef.current = undefined;
    const next = { ...booksRef.current, ...pendingRef.current };
    pendingRef.current = {};
    booksRef.current = next;
    setBooks(next);
  }, []);

  const scheduleCommit = useCallback(() => {
    if (commitTimerRef.current !== undefined) return;
    commitTimerRef.current = window.setTimeout(commitPending, config.orderbookUiIntervalMs);
  }, [commitPending]);

  const probeBook = useCallback(async (market: string) => {
    if (probeRef.current[market]) return;
    const controller = new AbortController();
    probeRef.current[market] = controller;
    const timeout = window.setTimeout(() => controller.abort(), 1_000);
    try {
      const canonical = runtimeSource === "canonical_v1";
      const response = await fetch(
        canonical
          ? `${config.apiBaseUrl}/markets/${encodeURIComponent(market)}/orderbook?depth=100`
          : `${config.multiprocessBaseUrl}/api/v2/orderbooks/${market}`,
        {
          signal: controller.signal,
          headers: canonical ? { Accept: "application/json", "X-API-Key": apiKey && apiKey !== config.multiprocessApiKey ? apiKey : config.canonicalApiKey } : { Accept: "application/json" },
        },
      );
      if (!response.ok) return;
      const rawPayload = await response.json();
      const payload = canonical
        ? {
            market_id: market,
            stream_epoch: `canonical:${String(rawPayload?.stream_id || market)}`,
            stream_id: String(rawPayload?.stream_id || `${market}-${rawPayload?.ts || Date.now()}`),
            seq: Number(rawPayload?.last_update_id || 0),
            published_at: Number(rawPayload?.ts) || Date.now(),
            bids: rawPayload?.bids || [],
            asks: rawPayload?.asks || [],
          }
        : rawPayload;
      const current = pendingRef.current[market] || booksRef.current[market];
      const parsed = parseBook(payload, current);
      const pending = pendingRef.current[market];
      if (parsed && (!pending || parsed.stream_epoch !== pending.stream_epoch || parsed.seq >= pending.seq)) {
        if (canonical) {
          const times = progressRef.current[market] || [];
          const now = Date.now();
          times.push(now);
          progressRef.current[market] = times.filter((item) => now - item <= 10_000);
        }
        pendingRef.current[market] = parsed;
        scheduleCommit();
      }
    } catch {
      // Probe is diagnostic/recovery only. It must not reconnect a healthy WS.
    } finally {
      window.clearTimeout(timeout);
      probeRef.current[market] = undefined;
    }
  }, [apiKey, runtimeSource, scheduleCommit]);

  useEffect(() => {
    window.localStorage.setItem("multiprocess-api-key", apiKey);
    void fetchRuntime();
    const timer = window.setInterval(() => void fetchRuntime(), runtimeSource === "canonical_v1" ? 3_000 : 1_000);
    return () => window.clearInterval(timer);
  }, [apiKey, fetchRuntime, runtimeSource]);

  useEffect(() => {
    let disposed = false;
    let socket: WebSocket | undefined;
    let reconnectTimer: number | undefined;
    let socketGeneration = 0;
    if (runtimeSource === "canonical_v1") {
      setSocketState("POLLING");
      const poll = () => MARKETS.forEach((market) => void probeBook(market.id));
      poll();
      const pollTimer = window.setInterval(poll, 1_000);
      return () => {
        disposed = true;
        window.clearInterval(pollTimer);
        if (commitTimerRef.current !== undefined) window.clearTimeout(commitTimerRef.current);
      };
    }
    const connect = () => {
      if (disposed) return;
      const generation = ++socketGeneration;
      setSocketState("CONNECTING");
      socket = new WebSocket(config.multiprocessWsUrl);
      socket.onopen = () => {
        if (generation !== socketGeneration) return;
        setSocketState("OPEN");
      };
      socket.onmessage = (event) => {
        if (generation !== socketGeneration) return;
        try {
          const payload = JSON.parse(event.data) as RuntimePayload;
          if (payload.channel !== "orderbook") return;
          const market = String(payload.market_id || "").toUpperCase();
          const previous = pendingRef.current[market] || booksRef.current[market];
          const epoch = String(payload.stream_epoch || "");
          const streamId = String(payload.stream_id || "");
          const seq = Number(payload.seq);
          if (!epoch || !streamId || !Number.isInteger(seq) || seq < 0) {
            protocolCounters.current.invalid += 1;
            return;
          }
          if (previous && previous.stream_epoch === epoch && previous.stream_id === streamId && seq === previous.seq) {
            const candidate = parseBook(payload, previous);
            if (!candidate || !sameContent(previous, candidate)) protocolCounters.current.sameSeqConflicts += 1;
            return;
          }
          if (previous && previous.stream_epoch === epoch && seq < previous.seq) {
            protocolCounters.current.rollbacks += 1;
            return;
          }
          if (previous && previous.stream_epoch === epoch && previous.stream_id !== streamId) {
            protocolCounters.current.invalid += 1;
            return;
          }
          const parsed = parseBook(payload, previous);
          if (!parsed) {
            protocolCounters.current.gaps += 1;
            void probeBook(market);
            return;
          }
          const pending = pendingRef.current[market];
          if (
            pending &&
            pending.stream_epoch === parsed.stream_epoch &&
            pending.stream_id === parsed.stream_id &&
            parsed.seq <= pending.seq
          ) {
            return;
          }
          const times = progressRef.current[market] || [];
          const now = Date.now();
          times.push(now);
          progressRef.current[market] = times.filter((item) => now - item <= 10_000);
          sequenceRef.current[market] = parsed.seq;
          streamRef.current[market] = parsed.stream_epoch;
          pendingRef.current[market] = parsed;
          scheduleCommit();
        } catch {
          protocolCounters.current.invalid += 1;
        }
      };
      socket.onerror = () => {
        if (generation === socketGeneration) setSocketState("ERROR");
      };
      socket.onclose = () => {
        if (generation !== socketGeneration) return;
        setSocketState("CLOSED");
        if (!disposed) reconnectTimer = window.setTimeout(connect, 1_000);
      };
    };
    connect();
    return () => {
      disposed = true;
      socketGeneration += 1;
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
      socket?.close();
      if (commitTimerRef.current !== undefined) window.clearTimeout(commitTimerRef.current);
    };
  }, [probeBook, runtimeSource, scheduleCommit, canonicalMarkets]);

  const tree = (health.process_tree || metrics.process_tree || {}) as RuntimePayload;
  const processComponents = (tree.components || {}) as Record<string, RuntimePayload>;
  const heartbeats = (health.metrics?.heartbeats || metrics.heartbeats || {}) as Record<string, RuntimePayload>;
  const engineHealth = (health.engine_health || {}) as Record<string, RuntimePayload>;
  const marketData = (health.market_data_health || {}) as Record<string, RuntimePayload>;
  const freshness = (health.book_freshness || {}) as Record<string, RuntimePayload>;
  const sampler = (health.sampler_health || {}) as RuntimePayload;
  const storage = (health.storage_health || {}) as RuntimePayload;
  const wsHealth = (health.ws_health || {}) as RuntimePayload;
  const action = async (request: RuntimePayload) => {
    const label = `${request.action}:${request.target || "all"}`;
    if (runtimeSource === "canonical_v1") {
      setActionResult({ status: "READ_ONLY", source: "canonical_v1", detail: "当前页面通过 canonical 5174 只读兼容层读取；运行控制请使用 sandbox_supervisor.py，避免把 v2 动作误发到旧单进程入口。" });
      return;
    }
    if (["stop", "restart_all", "clear_memory", "rotate_history"].includes(request.action) && !window.confirm(`确认执行 ${label}？影响：${request.impact}`)) return;
    setActionBusy(label);
    try {
      const response = await fetch(`${config.multiprocessBaseUrl}/api/v2/runtime/actions`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-API-Key": apiKey },
        body: JSON.stringify(request),
      });
      const result = await response.json();
      setActionResult(result);
      if (!response.ok) throw new Error(result.detail || result.status || `action ${response.status}`);
      setError("");
      window.setTimeout(() => void fetchRuntime(), 500);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "运行控制失败");
    } finally {
      setActionBusy("");
    }
  };

  return (
    <AppShell>
      <main className="min-w-0 space-y-3 pb-8">
        <section className="panel min-w-0 rounded-3xl p-4 md:p-5">
          <div className="flex min-w-0 flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2"><span className="text-xs tracking-wide text-cyan-300">服务与市场</span><StatusPill label="数据源" status={runtimeSource === "canonical_v1" ? "CANONICAL_V1" : "MULTIPROCESS_V2"} /><StatusPill label="行情连接" status={socketState} /><StatusPill label="服务" status={health.effective_status || "UNKNOWN"} /></div>
              <h1 className="mt-2 truncate font-display text-xl text-white">运行状态</h1>
              <p className="mt-1 max-w-3xl text-xs text-slate-500">查看各市场是否有报价、参考行情是否及时，以及服务是否可用。现货与永续是独立市场；没有报价时，先到管理后台检查做市实例。</p>
            </div>
            <div className="grid min-w-0 grid-cols-2 gap-2 sm:grid-cols-4">
              <Metric label="run id" value={shorten(health.run_id, 12)} hint={health.uptime_seconds ? `${fmtNumber(health.uptime_seconds, 0)} s uptime` : "-"} />
              <Metric label="CPU / 单核" value={`${fmtNumber(tree.tree_cpu_single_core_pct, 1)}% / ${fmtNumber(num(tree.tree_cpu_single_core_pct) === null ? null : Number(tree.tree_cpu_single_core_pct) / 100, 2)} 核`} hint={`${fmtNumber(tree.tree_cpu_machine_pct, 1)}% 整机`} />
              <Metric label="整棵树 RSS" value={fmtBytes(tree.tree_rss_bytes)} hint={`footprint ${fmtBytes(tree.tree_physical_footprint_bytes || tree.tree_footprint_bytes)}`} />
              <Metric label="system compressed / swap" value={`${fmtBytes(tree.tree_compressed_bytes || tree.system_memory?.compressed_bytes)} / ${fmtBytes(tree.tree_swap_bytes || tree.system_memory?.swap_used_bytes)}`} hint={`threads ${fmtNumber(tree.thread_count, 0)} · ${tree.tree_swap_source || "system scope"}`} />
            </div>
          </div>
          {error ? <div className="mt-3 rounded-2xl border border-rose-300/20 bg-rose-400/10 px-3 py-2 text-xs text-rose-200">{error}。请确认 canonical 5174 或隔离 5184 运行已启动，并检查 API key。</div> : null}
        </section>

        <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-base text-slate-200">市场状态 · {MARKETS.length}</h2><label className="text-sm text-slate-400">筛选市场 <input aria-label="筛选运行状态市场" placeholder="输入币对，如 BTC" value={marketFilter} onChange={(event) => setMarketFilter(event.target.value)} className="ml-2 rounded-lg border border-white/10 bg-slate-900 px-3 py-2" /></label></div>
        {MARKETS.length === 0 && <p className="panel rounded-xl p-4 text-sm text-slate-400">{error ? "市场列表暂不可用，请检查服务连接。" : "正在读取市场列表；可在管理后台创建交易市场。"}</p>}
        <section className="grid min-w-0 gap-3 xl:grid-cols-2">
          {MARKETS.filter((market) => market.id.includes(marketFilter.trim().toUpperCase())).map((market) => <MarketCard key={market.id} market={market} book={books[market.id]} marketData={marketData[market.id]} freshness={freshness[market.id]} engine={engineHealth[market.id]} progressTimes={progressRef.current[market.id] || []} />)}
        </section>

        <details className="panel min-w-0 overflow-hidden rounded-3xl p-4">
          <summary className="cursor-pointer text-sm text-slate-300">高级诊断 · 进程资源</summary>
          <label className="mt-3 block text-xs text-slate-400">诊断数据源 <select value={preferredSource} onChange={(event) => setPreferredSource(event.target.value as RuntimeSource)} className="ml-2 rounded-lg bg-slate-900 p-2"><option value="canonical_v1">当前服务</option><option value="multiprocess_v2">多进程实验服务（不可用时回退当前服务）</option></select></label>
          <div className="mt-3 flex flex-wrap items-end justify-between gap-2"><div><h2 className="font-display text-lg text-white">进程树资源</h2><p className="mt-1 text-xs text-slate-500">CPU 为进程总和，线程只计 PID；physical footprint 由 macOS footprint 提供，权限不足时明确显示来源。</p></div><span className="text-xs text-slate-500">collector={String(tree.collector || "-")}</span></div>
          <div className="mt-3 overflow-x-auto"><table className="w-full min-w-[900px] text-left text-xs"><thead className="text-[10px] uppercase tracking-[0.1em] text-slate-500"><tr><th className="px-2 py-2">component</th><th className="px-2 py-2">PID</th><th className="px-2 py-2">status</th><th className="px-2 py-2 text-right">CPU 单核</th><th className="px-2 py-2 text-right">CPU 整机</th><th className="px-2 py-2 text-right">RSS</th><th className="px-2 py-2 text-right">footprint</th><th className="px-2 py-2 text-right">swap</th><th className="px-2 py-2 text-right">threads</th><th className="px-2 py-2 text-right">uptime</th></tr></thead><tbody>{RESOURCE_ROLES.map(([role, label]) => <ResourceRow key={role} role={role} label={label} item={processComponents[role]} cpuCount={tree.cpu_count} />)}</tbody></table></div>
        </details>

        <section className="grid min-w-0 gap-3 lg:grid-cols-3">
          <HealthPanel title="健康语义" items={[["liveness", health.liveness?.status], ["readiness", health.readiness?.status], ["market data", Object.values(marketData).every((item) => ["HEALTHY", "FRESH"].includes(String(item.status).toUpperCase())) ? "HEALTHY" : "STALE"], ["engine", Object.values(engineHealth).every((item) => ["HEALTHY", "RUNNING"].includes(String(item.engine_status).toUpperCase())) ? "HEALTHY" : "HALTED"], ["book freshness", Object.values(freshness).every((item) => item.status === "FRESH") ? "FRESH" : "STALE"], ["sampler", sampler.status], ["storage", storage.status], ["WS", wsHealth.status]]} />
          <section className="panel min-w-0 rounded-3xl p-4"><h2 className="font-display text-lg text-white">细节指标</h2><div className="mt-3 grid grid-cols-2 gap-2"><Metric label="Spot Binance age" value={fmtAge(health.binance_age_ms?.BTCUSDT)} /><Metric label="Perp Binance age" value={fmtAge(health.binance_age_ms?.["BTCUSDT-PERP"])} /><Metric label="kline finalized" value={fmtTime(health.kline_last_finalized_time?.BTCUSDT)} /><Metric label="sampling drops" value={fmtNumber(sampler.sampling_drops, 0)} hint="DEGRADED only" /><Metric label="ACK cache" value={`${fmtNumber(health.ack_cache?.size, 0)} / ${fmtNumber(health.ack_cache?.capacity, 0)}`} /><Metric label="WS timeout/drop" value={`${fmtNumber(wsHealth.send_timeout, 0)} / ${fmtNumber(wsHealth.drop, 0)}`} /><Metric label="UI coalesce" value={`${fmtNumber(health.refresh_intervals?.ui_coalesce_ms, 0)} ms`} hint="16 ms extreme" /><Metric label="command age" value={fmtAge(Object.values(health.command_age || {})[0])} /></div></section>
          <section className="panel min-w-0 rounded-3xl p-4"><h2 className="font-display text-lg text-white">降级与告警</h2><div className="mt-3 max-h-[235px] space-y-2 overflow-y-auto pr-1">{(health.degraded_reasons || []).length === 0 && (health.alerts || []).length === 0 ? <p className="text-xs text-emerald-200">当前没有聚合告警。</p> : null}{(health.degraded_reasons || []).map((item: RuntimePayload, index: number) => <div key={`${item.code}-${index}`} className="rounded-xl border border-amber-300/15 bg-amber-400/5 px-2.5 py-2 text-xs"><div className="flex justify-between gap-2"><span className="text-amber-200">{item.code}</span><span className="text-slate-500">{item.affects_readiness ? "影响 readiness" : "不阻止撮合"}</span></div><p className="mt-1 text-slate-400">{item.detail}</p></div>)}{(health.alerts || []).map((item: RuntimePayload) => <div key={item.fingerprint} className="rounded-xl border border-white/8 bg-white/[0.03] px-2.5 py-2 text-xs"><div className="flex justify-between gap-2"><span className={tone(item.severity)}>{item.severity} · {item.message}</span><span className="font-mono tabular-nums text-slate-500">x{item.count}</span></div><p className="mt-1 text-slate-600">first {fmtTime(item.first_seen)} · last {fmtTime(item.last_seen)}</p></div>)}</div></section>
        </section>

        <section className="panel min-w-0 rounded-3xl p-4">
          <div className="flex flex-wrap items-end justify-between gap-2"><div><h2 className="font-display text-lg text-white">运行控制</h2><p className="mt-1 text-xs text-slate-500">每个动作都显示作用域；单 worker 重启只轮换对应市场 epoch，history 轮换保留可恢复 archive。canonical 5174 兼容层只读，控制按钮会明确拒绝。</p></div><label className="flex items-center gap-2 text-xs text-slate-500">API key<input value={apiKey} onChange={(event) => setApiKey(event.target.value)} className="w-44 rounded-xl border border-white/10 bg-white/5 px-2.5 py-1.5 font-mono text-xs text-slate-200 outline-none" /></label></div>
          <div className="mt-3 flex flex-wrap gap-2"><ControlButton label="启动" busy={actionBusy === "start:all"} onClick={() => void action({ action: "start", target: "all", impact: "补齐缺失的隔离进程；已运行进程不动" })} /><ControlButton label="停止全部" danger busy={actionBusy === "stop:all"} onClick={() => void action({ action: "stop", target: "all", impact: "暂停 Spot/Perp maker 并停止本次完整 run" })} /><ControlButton label="重启 Spot Worker" busy={actionBusy === "restart_worker:spot"} onClick={() => void action({ action: "restart_worker", target: "spot", impact: "只重启 BTCUSDT Spot Worker；Perp 保持运行" })} /><ControlButton label="重启 Perp Worker" busy={actionBusy === "restart_worker:perp"} onClick={() => void action({ action: "restart_worker", target: "perp", impact: "只重启 BTCUSDT-PERP Perp Worker；Spot 保持运行" })} /><ControlButton label="重启全部" danger busy={actionBusy === "restart_all:all"} onClick={() => void action({ action: "restart_all", target: "all", impact: "暂停两个 maker，按 Supervisor 顺序重建整棵进程树" })} /><ControlButton label="清空当前 run 内存" busy={actionBusy === "clear_memory:all"} onClick={() => void action({ action: "clear_memory", target: "all", impact: "只重启两个 engine process；history DB 保留" })} /><ControlButton label="轮换 history DB" busy={actionBusy === "rotate_history:all"} onClick={() => void action({ action: "rotate_history", target: "all", impact: "停止 Sampler，归档本次精确 history 文件，再启动 Sampler" })} /><ControlButton label="导出 run summary" busy={actionBusy === "export_run_summary:all"} onClick={() => void action({ action: "export_run_summary", target: "all", impact: "写入当前 runtime_dir 的 JSON 摘要" })} /></div>
          <div className="mt-3 flex flex-wrap items-center gap-2"><span className="text-xs text-slate-500">测试档（重启 Feed，影响 reference cadence）</span>{[300, 100, 50].map((profile) => <button key={profile} type="button" onClick={() => void action({ action: "set_profile", profile_ms: profile, target: String(profile), impact: `仅切换 Feed ${profile}ms 测试档，不改变撮合算法或持久化模式` })} className="rounded-xl border border-white/10 bg-white/5 px-3 py-1.5 text-xs text-slate-300 hover:border-cyan-300/30 hover:text-cyan-200">{profile}ms</button>)}</div>
          {actionResult ? <pre className="mt-3 max-h-36 overflow-auto rounded-2xl border border-cyan-300/15 bg-cyan-400/5 p-3 text-[11px] text-cyan-100">{JSON.stringify(actionResult, null, 2)}</pre> : null}
        </section>
      </main>
    </AppShell>
  );
}

function MarketCard({ market, book, marketData, freshness, engine, progressTimes }: { market: RuntimeMarket; book?: BookFrame; marketData?: RuntimePayload; freshness?: RuntimePayload; engine?: RuntimePayload; progressTimes: number[] }) {
  const [definition, setDefinition] = useState<MarketDefinition>();
  useEffect(() => {
    let active = true;
    void api.get<{ items: MarketDefinition[] }>("/markets").then((result) => {
      if (active) setDefinition(result.items.find((item) => item.symbol === market.id));
    }).catch(() => { /* Retain the fallback precision if metadata is unavailable. */ });
    return () => { active = false; };
  }, [market.id]);
  const priceDigits = definition?.price_precision ?? 2;
  const qtyDigits = definition?.qty_precision ?? 4;

  const bestBid = num(book?.bids?.[0]?.[0]);
  const bestAsk = num(book?.asks?.[0]?.[0]);
  const localMid = bestBid !== null && bestAsk !== null ? (bestBid + bestAsk) / 2 : null;
  const binance = num(marketData?.price);
  const deviation = localMid !== null && binance ? ((localMid - binance) / binance) * 10_000 : null;
  const now = Date.now();
  const measured = progressTimes.filter((item) => now - item <= 10_000).length / 10;
  return <section className="panel min-w-0 rounded-3xl p-4"><div className="flex min-w-0 items-start justify-between gap-3"><div className="min-w-0"><div className="flex items-center gap-2"><span className="rounded-lg bg-cyan-400/10 px-2 py-1 text-[10px] uppercase tracking-[0.14em] text-cyan-200">{market.label}</span><span className="truncate text-sm text-slate-300">{market.product} · {market.id}</span></div><div className="mt-2 flex flex-wrap items-baseline gap-x-3 gap-y-1"><span className="font-mono text-2xl tabular-nums text-white">{binance === null ? "-" : binance.toFixed(2)}</span><span className="text-xs text-slate-500">Binance</span><span className={`text-xs ${tone(marketData?.status)}`}>{marketData?.status || "UNKNOWN"}</span></div></div><div className="text-right text-xs"><div className="text-slate-500">更新时间 {fmtTime(marketData?.updated_at)}</div><div className="mt-1 text-slate-400">延迟 <span className="font-mono tabular-nums text-cyan-200">{fmtAge(marketData?.binance_age_ms)}</span></div></div></div><div className="mt-3 grid min-w-0 grid-cols-2 gap-2 sm:grid-cols-4"><Metric label="本地买一" value={bestBid === null ? "-" : bestBid.toFixed(2)} /><Metric label="本地卖一" value={bestAsk === null ? "-" : bestAsk.toFixed(2)} /><Metric label="本地 mid" value={localMid === null ? "-" : localMid.toFixed(2)} /><Metric label="偏差 bps" value={deviation === null ? "-" : `${deviation >= 0 ? "+" : ""}${deviation.toFixed(2)}`} hint="local mid - Binance" /></div><div className="mt-3 grid min-w-0 grid-cols-2 gap-2 text-xs text-slate-400 sm:grid-cols-4"><span>epoch <b className="font-mono text-slate-200">{shorten(book?.stream_epoch, 12)}</b></span><span>seq <b className="font-mono tabular-nums text-slate-200">{book?.seq ?? "-"}</b></span><span>业务进度 <b className="font-mono text-slate-200">{fmtTime(freshness?.last_business_progress_at)}</b></span><span>盘口变化 <b className="font-mono tabular-nums text-cyan-200">{measured.toFixed(1)}/s</b></span></div><details className="mt-3"><summary className="cursor-pointer text-xs text-slate-400">查看买卖深度</summary><div className="mt-3 grid min-w-0 grid-cols-2 gap-3"><div><div className="mb-1 text-[10px] uppercase tracking-[0.1em] text-slate-500">bids · 可见深度 {book?.bids?.length || 0}</div><VirtualDepth levels={book?.bids || []} side="bid" priceDigits={priceDigits} qtyDigits={qtyDigits} /></div><div><div className="mb-1 text-[10px] uppercase tracking-[0.1em] text-slate-500">asks · 可见深度 {book?.asks?.length || 0}</div><VirtualDepth levels={book?.asks || []} side="ask" priceDigits={priceDigits} qtyDigits={qtyDigits} /></div></div></details><div className="mt-2 flex flex-wrap gap-2"><StatusPill label="engine" status={engine?.engine_status || "UNKNOWN"} /><StatusPill label="reference" status={marketData?.status || "UNKNOWN"} /><StatusPill label="book" status={freshness?.status || "UNKNOWN"} /><span className="max-w-full truncate text-[10px] text-slate-600">stream_id={shorten(book?.stream_id, 30)}</span></div></section>;
}

function ResourceRow({ role, label, item, cpuCount }: { role: string; label: string; item?: RuntimePayload; cpuCount?: number }) {
  const cpu = num(item?.cpu_single_core_pct);
  return <tr className="border-t border-white/5"><td className="px-2 py-2 text-slate-200">{label}<span className="ml-1 text-[10px] text-slate-600">{role}</span></td><td className="px-2 py-2 font-mono tabular-nums text-slate-400">{item?.pid || "-"}</td><td className={`px-2 py-2 ${tone(item?.alive === false ? "HALTED" : item?.status || "UNKNOWN")}`}>{item?.alive === false ? "DEAD" : item?.status || "-"}</td><td className="px-2 py-2 text-right font-mono tabular-nums text-slate-300">{cpu === null ? "-" : `${cpu.toFixed(1)}% / ${(cpu / 100).toFixed(2)}核`}</td><td className="px-2 py-2 text-right font-mono tabular-nums text-slate-400">{cpu === null || !cpuCount ? "-" : `${(cpu / cpuCount).toFixed(1)}%`}</td><td className="px-2 py-2 text-right font-mono tabular-nums text-slate-400">{fmtBytes(item?.rss_bytes)}</td><td className="px-2 py-2 text-right font-mono tabular-nums text-slate-400">{fmtBytes(item?.physical_footprint_bytes || item?.footprint_bytes)}</td><td className="px-2 py-2 text-right font-mono tabular-nums text-slate-400">{fmtBytes(item?.swap_bytes)}</td><td className="px-2 py-2 text-right font-mono tabular-nums text-slate-400">{fmtNumber(item?.thread_count, 0)}</td><td className="px-2 py-2 text-right font-mono tabular-nums text-slate-400">{fmtNumber(item?.uptime_seconds, 0)}s</td></tr>;
}

function HealthPanel({ title, items }: { title: string; items: [string, unknown][] }) {
  return <section className="panel min-w-0 rounded-3xl p-4"><h2 className="font-display text-lg text-white">{title}</h2><div className="mt-3 flex flex-wrap gap-2">{items.map(([label, status]) => <StatusPill key={label} label={label} status={status} />)}</div><div className="mt-4 space-y-2 text-xs text-slate-500"><p>顶层 effective_status：<span className={tone(items.some((item) => ["HALTED", "FAILED"].includes(String(item[1]).toUpperCase())) ? "HALTED" : "HEALTHY")}>{items.some((item) => ["HALTED", "FAILED"].includes(String(item[1]).toUpperCase())) ? "HALTED" : "由子状态聚合"}</span></p><p>Sampler degraded 只影响 history 可用性；engine 或 Binance stale 会使 readiness=NOT_READY。</p></div></section>;
}

function ControlButton({ label, onClick, busy, danger = false }: { label: string; onClick: () => void; busy: boolean; danger?: boolean }) {
  return <button type="button" disabled={busy} onClick={onClick} className={`rounded-xl border px-3 py-2 text-xs transition disabled:cursor-not-allowed disabled:opacity-50 ${danger ? "border-rose-300/20 bg-rose-400/8 text-rose-100 hover:bg-rose-400/15" : "border-white/10 bg-white/5 text-slate-200 hover:border-cyan-300/30 hover:text-cyan-200"}`}>{busy ? "处理中…" : label}</button>;
}
