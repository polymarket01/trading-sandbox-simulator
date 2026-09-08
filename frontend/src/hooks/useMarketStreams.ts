import { startTransition, useEffect } from "react";
import { config } from "../lib/config";
import { useAppStore } from "../store/useAppStore";
import type { Level, TradeItem } from "../types";

const hasVisibleTradeSource = (item: Record<string, unknown>) => {
  const sourceCounts = item.source_counts;
  if (sourceCounts && typeof sourceCounts === "object") {
    return Object.entries(sourceCounts as Record<string, unknown>).some(
      ([source, count]) => source !== "bootstrap_seed" && Number(count) > 0,
    );
  }
  return item.source !== "bootstrap_seed";
};

const isValidKline = (item: Record<string, unknown>) => {
  const prices = [item.open, item.high, item.low, item.close].map(Number);
  return prices.every((value) => Number.isFinite(value) && value > 0) && Number(item.high) >= Number(item.low);
};

const finiteNumber = (value: unknown) => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
};

const safeSequence = (value: unknown) => {
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : undefined;
};

const validStreamId = (value: unknown) => (typeof value === "string" && value.trim().length > 0 ? value : undefined);

type PendingSnapshot = {
  bids: [string, string][];
  asks: [string, string][];
  ts?: number;
  seq: number;
  streamId: string;
  receivedAtPerfMs: number;
};

type FrontendOrderbookLatency = {
  count: number;
  p50_ms: number;
  p95_ms: number;
  p99_ms: number;
  last_ms: number;
};

const observeFrontendApply = (elapsedMs: number) => {
  if (typeof window === "undefined") return;
  const target = window as Window & { __mmOrderbookLatency?: FrontendOrderbookLatency & { samples?: number[] } };
  const samples = [...(target.__mmOrderbookLatency?.samples ?? []), Math.max(0, elapsedMs)].slice(-256);
  const ordered = [...samples].sort((left, right) => left - right);
  const percentile = (ratio: number) => ordered.length > 0
    ? ordered[Math.min(ordered.length - 1, Math.max(0, Math.round(ratio * (ordered.length - 1))))]
    : 0;
  target.__mmOrderbookLatency = {
    count: (target.__mmOrderbookLatency?.count ?? 0) + 1,
    p50_ms: Number(percentile(0.5).toFixed(3)),
    p95_ms: Number(percentile(0.95).toFixed(3)),
    p99_ms: Number(percentile(0.99).toFixed(3)),
    last_ms: Number(Math.max(0, elapsedMs).toFixed(3)),
    samples,
  };
};

