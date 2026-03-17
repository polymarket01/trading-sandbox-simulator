import { useEffect, useEffectEvent } from "react";
import { api } from "../api/client";
import { signPrivateWs } from "../lib/crypto";
import { config } from "../lib/config";
import { useAppStore } from "../store/useAppStore";

export function usePrivateStream(symbol: string) {
  const setBalances = useAppStore((state) => state.setBalances);
  const upsertOrder = useAppStore((state) => state.upsertOrder);
  const setAccountTrades = useAppStore((state) => state.setAccountTrades);
  const setLedger = useAppStore((state) => state.setLedger);

  const refreshPanels = useEffectEvent(async () => {
    const [trades, ledger] = await Promise.all([
      api.get<{ items: unknown[] }>(`/account/trades?symbol=${symbol}&limit=100`, config.manualApiKey),
      api.get<{ items: unknown[] }>("/account/ledger?limit=100", config.manualApiKey),
    ]);
    setAccountTrades(trades.items as never[]);
    setLedger(ledger.items as never[]);
  });

  const onMessage = useEffectEvent((event: MessageEvent<string>) => {
    const payload = JSON.parse(event.data);
    if (payload.channel === "balances") setBalances(payload.data);
    if (payload.channel === "orders" && payload.type === "update") {
      upsertOrder(payload.data);
      void refreshPanels();
    }
    if (payload.channel === "orders" && payload.type === "snapshot") {
      payload.items.forEach((item: unknown) => upsertOrder(item as never));
    }
  });

  useEffect(() => {
    let socket: WebSocket | undefined;
    void (async () => {
      const timestamp = Date.now();
      const signature = await signPrivateWs(config.manualApiKey, config.manualApiSecret, timestamp);
      socket = new WebSocket(config.privateWsUrl);
      socket.addEventListener("open", () => {
        socket?.send(JSON.stringify({ op: "auth", api_key: config.manualApiKey, timestamp, signature }));
      });
      socket.addEventListener("message", (event) => {
        const payload = JSON.parse(event.data);
        if (payload.type === "auth_ok") {
          socket?.send(JSON.stringify({ op: "subscribe", channel: "balances" }));
          socket?.send(JSON.stringify({ op: "subscribe", channel: "orders" }));
          return;
        }
        onMessage(event);
      });
    })();
    return () => socket?.close();
  }, [onMessage]);
}
