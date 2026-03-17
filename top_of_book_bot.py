#!/usr/bin/env python3
"""
极简买一卖一机器人 - BTC 盘口顶档挂单

在沙盒启动后，可选运行此脚本，为 BTC/USDT 提供买一、卖一挂单。
用户使用市价单与挂单成交后，会产生真实成交，K 线将实时更新。

零外部依赖，仅需 Python 3.10+ 标准库。

用法：
  1. 先启动沙盒：python3 run_sandbox.py
  2. 另开终端运行：python3 top_of_book_bot.py

可选参数：
  --base-url URL      API 地址，默认 http://localhost:5174/api/v1
  --api-key KEY       使用 spot_mm_1 默认 mm1-demo-key
  --order-qty QTY     每档挂单量，默认 0.01 BTC
  --interval SEC      轮询间隔秒数，默认 2
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal

SYMBOL = "BTCUSDT"
DEFAULT_BASE_URL = "http://localhost:5174/api/v1"
DEFAULT_API_KEY = "mm1-demo-key"
DEFAULT_ORDER_QTY = "0.01"
DEFAULT_INTERVAL = 2.0


def quantize_price(price: Decimal, tick: Decimal) -> Decimal:
    """价格对齐到 tick"""
    return (price / tick).quantize(Decimal("1")) * tick


def quantize_qty(qty: Decimal, step: Decimal) -> Decimal:
    """数量对齐到 step"""
    return (qty / step).quantize(Decimal("1")) * step


def _req(
    url: str,
    method: str = "GET",
    data: dict | None = None,
    api_key: str | None = None,
) -> dict | None:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-KEY"] = api_key
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as e:
        logging.warning("请求失败 %s: %s", url, e)
        return None


def fetch_ticker(base_url: str) -> dict | None:
    return _req(f"{base_url}/markets/{SYMBOL}/ticker")


def fetch_markets(base_url: str) -> dict | None:
    data = _req(f"{base_url}/markets")
    if not data:
        return None
    for m in data.get("items", []):
        if m.get("symbol") == SYMBOL:
            return m
    return None


def fetch_open_orders(base_url: str, api_key: str) -> list:
    data = _req(
        f"{base_url}/account/orders/open?symbol={SYMBOL}",
        api_key=api_key,
    )
    return data.get("items", []) if data else []


def cancel_all(base_url: str, api_key: str) -> bool:
    result = _req(
        f"{base_url}/orders/cancel-all",
        method="POST",
        data={"symbol": SYMBOL},
        api_key=api_key,
    )
    return result is not None


def place_order(
    base_url: str,
    api_key: str,
    side: str,
    price: str,
    quantity: str,
) -> bool:
    result = _req(
        f"{base_url}/orders",
        method="POST",
        data={
            "symbol": SYMBOL,
            "side": side,
            "type": "limit",
            "tif": "gtc",
            "price": price,
            "quantity": quantity,
            "client_order_id": f"top-bot-{side}-{price}",
        },
        api_key=api_key,
    )
    return result is not None


def run_bot(
    base_url: str,
    api_key: str,
    order_qty: str,
    interval: float,
) -> None:
    base_url = base_url.rstrip("/")
    qty_decimal = Decimal(order_qty)
    fallback_mid = Decimal("60000")

    market = fetch_markets(base_url)
    if not market:
        logging.error("无法获取 %s 市场信息，请确认沙盒已启动", SYMBOL)
        sys.exit(1)

    price_tick = Decimal(str(market.get("price_tick", "0.1")))
    qty_step = Decimal(str(market.get("qty_step", "0.0001")))
    min_qty = Decimal(str(market.get("min_qty", "0.0001")))
    qty = quantize_qty(max(qty_decimal, min_qty), qty_step)

    logging.info("买一卖一机器人已启动，挂单量 %s BTC", qty)

    while True:
        ticker = fetch_ticker(base_url)
        if not ticker:
            time.sleep(interval)
            continue

        best_bid = ticker.get("best_bid")
        best_ask = ticker.get("best_ask")
        mid_price = ticker.get("mid_price") or ticker.get("last_price")

        if best_bid is None or best_ask is None:
            mid = Decimal(str(mid_price)) if mid_price else fallback_mid
            spread = Decimal("10")
            bid_price = quantize_price(mid - spread, price_tick)
            ask_price = quantize_price(mid + spread, price_tick)
        else:
            bid_price = Decimal(str(best_bid))
            ask_price = Decimal(str(best_ask))

        if bid_price >= ask_price:
            mid = Decimal(str(mid_price)) if mid_price else fallback_mid
            bid_price = quantize_price(mid - price_tick, price_tick)
            ask_price = quantize_price(mid + price_tick, price_tick)

        bid_str = str(bid_price)
        ask_str = str(ask_price)
        qty_str = str(qty)

        open_orders = fetch_open_orders(base_url, api_key)
        my_bids = [o for o in open_orders if o.get("side") == "buy"]
        my_asks = [o for o in open_orders if o.get("side") == "sell"]

        need_bid = not any(o.get("price") == bid_str for o in my_bids)
        need_ask = not any(o.get("price") == ask_str for o in my_asks)

        if need_bid or need_ask:
            cancel_all(base_url, api_key)
            time.sleep(0.3)

        if need_bid:
            if place_order(base_url, api_key, "buy", bid_str, qty_str):
                logging.info("挂买一 %s @ %s", qty_str, bid_str)
        if need_ask:
            if place_order(base_url, api_key, "sell", ask_str, qty_str):
                logging.info("挂卖一 %s @ %s", qty_str, ask_str)

        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="极简买一卖一机器人，为 BTC/USDT 提供盘口顶档挂单"
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"API 地址，默认 {DEFAULT_BASE_URL}",
    )
    parser.add_argument(
        "--api-key",
        default=DEFAULT_API_KEY,
        help=f"API Key，默认 {DEFAULT_API_KEY}",
    )
    parser.add_argument(
        "--order-qty",
        default=DEFAULT_ORDER_QTY,
        help=f"每档挂单量 BTC，默认 {DEFAULT_ORDER_QTY}",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help=f"轮询间隔秒数，默认 {DEFAULT_INTERVAL}",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    try:
        run_bot(
            base_url=args.base_url,
            api_key=args.api_key,
            order_qty=args.order_qty,
            interval=args.interval,
        )
    except KeyboardInterrupt:
        logging.info("已停止")


if __name__ == "__main__":
    main()
