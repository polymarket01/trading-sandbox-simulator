import { useEffect, useEffectEvent } from "react";
import { signPrivateWs } from "../lib/crypto";
import { config } from "../lib/config";
import { useAppStore } from "../store/useAppStore";
import type { LedgerItem, PaperPerpAccount, PaperPosition, TradeItem } from "../types";

export function usePrivateStream(_symbol: string) {
  const setBalances = useAppStore((state) => state.setBalances);
  const upsertOrder = useAppStore((state) => state.upsertOrder);
  const setAccountTrades = useAppStore((state) => state.setAccountTrades);
  const prependAccountTrades = useAppStore((state) => state.prependAccountTrades);
  const setLedger = useAppStore((state) => state.setLedger);
  const prependLedgerEntries = useAppStore((state) => state.prependLedgerEntries);
  const setPrivateStreamStatus = useAppStore((state) => state.setPrivateStreamStatus);
  const setPaperPerpAccount = useAppStore((state) => state.setPaperPerpAccount);
  const setPaperPositions = useAppStore((state) => state.setPaperPositions);
  const pushToast = useAppStore((state) => state.pushToast);
  const authSession = useAppStore((state) => state.authSession);
  const apiKey = authSession?.api_key;
  const apiSecret = authSession?.api_secret;
  const cookieSession = Boolean(authSession && !apiKey && !apiSecret);

  const onMessage = useEffectEvent((event: MessageEvent<string>) => {
    const payload = JSON.parse(event.data);
    setPrivateStreamStatus("open", Date.now());
    if (payload.channel === "balances") setBalances(payload.data);
    if (payload.channel === "orders" && payload.type === "update") {
      upsertOrder(payload.data);
    }
    if (payload.channel === "orders" && payload.type === "snapshot") {
      payload.items.forEach((item: unknown) => upsertOrder(item as never));
    }
    if (payload.channel === "trades" && payload.type === "snapshot") {
      setAccountTrades((payload.items ?? []) as TradeItem[]);
    }
    if (payload.channel === "trades" && payload.type === "update") {
      prependAccountTrades((payload.items ?? []) as TradeItem[]);
    }
    if (payload.channel === "ledger" && payload.type === "snapshot") {
      setLedger((payload.items ?? []) as LedgerItem[]);
    }
    if (payload.channel === "ledger" && payload.type === "update") {
      prependLedgerEntries((payload.items ?? []) as LedgerItem[]);
    }
    if (payload.channel === "contracts" && payload.type === "snapshot") {
      if (payload.account) setPaperPerpAccount(payload.account as PaperPerpAccount);
      if (Array.isArray(payload.positions)) setPaperPositions(payload.positions as PaperPosition[]);
    }
    if (payload.channel === "contracts" && payload.type === "update") {
      if (payload.account) setPaperPerpAccount(payload.account as PaperPerpAccount);
      if (Array.isArray(payload.positions)) setPaperPositions(payload.positions as PaperPosition[]);
    }
    if (payload.type === "account_reset") {
      pushToast("info", "模拟账户已复位，已开启新的账户运行批次");
    }
    if (payload.type === "system_notice" || payload.channel === "system_notice") {
      if (typeof payload.message === "string" && payload.message.length > 0) {
        pushToast("info", payload.message);
      }
    }
  });

  useEffect(() => {
    let socket: WebSocket | undefined;
    let retryTimer: number | undefined;
    let watchdogTimer: number | undefined;
    let closedByEffect = false;
    let wsOpen = false;
    let lastMessageAt = 0;
    let lastErrorToastAt = 0;
    let lastErrorToastText = "";
    if (!authSession || (!cookieSession && (!apiKey || !apiSecret))) return () => undefined;
    const stopWatchdog = () => {
      if (watchdogTimer !== undefined) {
        window.clearInterval(watchdogTimer);
        watchdogTimer = undefined;
      }
    };
    const connect = () => {
      if (closedByEffect) return;
      setPrivateStreamStatus("connecting");
      void (async () => {
        const timestamp = Date.now();
        const signature = cookieSession ? "" : await signPrivateWs(apiKey ?? "", apiSecret ?? "", timestamp);
        if (closedByEffect) return;
        const ws = new WebSocket(config.privateWsUrl);
        socket = ws;
        ws.addEventListener("open", () => {
          wsOpen = true;
          lastMessageAt = Date.now();
          stopWatchdog();
          watchdogTimer = window.setInterval(() => {
            if (wsOpen && Date.now() - lastMessageAt > 8000) ws.close();
          }, 1000);
          ws.send(JSON.stringify(cookieSession ? { op: "auth" } : { op: "auth", api_key: apiKey, timestamp, signature }));
        });
        ws.addEventListener("message", (event) => {
          lastMessageAt = Date.now();
          const payload = JSON.parse(event.data);
          if (payload.type === "auth_ok") {
            setPrivateStreamStatus("open", Date.now());
            ws.send(JSON.stringify({ op: "subscribe", channel: "balances" }));
            ws.send(JSON.stringify({ op: "subscribe", channel: "orders" }));
            ws.send(JSON.stringify({ op: "subscribe", channel: "trades" }));
            ws.send(JSON.stringify({ op: "subscribe", channel: "ledger" }));
            ws.send(JSON.stringify({ op: "subscribe", channel: "contracts" }));
            return;
          }
          if (payload.type === "error") {
            const errorText = typeof payload.detail === "string" ? payload.detail : "私有连接错误";
            const now = Date.now();
            if (errorText !== lastErrorToastText || now - lastErrorToastAt >= 5000) {
              pushToast("error", errorText);
              lastErrorToastText = errorText;
              lastErrorToastAt = now;
            }
            return;
          }
          onMessage(event);
        });
        ws.addEventListener("close", () => {
          wsOpen = false;
          setPrivateStreamStatus("reconnecting");
          stopWatchdog();
          if (!closedByEffect && retryTimer === undefined) {
            retryTimer = window.setTimeout(() => {
              retryTimer = undefined;
              connect();
            }, 1000);
          }
        });
        ws.addEventListener("error", () => {
          // Some engines surface a failed handshake as an error without a
          // close event. Force-close so the retry path above always fires;
          // the REST account poller keeps data fresh while reconnecting.
          wsOpen = false;
          stopWatchdog();
          try {
            ws.close();
          } catch {
            // Already closing; the close handler will schedule the retry.
          }
        });
      })();
    };
    connect();
    return () => {
      closedByEffect = true;
      setPrivateStreamStatus("idle");
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
      stopWatchdog();
      socket?.close();
    };
  }, [apiKey, apiSecret, authSession, cookieSession]);
}
