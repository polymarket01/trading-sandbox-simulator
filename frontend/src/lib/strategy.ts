export type StrategyOption = {
  strategy_key: string;
  display_name?: string;
  products?: string[];
};

const STRATEGY_DISPLAY_NAMES: Record<string, string> = {
  LITE: "Lite 精简做市",
  LITE_RELATIVE: "Lite 相对做市",
  PERP_MM: "PERP_MM 合约做市",
  SIMPLE_BBO: "极简四档铺单",
  CONTRACT_LADDER: "CONTRACT_LADDER 内部合约铺单",
};

export function normalizedStrategyKey(value?: string | null) {
  return String(value ?? "").trim().toUpperCase();
}

export function strategyDisplayName(value?: string | null) {
  const key = normalizedStrategyKey(value);
  if (!key) return "-";
  return STRATEGY_DISPLAY_NAMES[key] ?? key;
}

export function strategyOptionsForProduct(productType?: string | null, options: StrategyOption[] = []) {
  const legacyProducts:Record<string,string[]> = {LITE:["SPOT"], PERP_MM:["PERP"], CONTRACT_LADDER:["PERP"], SIMPLE_BBO:["SPOT","PERP"]};
  return Array.from(new Map(options
    .filter(item => {const products=item.products || legacyProducts[item.strategy_key]; return !products || products.includes(productType || "SPOT");})
    .map(item => ({...item, strategy_key: normalizedStrategyKey(item.strategy_key), display_name: item.display_name || strategyDisplayName(item.strategy_key)}))
    .filter(item => item.strategy_key)
    .map(item => [item.strategy_key, item])).values());
}