export function useMarketStreams(symbol: string, interval: string, depth: number, includeSeed = false, apiPrefix = "") {
  const setOrderbook = useAppStore((state) => state.setOrderbook);

  const prependRecentTrades = useAppStore((state) => state.prependRecentTrades);
  const patchTicker = useAppStore((state) => state.patchTicker);
  const upsertKline = useAppStore((state) => state.upsertKline);
  const setPublicStreamStatus = useAppStore((state) => state.setPublicStreamStatus);
  const setTickerUpdatedAt = useAppStore((state) => state.setTickerUpdatedAt);
  const setKlinesUpdatedAt = useAppStore((state) => state.setKlinesUpdatedAt);

  useEffect(() => {
    const normalizedApiPrefix = apiPrefix ? `/${apiPrefix.replace(/^\/+|\/+$/g, "")}` : "";
    const marketApiBase = `${config.apiBaseUrl}${normalizedApiPrefix}`;
    // Subscribe to an actual backend depth projection; display 10/30 locally.
    const transportDepth = depth <= 50 ? 50 : 100;
    let receivedBook: PendingSnapshot | null = null;
    const mergeChanges = (current: Level[], changes: Level[], reverse: boolean): Level[] => {
      const rows = new Map(current.map(([price, quantity]) => [Number(price), quantity]));
      for (const [price, quantity] of changes) {
        if (Number(quantity) === 0) rows.delete(Number(price));
        else rows.set(Number(price), quantity);
      }
      return [...rows].sort((a, b) => reverse ? b[0] - a[0] : a[0] - b[0]).map(([price, quantity]) => [String(price), quantity]);
    };
    let socket: WebSocket | null = null;
    let reconnectTimer: number | null = null;
    let fallbackTimer: number | null = null;
    let orderbookCommitTimer: number | null = null;
    let watchdogTimer: number | null = null;
    let fallbackAbortController: AbortController | null = null;
    let fallbackInFlight = false;
    let pendingSnapshot: PendingSnapshot | null = null;
    let closedByEffect = false;
    let wsOpen = false;
    let socketGeneration = 0;
    let lastOrderbookFrameAt = 0;
    let lastOrderbookProgressAt = Date.now();
    let lastProgressProbeAt = 0;
    let observedOrderbookStreamId: string | undefined;
    let observedOrderbookSeq: number | undefined;
    let observedOrderbookTs: number | undefined;
    const WATCHDOG_MS = 8000;
    const FALLBACK_TIMEOUT_MS = 2500;

    const reportStatus = (next: "connecting" | "open" | "reconnecting" | "fallback" | "idle", lastMessageAt?: number) => {
      setPublicStreamStatus(next, lastMessageAt);
    };

    const markTransportFrame = () => {
      reportStatus(wsOpen ? "open" : "fallback", Date.now());
    };

    const parseSnapshot = (payload: Record<string, any>, sequenceKey: "seq" | "last_update_id"): PendingSnapshot | null => {
      if (!Array.isArray(payload.bids) || !Array.isArray(payload.asks)) return null;
      const seq = safeSequence(payload[sequenceKey]);
      const streamId = validStreamId(payload.stream_id);
      if (seq === undefined || streamId === undefined) return null;
      return {
        bids: payload.bids,
        asks: payload.asks,
        ts: finiteNumber(payload.ts),
        seq,
        streamId,
        receivedAtPerfMs: performance.now(),
      };
    };

    const recordOrderbookProgress = (snapshot: PendingSnapshot) => {
      const streamChanged = snapshot.streamId !== observedOrderbookStreamId;
      if (!streamChanged && observedOrderbookSeq !== undefined && snapshot.seq < observedOrderbookSeq) return false;
      const progressed = streamChanged
        || observedOrderbookSeq === undefined
        || snapshot.seq > observedOrderbookSeq
        || (snapshot.ts !== undefined && snapshot.ts > (observedOrderbookTs ?? 0));
      observedOrderbookStreamId = snapshot.streamId;
      observedOrderbookSeq = snapshot.seq;
      if (snapshot.ts !== undefined) observedOrderbookTs = Math.max(observedOrderbookTs ?? 0, snapshot.ts);
      if (progressed) lastOrderbookProgressAt = Date.now();
      return true;
    };

    const stopWatchdog = () => {
      if (watchdogTimer !== null) {
        window.clearInterval(watchdogTimer);
        watchdogTimer = null;
      }
    };

    const armWatchdog = () => {
      stopWatchdog();
      lastOrderbookFrameAt = Date.now();
      watchdogTimer = window.setInterval(() => {
        if (closedByEffect) return;
        // Transport freshness and business progress are deliberately separate:
        // an unchanged heartbeat proves the channel is alive, not that makers
        // are still mutating the published book.
        if (wsOpen && Date.now() - lastOrderbookFrameAt > WATCHDOG_MS) {
          restartSocket();
          return;
        }
        if (
          wsOpen
          && Date.now() - lastOrderbookProgressAt > WATCHDOG_MS
          && Date.now() - lastProgressProbeAt > WATCHDOG_MS
        ) {
          lastProgressProbeAt = Date.now();
          void probeOrderbookProgress();
        }
      }, 1000);
    };

    const requestOrderbookSnapshot = async (controller: AbortController) => {
      const response = await fetch(
        `${marketApiBase}/markets/${encodeURIComponent(symbol)}/orderbook?depth=${transportDepth}`,
        { signal: controller.signal },
      );
      if (!response.ok) return null;
      return parseSnapshot(await response.json(), "last_update_id");
    };

    const fetchOrderbookFallback = async () => {
      if (closedByEffect || wsOpen || fallbackInFlight) return;
      const requestGeneration = socketGeneration;
      const controller = new AbortController();
      fallbackAbortController = controller;
      fallbackInFlight = true;
      const timeoutId = window.setTimeout(() => controller.abort(), FALLBACK_TIMEOUT_MS);
      try {
        const snapshot = await requestOrderbookSnapshot(controller);
        if (!snapshot) return;
        if (closedByEffect || wsOpen || requestGeneration !== socketGeneration) return;
        if (!recordOrderbookProgress(snapshot)) return;
        queueSnapshot(snapshot);
      } catch {
        // The next bounded fallback/reconnect attempt retains the last good book.
      } finally {
        window.clearTimeout(timeoutId);
        if (fallbackAbortController === controller) fallbackAbortController = null;
        fallbackInFlight = false;
      }
    };

    const probeOrderbookProgress = async () => {
      if (closedByEffect || !wsOpen || fallbackInFlight) return;
      const requestGeneration = socketGeneration;
      const controller = new AbortController();
      fallbackAbortController = controller;
      fallbackInFlight = true;
      const timeoutId = window.setTimeout(() => controller.abort(), FALLBACK_TIMEOUT_MS);
      try {
        const snapshot = await requestOrderbookSnapshot(controller);
        if (!snapshot || closedByEffect || !wsOpen || requestGeneration !== socketGeneration) return;
        // Compare against the live observation after the REST request finishes.
        // The socket may have caught up while the request was in flight; comparing
        // against the probe-start watermark would then close a healthy connection.
        const newer = snapshot.streamId !== observedOrderbookStreamId
          || observedOrderbookSeq === undefined
          || snapshot.seq > observedOrderbookSeq
          || (snapshot.ts !== undefined && snapshot.ts > (observedOrderbookTs ?? 0));
        if (!newer || !recordOrderbookProgress(snapshot)) return;
        queueSnapshot(snapshot);
        // REST moving while this socket only receives unchanged heartbeats means
        // the channel is stale even though TCP is still open.
        restartSocket();
      } catch {
        // A later bounded probe will retry; the last good book remains visible.
      } finally {
        window.clearTimeout(timeoutId);
        if (fallbackAbortController === controller) fallbackAbortController = null;
        fallbackInFlight = false;
      }
    };

    const startFallback = () => {
      if (closedByEffect || fallbackTimer !== null) return;
      reportStatus(wsOpen ? "open" : "fallback");
      void fetchOrderbookFallback();
      fallbackTimer = window.setInterval(() => void fetchOrderbookFallback(), 1000);
    };

    const stopFallback = () => {
      if (fallbackTimer !== null) {
        window.clearInterval(fallbackTimer);
        fallbackTimer = null;
      }
    };

    const commitOrderbook = () => {
      orderbookCommitTimer = null;
      if (closedByEffect || !pendingSnapshot) return;
      const snapshot = pendingSnapshot;
      pendingSnapshot = null;
      const applyStarted = snapshot.receivedAtPerfMs;
      startTransition(() => {
        setOrderbook(symbol, { bids: snapshot.bids, asks: snapshot.asks }, Date.now(), snapshot.seq, snapshot.streamId);
        observeFrontendApply(performance.now() - applyStarted);
      });
    };

    const scheduleOrderbookCommit = () => {
      if (orderbookCommitTimer !== null) return;
      // 将 50Hz+ 服务端快照合并到默认 20 FPS UI 更新，避免每帧重算100档深度。
      orderbookCommitTimer = window.setTimeout(commitOrderbook, config.orderbookUiIntervalMs);
    };

    const queueSnapshot = (snapshot: PendingSnapshot) => {
      if (
        pendingSnapshot
        && pendingSnapshot.streamId === snapshot.streamId
        && pendingSnapshot.seq !== undefined
        && snapshot.seq !== undefined
        && snapshot.seq < pendingSnapshot.seq
      ) {
        return;
      }
      if (receivedBook && receivedBook.streamId === snapshot.streamId && snapshot.seq < receivedBook.seq) return;
      receivedBook = snapshot;
      pendingSnapshot = snapshot;
      scheduleOrderbookCommit();
    };

    const scheduleReconnect = () => {
      if (closedByEffect || reconnectTimer !== null) return;
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, 1000);
    };

    const restartSocket = () => {
      if (closedByEffect) return;
      const staleSocket = socket;
      socket = null;
      socketGeneration += 1;
      wsOpen = false;
      reportStatus("reconnecting");
      stopWatchdog();
      startFallback();
      scheduleReconnect();
      try {
        staleSocket?.close();
      } catch {
        // Reconnect/fallback are already armed.
      }
    };

    const connect = () => {
      if (closedByEffect) return;
      reportStatus("connecting");
      const generation = ++socketGeneration;
      const currentSocket = new WebSocket(config.publicWsUrl);
      let connectionStreamId: string | undefined;
      socket = currentSocket;

      currentSocket.addEventListener("open", () => {
        if (closedByEffect || generation !== socketGeneration) return;
        wsOpen = true;
        reportStatus("open", Date.now());
        armWatchdog();
        stopFallback();
        fallbackAbortController?.abort();
        currentSocket.send(JSON.stringify({ op: "subscribe", channel: "orderbook", symbol, depth: transportDepth }));
        currentSocket.send(JSON.stringify({ op: "subscribe", channel: "trades", symbol }));
        currentSocket.send(JSON.stringify({ op: "subscribe", channel: "stats", symbol }));
        currentSocket.send(JSON.stringify({ op: "subscribe", channel: "kline", symbol, interval }));
      });

      currentSocket.addEventListener("message", (event: MessageEvent<string>) => {
        if (closedByEffect || generation !== socketGeneration) return;
        let payload: Record<string, any>;
        try {
          payload = JSON.parse(event.data);
        } catch {
          return;
        }
        markTransportFrame();
        if (payload.channel === "orderbook" && payload.type === "snapshot") {
          const snapshot = parseSnapshot(payload, "seq");
          if (!snapshot) return;
          if (connectionStreamId === undefined) connectionStreamId = snapshot.streamId;
          else if (connectionStreamId !== snapshot.streamId) {
            restartSocket();
            return;
          }
          lastOrderbookFrameAt = Date.now();
          if (!recordOrderbookProgress(snapshot)) return;
          queueSnapshot(snapshot);
          return;
        }
        if (payload.channel === "orderbook" && payload.type === "delta") {
          if (!Array.isArray(payload.bids) || !Array.isArray(payload.asks)) return;
          const seq = safeSequence(payload.seq);
          const streamId = validStreamId(payload.stream_id);
          if (seq === undefined || streamId === undefined) return;
          if (connectionStreamId === undefined) connectionStreamId = streamId;
          else if (connectionStreamId !== streamId) {
            restartSocket();
            return;
          }
          const deltaFrame: PendingSnapshot = {
            bids: payload.bids,
            asks: payload.asks,
            ts: finiteNumber(payload.ts),
            seq,
            streamId,
            receivedAtPerfMs: performance.now(),
          };
          lastOrderbookFrameAt = Date.now();
          if (!recordOrderbookProgress(deltaFrame)) return;
          // Merge every delta into the received baseline, then render one full
          // result per UI window. Do not expose intermediate cancel/place rows.
          if (!receivedBook || receivedBook.streamId !== streamId || seq <= receivedBook.seq) return;
          const baseline = receivedBook;
          queueSnapshot({
            ...deltaFrame,
            bids: mergeChanges(baseline.bids, payload.bids, true),
            asks: mergeChanges(baseline.asks, payload.asks, false),
          });
          return;
        }
        if (payload.channel === "trades" && Array.isArray(payload.items)) {
          const items = payload.items
            .filter((item: Record<string, unknown>) => includeSeed || hasVisibleTradeSource(item))
            .map((item: Record<string, unknown>) => ({ ...item, symbol }) as TradeItem);
          if (items.length > 0) prependRecentTrades(symbol, items);
          return;
        }
        if (payload.channel === "stats") {
          patchTicker(symbol, { symbol, ...payload.data });
          setTickerUpdatedAt(Date.now());
          return;
        }
        if (payload.channel === "kline" && payload.interval === interval && payload.kline) {
          if (!isValidKline(payload.kline)) return;
          if (!includeSeed && !hasVisibleTradeSource(payload.kline)) return;
          upsertKline(symbol, payload.kline);
          setKlinesUpdatedAt(Date.now());
        }
      });

      currentSocket.addEventListener("close", () => {
        if (closedByEffect || generation !== socketGeneration) return;
        socket = null;
        wsOpen = false;
        reportStatus("reconnecting");
        stopWatchdog();
        startFallback();
        scheduleReconnect();
      });
      currentSocket.addEventListener("error", () => {
        if (generation === socketGeneration) restartSocket();
      });
    };

    const handleVisibilityChange = () => {
      if (closedByEffect || document.visibilityState !== "visible") return;
      if (pendingSnapshot) {
        if (orderbookCommitTimer !== null) window.clearTimeout(orderbookCommitTimer);
        commitOrderbook();
      }
      if (wsOpen && Date.now() - lastOrderbookFrameAt > WATCHDOG_MS) restartSocket();
      else if (wsOpen && Date.now() - lastOrderbookProgressAt > WATCHDOG_MS) void probeOrderbookProgress();
    };

    connect();
    startFallback();
    document.addEventListener("visibilitychange", handleVisibilityChange);

    return () => {
      closedByEffect = true;
      socketGeneration += 1;
      reportStatus("idle");
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer);
      }
      if (orderbookCommitTimer !== null) {
        window.clearTimeout(orderbookCommitTimer);
      }
      fallbackAbortController?.abort();
      stopWatchdog();
      stopFallback();
      socket?.close();
    };
  }, [symbol, interval, depth, includeSeed, apiPrefix, setOrderbook, prependRecentTrades, patchTicker, upsertKline, setPublicStreamStatus, setTickerUpdatedAt, setKlinesUpdatedAt]);
}
