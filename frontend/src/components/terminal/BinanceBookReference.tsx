import { useEffect, useState } from 'react';
import { api } from '../../api/client';
import { bookDeviationBps, parseBinanceBookTicker } from '../../lib/bookReference';

type Quote = {bid: number; ask: number; observedAt: number; transport: string; source: string; local: string};
export function BinanceBookReference({symbol, bid, ask}: {symbol: string; bid: number; ask: number}) {
  const [mapping, setMapping] = useState<{local: string; source: string} | null>(null);
  const [quote, setQuote] = useState<Quote | null>(null);
  const [now, setNow] = useState(Date.now());
  const [message, setMessage] = useState('读取价格源');
  useEffect(() => {
    let disposed = false;
    let timer: number;
    const refresh = async () => {
      try {
        const result = await api.get<{items: {symbol: string; price_source_symbol?: string; price_source?: string}[]}>('/paper/markets');
        const market = result.items.find(x => x.symbol === symbol);
        const source = (market?.price_source_symbol || market?.symbol.replace(/-PERP$/, ''))?.trim().toUpperCase();
        if (!disposed) {
          setMapping(source && /^[A-Z0-9]+$/.test(source) ? {local:symbol, source} : null);
          setMessage(source ? '连接中' : '未配置币安映射');
        }
      } catch { if (!disposed) { setMapping(null); setMessage('价格源暂不可用'); } }
      if (!disposed) timer = window.setTimeout(refresh, 30000);
    };
    setMapping(null); setQuote(null); setMessage('读取价格源'); void refresh();
    return () => { disposed = true; window.clearTimeout(timer); };
  }, [symbol]);
  const source = mapping?.local === symbol ? mapping.source : undefined;
  useEffect(() => {
    if (!source) return;
    let stopped = false;
    let socket: WebSocket | undefined;
    let reconnect: number;
    let pending: Quote | null = null;
    let lastReceived = 0;
    let restAt = 0;
    let controller: AbortController | null = null;
    const accept = (data: unknown, transport: string) => {
      const parsed = parseBinanceBookTicker(data, source);
      if (!parsed || stopped || (pending && parsed.observedAt < pending.observedAt)) return;
      pending = {...parsed, source, local:symbol, transport}; lastReceived = Date.now();
    };
    const connect = () => {
      if (stopped) return;
      socket = new WebSocket(`wss://fstream.binance.com/public/ws/${source.toLowerCase()}@bookTicker`);
      socket.onmessage = event => { try { accept(JSON.parse(event.data), 'WS'); } catch {} };
      socket.onclose = () => { if (!stopped) reconnect = window.setTimeout(connect, 2000); };
    };
    const tick = async () => {
      if (stopped) return;
      setNow(Date.now()); if (pending) setQuote(pending);
      if (Date.now() - lastReceived < 5000 || Date.now() - restAt < 5000 || controller) return;
      restAt = Date.now(); const abort = new AbortController(); controller = abort;
      const timeout = window.setTimeout(() => abort.abort(), 2500);
      try {
        const response = await fetch(`https://fapi.binance.com/fapi/v1/ticker/bookTicker?symbol=${encodeURIComponent(source)}`, {signal:abort.signal});
        if (response.ok) {
          const data = await response.json();
          // A successful REST read verifies the present BBO, even in a quiet market.
          if (lastReceived <= restAt) accept({...data, time:Date.now()}, 'REST');
        }
      } catch { if (!stopped) setMessage('币安行情暂不可用'); }
      finally { window.clearTimeout(timeout); if (controller === abort) controller = null; }
    };
    setQuote(null); connect(); void tick();
    const timer = window.setInterval(() => void tick(), 250);
    return () => { stopped = true; socket?.close(); window.clearTimeout(reconnect); window.clearInterval(timer); controller?.abort(); };
  }, [source, symbol]);
  const current = quote?.local === symbol && quote.source === source ? quote : null;
  const stale = !current || now - current.observedAt > 5000;
  const price = (value?: number) => value ? value.toLocaleString('en-US', {maximumFractionDigits:8}) : '—';
  const deviation = (value: number, reference?: number) => {
    const bps = stale ? null : bookDeviationBps(value, reference ?? 0);
    return bps === null ? '—' : `${bps >= 0 ? '+' : ''}${bps.toFixed(2)} bps`;
  };
  return <div className="my-1 shrink-0 rounded border border-white/10 px-2 py-1 text-[10px]" aria-label="币安合约盘口对照">
    <div className="flex justify-between gap-2 text-slate-500"><span>币安合约 · {source || '—'} · bookTicker</span><span>{current ? stale ? '参考已过期' : current.transport : message}</span></div>
    <div className="mt-1 grid grid-cols-2 gap-3 tabular-nums">
      <div><div className="flex justify-between gap-1"><span className="text-slate-500">币安买一</span><span className={stale ? 'text-slate-500' : 'text-emerald-200'}>{price(current?.bid)}</span></div><div className="flex justify-between gap-1" title="(本地原始买一 / 币安合约买一 − 1) × 10000"><span className="text-slate-500">本地买价差</span><span>{deviation(bid,current?.bid)}</span></div></div>
      <div><div className="flex justify-between gap-1"><span className="text-slate-500">币安卖一</span><span className={stale ? 'text-slate-500' : 'text-rose-200'}>{price(current?.ask)}</span></div><div className="flex justify-between gap-1" title="(本地原始卖一 / 币安合约卖一 − 1) × 10000"><span className="text-slate-500">本地卖价差</span><span>{deviation(ask,current?.ask)}</span></div></div>
    </div>
  </div>;
}
