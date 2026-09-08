import type { Level } from "../types";

type DecimalUnits = { units: bigint; scale: number };

const decimalUnits = (text: string): DecimalUnits | null => {
  const match = /^\+?(\d+)(?:\.(\d*))?(?:e([+-]?\d+))?$/i.exec(text.trim());
  if (!match) return null;
  const fraction = match[2] ?? "";
  const exponent = Number(match[3] ?? 0);
  if (!Number.isSafeInteger(exponent) || Math.abs(exponent) > 100 || fraction.length > 100) return null;
  let units = BigInt(`${match[1]}${fraction}`);
  let scale = fraction.length - exponent;
  if (scale < 0) {
    units *= 10n ** BigInt(-scale);
    scale = 0;
  }
  return { units, scale };
};

const rescale = (value: DecimalUnits, scale: number) => value.units * 10n ** BigInt(scale - value.scale);

const decimalText = (units: bigint, scale: number) => {
  if (scale === 0) return String(units);
  const digits = String(units).padStart(scale + 1, "0");
  return `${digits.slice(0, -scale)}.${digits.slice(-scale)}`.replace(/\.?0+$/, "");
};

export function mergeLevels(levels: Level[], side: "buy" | "sell", tickSize: number, mergeTicks = 1): Level[] {
  if (!Number.isFinite(tickSize) || tickSize <= 0 || !Number.isSafeInteger(mergeTicks) || mergeTicks < 1) return levels;
  const tick = decimalUnits(String(tickSize));
  if (!tick) return levels;
  const parsed = levels.flatMap(([priceText, quantityText]) => {
    const price = decimalUnits(priceText);
    const quantity = decimalUnits(quantityText);
    return price && quantity && price.units > 0n && quantity.units > 0n ? [{ price, quantity }] : [];
  });
  const priceScale = Math.max(tick.scale, ...parsed.map(({ price }) => price.scale));
  const quantityScale = Math.max(0, ...parsed.map(({ quantity }) => quantity.scale));
  // Divide integer decimal units: 79012.2 / 0.1 must not become
  // 790121.9999999999 and merge two distinct prices into a doubled bid.
  // Keep tick and multiplier separate to avoid introducing float error first.
  const group = rescale(tick, priceScale) * BigInt(mergeTicks);
  const buckets = new Map<bigint, bigint>();
  for (const { price, quantity } of parsed) {
    const priceUnits = rescale(price, priceScale);
    const bucket = (side === "buy" ? priceUnits / group : (priceUnits + group - 1n) / group) * group;
    buckets.set(bucket, (buckets.get(bucket) ?? 0n) + rescale(quantity, quantityScale));
  }
  return Array.from(buckets.entries())
    .sort((a, b) => (a[0] === b[0] ? 0 : (a[0] < b[0] ? -1 : 1) * (side === "buy" ? -1 : 1)))
    .map(([price, quantity]) => [decimalText(price, priceScale), decimalText(quantity, quantityScale)] as Level);
}

export function summarizeOrderbook(bids: Level[], asks: Level[]) {
  const bestBid = Number(bids[0]?.[0] ?? 0);
  const bestAsk = Number(asks[0]?.[0] ?? 0);
  const mid = bestBid > 0 && bestAsk > 0 ? (bestBid + bestAsk) / 2 : bestBid || bestAsk || 0;
  const spread = bestBid > 0 && bestAsk > 0 ? bestAsk - bestBid : 0;
  const spreadPct = mid > 0 ? (spread / mid) * 100 : 0;

  const depthWithin = (limitPct: number, levels: Level[], side: "buy" | "sell") => {
    if (mid <= 0) return 0;
    let total = 0;
    for (const [priceText, quantityText] of levels) {
      const price = Number(priceText);
      const quantity = Number(quantityText);
      if (side === "buy" && price < mid * (1 - limitPct)) {
        continue;
      }
      if (side === "sell" && price > mid * (1 + limitPct)) {
        continue;
      }
      total += price * quantity;
    }
    return total;
  };

  const bidDepth05 = depthWithin(0.005, bids, "buy");
  const askDepth05 = depthWithin(0.005, asks, "sell");
  const bidDepth2 = depthWithin(0.02, bids, "buy");
  const askDepth2 = depthWithin(0.02, asks, "sell");

  return {
    best_bid: String(bestBid),
    best_ask: String(bestAsk),
    mid_price: String(mid),
    spread: String(spread),
    spread_pct: String(spreadPct),
    depth_amount_0_5pct_bid: String(bidDepth05),
    depth_amount_0_5pct_ask: String(askDepth05),
    depth_amount_0_5pct: String(bidDepth05 + askDepth05),
    depth_amount_2pct_bid: String(bidDepth2),
    depth_amount_2pct_ask: String(askDepth2),
    depth_amount_2pct: String(bidDepth2 + askDepth2),
  };
}
