import { useEffect } from "react";
import { config } from "../lib/config";
import { useAppStore } from "../store/useAppStore";

export function useMarketStreams(symbol: string, interval: string, depth: number) {
  const setOrderbook = useAppStore((state) => state.setOrderbook);
  const applyOrderbookDelta = useAppStore((state) => state.applyOrderbookDelta);
  const prependRecentTrades = useAppStore((state) => state.prependRecentTrades);
  const patchTicker = useAppStore((state) => state.patchTicker);
  const upsertKline = useAppStore((state) => state.upsertKline);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let reconnectTimer: number | null = null;
    let closedByEffect = false;

    const scheduleReconnect = () => {
      if (closedByEffect || reconnectTimer !== null) return;
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, 1000);
    };

    const connect = () => {
      if (closedByEffect) return;
      socket = new WebSocket(config.publicWsUrl);

      socket.addEventListener("open", () => {
        socket?.send(JSON.stringify({ op: "subscribe", channel: "orderbook", symbol, depth }));
        socket?.send(JSON.stringify({ op: "subscribe", channel: "trades", symbol }));
        socket?.send(JSON.stringify({ op: "subscribe", channel: "stats", symbol }));
        socket?.send(JSON.stringify({ op: "subscribe", channel: "kline", symbol, interval }));
      });

      socket.addEventListener("message", (event: MessageEvent<string>) => {
        const payload = JSON.parse(event.data);
        if (payload.channel === "orderbook" && payload.type === "snapshot") {
          setOrderbook({ bids: payload.bids, asks: payload.asks });
          return;
        }
        if (payload.channel === "orderbook" && payload.type === "delta") {
          applyOrderbookDelta({ bids: payload.bids, asks: payload.asks });
          return;
        }
        if (payload.channel === "trades" && Array.isArray(payload.items)) {
          prependRecentTrades(payload.items.map((item: Record<string, unknown>) => ({ ...item, symbol })));
          return;
        }
        if (payload.channel === "stats") {
          patchTicker({ symbol, ...payload.data });
          return;
        }
        if (payload.channel === "kline" && payload.interval === interval && payload.kline) {
          upsertKline(payload.kline);
        }
      });

      socket.addEventListener("close", scheduleReconnect);
      socket.addEventListener("error", () => {
        socket?.close();
      });
    };

    connect();

    return () => {
      closedByEffect = true;
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer);
      }
      socket?.close();
    };
  }, [symbol, interval, depth, setOrderbook, applyOrderbookDelta, prependRecentTrades, patchTicker, upsertKline]);
}
