export function bookDeviationBps(local: number, reference: number): number | null {
  return Number.isFinite(local) && Number.isFinite(reference) && local > 0 && reference > 0
    ? (local / reference - 1) * 10000 : null;
}
export function parseBinanceBookTicker(payload: any, symbol: string) {
  if (String(payload?.s ?? payload?.symbol ?? '').toUpperCase() !== symbol.toUpperCase()) return null;
  const bid = Number(payload.b ?? payload.bidPrice);
  const ask = Number(payload.a ?? payload.askPrice);
  if (!Number.isFinite(bid) || !Number.isFinite(ask) || bid <= 0 || ask <= 0 || bid > ask) return null;
  const time = Number(payload.E ?? payload.time ?? payload.T);
  return {bid, ask, observedAt: Number.isFinite(time) && time > 0 ? time : Date.now()};
}
