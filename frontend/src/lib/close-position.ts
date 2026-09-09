import type { ContractPosition, ContractSetting, OrderItem } from "../types";

type CloseApi = {
  get<T>(path: string, key: string): Promise<T>;
  delete<T>(path: string, key: string): Promise<T>;
};

// A one-way market has one net position; either order side can change it.
export function belongsToPosition(order: OrderItem, symbol: string, side: string, mode: string) {
  if (order.symbol !== symbol) return false;
  if (mode === "one_way") return true;
  const closing = order.reduce_only || order.position_action === "close";
  const direction = closing ? (order.side === "sell" ? "long" : "short") : (order.side === "buy" ? "long" : "short");
  return direction === side;
}

export async function preparePositionClose(api: CloseApi, key: string, symbol: string, side: string) {
  const encoded = encodeURIComponent(symbol);
  const setting = await api.get<ContractSetting>(`/contracts/settings/${encoded}`, key);
  const mode = setting.position_mode;
  if (mode !== "one_way" && mode !== "hedge") throw new Error("持仓模式未确认，未执行撤单或平仓");
  const orders = await api.get<{ items: OrderItem[] }>(`/contracts/orders/open?symbol=${encoded}`, key);
  for (const order of orders.items.filter((item) => belongsToPosition(item, symbol, side, mode))) {
    const result = await api.delete<{ order: OrderItem }>(`/contracts/orders/${encodeURIComponent(order.order_id)}`, key);
    if (!["canceled", "filled", "rejected", "expired"].includes(result.order?.status)) {
      throw new Error("挂单撤销未确认，本次未提交平仓，请刷新订单后重试");
    }
  }
  // Do not loop/chase orders created concurrently by another API client.
  const remaining = await api.get<{ items: OrderItem[] }>(`/contracts/orders/open?symbol=${encoded}`, key);
  if (remaining.items.some((item) => belongsToPosition(item, symbol, side, mode))) {
    throw new Error("该方向仍有活动挂单，本次未提交平仓，请停止同时挂单后重试");
  }
  return api.get<{ items: ContractPosition[] }>(`/contracts/positions?symbol=${encoded}`, key);
}
