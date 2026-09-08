import { useMemo, useState } from "react";
import { bjDateTime, bjTime, fmt, sideColor } from "../lib/format";
import type { LedgerItem, OrderItem, TradeItem } from "../types";

type Tab = "open" | "history" | "trades" | "ledger";
type SideFilter = "all" | "buy" | "sell";
type OpenSort = "time_desc" | "time_asc" | "side";
type TradeRoleFilter = "all" | "maker" | "taker";

export function OrdersTabs({
  openOrders,
  orderHistory,
  accountTrades,
  ledger,
  priceDigits,
  quantityDigits,
  onCancel,
}: {
  openOrders: OrderItem[];
  orderHistory: OrderItem[];
  accountTrades: TradeItem[];
  ledger: LedgerItem[];
  priceDigits: number;
  quantityDigits: number;
  onCancel: (orderId: string) => Promise<void>;
}) {
  const [tab, setTab] = useState<Tab>("open");
  const [searchText, setSearchText] = useState("");
  const [openFilter, setOpenFilter] = useState<SideFilter>("all");
  const [openSort, setOpenSort] = useState<OpenSort>("time_desc");
  const [historySide, setHistorySide] = useState<SideFilter>("all");
  const [historyStatus, setHistoryStatus] = useState("all");
  const [tradeSide, setTradeSide] = useState<SideFilter>("all");
  const [tradeRole, setTradeRole] = useState<TradeRoleFilter>("all");
  const [ledgerAsset, setLedgerAsset] = useState("all");
  const [ledgerType, setLedgerType] = useState("all");
  const tabs: [Tab, string][] = [
    ["open", "当前订单"],
    ["history", "历史订单"],
    ["trades", "成交记录"],
    ["ledger", "账本流水"],
  ];
  const normalizedSearch = searchText.trim().toLowerCase();
  const historyStatuses = useMemo(() => uniqueValues(orderHistory.map((item) => item.status)), [orderHistory]);
  const ledgerAssets = useMemo(() => uniqueValues(ledger.map((item) => item.asset)), [ledger]);
  const ledgerTypes = useMemo(() => uniqueValues(ledger.map((item) => item.change_type)), [ledger]);
  const filteredOpenOrders = [...openOrders]
    .filter((item) => openFilter === "all" || item.side === openFilter)
    .filter((item) => matchesSearch(normalizedSearch, item.order_id, item.client_order_id, item.status, item.type))
    .sort((a, b) => {
      if (openSort === "time_asc") return a.created_at - b.created_at;
      if (openSort === "side") return a.side.localeCompare(b.side) || b.created_at - a.created_at;
      return b.created_at - a.created_at;
    });
  const filteredHistory = orderHistory.filter((item) => {
    if (historySide !== "all" && item.side !== historySide) return false;
    if (historyStatus !== "all" && item.status !== historyStatus) return false;
    return matchesSearch(normalizedSearch, item.order_id, item.client_order_id, item.status, item.type, item.reject_reason);
  });
  const filteredTrades = accountTrades.filter((item) => {
    const side = item.side ?? item.taker_side;
    if (tradeSide !== "all" && side !== tradeSide) return false;
    if (tradeRole !== "all" && item.liquidity_role !== tradeRole) return false;
    return matchesSearch(normalizedSearch, item.trade_id, item.symbol, item.liquidity_role, item.fee_asset);
  });
  const filteredLedger = ledger.filter((item) => {
    if (ledgerAsset !== "all" && item.asset !== ledgerAsset) return false;
    if (ledgerType !== "all" && item.change_type !== ledgerType) return false;
    return matchesSearch(normalizedSearch, item.entry_id, item.asset, item.change_type, item.related_order_id, item.related_trade_id, item.note);
  });
  const visibleCount =
    tab === "open" ? filteredOpenOrders.length : tab === "history" ? filteredHistory.length : tab === "trades" ? filteredTrades.length : filteredLedger.length;
  const totalCount = tab === "open" ? openOrders.length : tab === "history" ? orderHistory.length : tab === "trades" ? accountTrades.length : ledger.length;

  const clearFilters = () => {
    setSearchText("");
    setOpenFilter("all");
    setOpenSort("time_desc");
    setHistorySide("all");
    setHistoryStatus("all");
    setTradeSide("all");
    setTradeRole("all");
    setLedgerAsset("all");
    setLedgerType("all");
  };

  return (
    <section className="panel flex h-[620px] min-h-0 flex-col rounded-3xl p-4">
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <div className="flex flex-wrap gap-2">
          {tabs.map(([value, label]) => (
            <button
              key={value}
              onClick={() => setTab(value)}
              className={`rounded-full border px-4 py-2 text-sm transition ${
                tab === value
                  ? "border-cyan-300/30 bg-cyan-400/16 text-cyan-100"
                  : "border-transparent bg-white/5 text-slate-300 hover:bg-white/8"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="ml-auto text-xs text-slate-400">
          {visibleCount} / {totalCount}
        </div>
      </div>
      <div className="mb-4 grid gap-2 xl:grid-cols-[1fr_auto]">
        <input
          value={searchText}
          onChange={(event) => setSearchText(event.target.value)}
          placeholder="搜索订单号、client id、成交号、账本关联号"
          className="h-10 w-full rounded-2xl border border-white/10 bg-white/5 px-3 text-sm text-slate-100 outline-none placeholder:text-slate-600"
        />
        <button type="button" onClick={clearFilters} className="h-10 rounded-2xl bg-white/8 px-4 text-sm text-slate-200">
          清空筛选
        </button>
      </div>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        {tab === "open" && (
          <>
            <SideSegment value={openFilter} onChange={setOpenFilter} />
            <SelectFilter label="排序" value={openSort} onChange={(value) => setOpenSort(value as OpenSort)} options={openSortOptions} />
          </>
        )}
        {tab === "history" && (
          <>
            <SideSegment value={historySide} onChange={setHistorySide} />
            <SelectFilter label="状态" value={historyStatus} onChange={setHistoryStatus} options={[["all", "全部状态"], ...historyStatuses.map((item) => [item, item] as [string, string])]} />
          </>
        )}
        {tab === "trades" && (
          <>
            <SideSegment value={tradeSide} onChange={setTradeSide} />
            <SelectFilter label="角色" value={tradeRole} onChange={(value) => setTradeRole(value as TradeRoleFilter)} options={tradeRoleOptions} />
          </>
        )}
        {tab === "ledger" && (
          <>
            <SelectFilter label="资产" value={ledgerAsset} onChange={setLedgerAsset} options={[["all", "全部资产"], ...ledgerAssets.map((item) => [item, item] as [string, string])]} />
            <SelectFilter label="类型" value={ledgerType} onChange={setLedgerType} options={[["all", "全部类型"], ...ledgerTypes.map((item) => [item, item] as [string, string])]} />
          </>
        )}
      </div>
      <div className="scrollbar flex-1 overflow-auto">
        {tab === "open" && (
          <table className="min-w-full text-left text-sm">
            <thead className="text-slate-500">
              <tr>
                <th className="pb-2">时间</th><th className="pb-2">方向</th><th className="pb-2">价格</th><th className="pb-2">数量</th><th className="pb-2">状态</th><th className="pb-2"></th>
              </tr>
            </thead>
            <tbody>
              {filteredOpenOrders.slice(0, 500).map((item) => (
                <tr key={item.order_id} className="border-t border-white/5">
                  <td className="py-3 text-slate-400">{bjDateTime(item.created_at)}</td>
                  <td className={`py-3 ${sideColor(item.side)}`}>{item.side}</td>
                  <td className="py-3">{fmt(item.price, priceDigits)}</td>
                  <td className="py-3">{fmt(item.quantity, quantityDigits)}</td>
                  <td className="py-3">{item.status}</td>
                  <td className="py-3 text-right">
                    <button onClick={() => onCancel(item.order_id)} className="rounded-lg bg-white/6 px-3 py-1 text-xs text-slate-200">撤单</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {tab === "history" && (
          <div className="space-y-2">
            {filteredHistory.map((item) => (
              <div key={item.order_id} className="grid gap-2 rounded-2xl border border-white/6 bg-white/5 px-4 py-3 md:grid-cols-[1.2fr_0.8fr_0.8fr_0.8fr_0.8fr]">
                <div className="text-slate-300">{bjDateTime(item.created_at)}</div>
                <div className={sideColor(item.side)}>{item.side} / {item.type}</div>
                <div>{fmt(item.price, priceDigits)}</div>
                <div>{fmt(item.filled_quantity, quantityDigits)} / {fmt(item.quantity, quantityDigits)}</div>
                <div>
                  <span className="rounded-full bg-white/6 px-2 py-1 text-xs text-slate-300">{item.status}</span>
                </div>
                <div className="md:col-span-5 truncate font-mono text-[11px] text-slate-500">
                  {item.client_order_id ? `client ${item.client_order_id}` : `order ${item.order_id}`}
                </div>
                {item.reject_reason && (
                  <div className="md:col-span-5 rounded-xl bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
                    拒单原因：{item.reject_reason}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
        {tab === "trades" && (
          <div className="space-y-2">
            {filteredTrades.map((item) => (
              <div key={item.trade_id} className="grid gap-2 rounded-2xl bg-white/5 px-4 py-3 md:grid-cols-[1fr_0.7fr_0.8fr_0.8fr_1fr]">
                <div className="text-slate-400">{bjDateTime(item.ts ?? Date.now())}</div>
                <div className={sideColor(item.side)}>{item.side}</div>
                <div>{fmt(item.price, priceDigits)}</div>
                <div>{fmt(item.quantity, quantityDigits)}</div>
                <div className="text-slate-400">{item.liquidity_role} / fee {fmt(item.fee, 6)} {item.fee_asset}</div>
              </div>
            ))}
          </div>
        )}
        {tab === "ledger" && (
          <div className="space-y-2">
            {filteredLedger.map((item) => (
              <div key={item.entry_id} className="grid gap-2 rounded-2xl bg-white/5 px-4 py-3 md:grid-cols-[1.1fr_0.8fr_0.7fr_1.2fr_1.2fr]">
                <div className="text-slate-400">{bjDateTime(item.created_at)}</div>
                <div>{item.asset}</div>
                <div>{item.change_type}</div>
                <div>{fmt(item.amount, 8)}</div>
                <div className="text-slate-500">A {fmt(item.available_before, 4)}→{fmt(item.available_after, 4)} / F {fmt(item.frozen_before, 4)}→{fmt(item.frozen_after, 4)}</div>
                {(item.related_order_id || item.related_trade_id || item.note) && (
                  <div className="md:col-span-5 truncate font-mono text-[11px] text-slate-500">
                    {[item.related_order_id ? `order ${item.related_order_id}` : "", item.related_trade_id ? `trade ${item.related_trade_id}` : "", item.note ?? ""]
                      .filter(Boolean)
                      .join(" · ")}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
        {visibleCount === 0 && (
          <div className="rounded-2xl border border-white/8 bg-white/5 px-4 py-6 text-center text-sm text-slate-400">
            当前筛选下没有记录。
          </div>
        )}
      </div>
    </section>
  );
}

const sideOptions: [SideFilter, string][] = [
  ["all", "全部"],
  ["buy", "只看买单"],
  ["sell", "只看卖单"],
];

const openSortOptions: [string, string][] = [
  ["time_desc", "时间倒序"],
  ["time_asc", "时间正序"],
  ["side", "方向优先"],
];

const tradeRoleOptions: [string, string][] = [
  ["all", "全部角色"],
  ["maker", "maker"],
  ["taker", "taker"],
];

function SideSegment({ value, onChange }: { value: SideFilter; onChange: (value: SideFilter) => void }) {
  return (
    <div className="flex flex-wrap gap-2">
      {sideOptions.map(([nextValue, label]) => (
        <button
          key={nextValue}
          onClick={() => onChange(nextValue)}
          className={`rounded-full px-3 py-1.5 text-xs transition ${
            value === nextValue ? "bg-cyan-400/16 text-cyan-100" : "bg-white/5 text-slate-300 hover:bg-white/8"
          }`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

function SelectFilter({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: [string, string][];
}) {
  return (
    <label className="flex items-center gap-2 text-xs text-slate-400">
      <span>{label}</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-9 rounded-full border border-white/10 bg-white/5 px-3 text-slate-200 outline-none"
      >
        {options.map(([optionValue, optionLabel]) => (
          <option key={optionValue} value={optionValue}>
            {optionLabel}
          </option>
        ))}
      </select>
    </label>
  );
}

function uniqueValues(values: Array<string | undefined | null>) {
  return Array.from(new Set(values.filter((item): item is string => Boolean(item)))).sort((a, b) => a.localeCompare(b));
}

function matchesSearch(search: string, ...values: Array<string | number | null | undefined>) {
  if (!search) return true;
  return values.some((value) => String(value ?? "").toLowerCase().includes(search));
}
