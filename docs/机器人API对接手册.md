# 机器人 API 对接手册

## 1. 接入原则

本沙盒中的机器人账户不是特殊角色，本质上与普通用户一致：

- 使用同一套撮合引擎
- 使用同一套下单 / 撤单 / 查询接口
- 差别主要在于预置 API Key / Secret 与默认费率

建议把机器人当作真实 API 客户端来接。

## 2. 基础信息

### 2.1 地址

- REST 基础地址：`http://localhost:5174/api/v1`
- 公共 WS：`ws://localhost:5174/ws/public`
- 私有 WS：`ws://localhost:5174/ws/private`
- OpenAPI：`http://localhost:5174/docs`

### 2.2 市场

- `BTCUSDT`
- `XXXUSDT`
- `YYYUSDT`
- `ZZZUSDT`

### 2.3 预置机器人账户

| 用户名 | API Key | API Secret |
| --- | --- | --- |
| `spot_mm_1` | `mm1-demo-key` | `mm1-demo-secret` |
| `spot_mm_2` | `mm2-demo-key` | `mm2-demo-secret` |
| `spot_mm_3` | `mm3-demo-key` | `mm3-demo-secret` |
| `spot_mm_4` | `mm4-demo-key` | `mm4-demo-secret` |
| `spot_mm_5` | `mm5-demo-key` | `mm5-demo-secret` |
| `spot_mm_6` | `mm6-demo-key` | `mm6-demo-secret` |
| `spot_mm_7` | `mm7-demo-key` | `mm7-demo-secret` |
| `spot_mm_8` | `mm8-demo-key` | `mm8-demo-secret` |
| `spot_mm_9` | `mm9-demo-key` | `mm9-demo-secret` |
| `spot_mm_10` | `mm10-demo-key` | `mm10-demo-secret` |
| `flow_user_1` | `flow1-demo-key` | `flow1-demo-secret` |
| `flow_user_2` | `flow2-demo-key` | `flow2-demo-secret` |

说明：

- V2 做市默认使用 `spot_mm_1 / spot_mm_2 / spot_mm_3`
- 背景流默认使用 `flow_user_1 / flow_user_2`
- 如果当前后端还在旧种子上、尚未创建 `flow_user_1 / 2`，V2 机器人会自动回退使用 `spot_mm_4 / spot_mm_5`

## 3. 鉴权

REST 当前采用简单 API Key 头鉴权。

请求头：

```http
X-API-KEY: mm1-demo-key
```

私有 WS 采用：

- `api_key`
- `timestamp`
- HMAC-SHA256 签名

签名原文：

```text
{api_key}:{timestamp}
```

签名算法：

```text
HMAC_SHA256(secret, "{api_key}:{timestamp}")
```

## 4. 核心 REST API

### 4.1 查询市场列表

`GET /markets`

示例：

```bash
curl http://localhost:5174/api/v1/markets
```

用途：

- 获取支持的交易对
- 获取精度、步长、最小下单量、最小成交额

### 4.2 查询 ticker

`GET /markets/{symbol}/ticker`

示例：

```bash
curl http://localhost:5174/api/v1/markets/BTCUSDT/ticker
```

返回重点字段：

- `last_price`
- `best_bid`
- `best_ask`
- `spread`
- `spread_pct`
- `mid_price`
- `depth_amount_0_5pct`
- `depth_amount_2pct`
- `is_active`

说明：

- `spread_pct` 已按百分比返回
- `depth_amount_*` 是深度金额，单位为 `USDT`

### 4.3 查询订单簿

`GET /markets/{symbol}/orderbook?depth=20`

参数：

- `depth`：`1 ~ 50`

示例：

```bash
curl "http://localhost:5174/api/v1/markets/BTCUSDT/orderbook?depth=50"
```

返回：

- `bids`
- `asks`
- `last_update_id`
- `ts`

### 4.4 查询最近成交

`GET /markets/{symbol}/trades?limit=100`

示例：

```bash
curl "http://localhost:5174/api/v1/markets/BTCUSDT/trades?limit=100"
```

说明：

- K 线就是基于这些成交聚合而来
- 时间戳单位为毫秒

### 4.5 查询 K 线

`GET /markets/{symbol}/klines?interval=1m&limit=300`

支持周期：

- `1s`
- `5s`
- `15s`
- `1m`
- `5m`
- `15m`

示例：

```bash
curl "http://localhost:5174/api/v1/markets/BTCUSDT/klines?interval=1m&limit=300"
```

## 5. 账户 REST API

以下接口都需要 `X-API-KEY`。

### 5.1 查询余额

`GET /account/balances`

```bash
curl -H "X-API-KEY: mm1-demo-key" \
  http://localhost:5174/api/v1/account/balances
```

### 5.2 查询当前挂单

`GET /account/orders/open?symbol=BTCUSDT`

```bash
curl -H "X-API-KEY: mm1-demo-key" \
  "http://localhost:5174/api/v1/account/orders/open?symbol=BTCUSDT"
```

### 5.3 查询订单历史

`GET /account/orders/history?symbol=BTCUSDT&limit=100`

### 5.4 查询账户成交

`GET /account/trades?symbol=BTCUSDT&limit=100`

### 5.5 查询账本

`GET /account/ledger?limit=100`

### 5.6 查询单笔订单

`GET /orders/{order_id}`

## 6. 下单接口

### 6.1 创建订单

`POST /orders`

请求头：

```http
Content-Type: application/json
X-API-KEY: mm1-demo-key
```

#### 限价单

```json
{
  "symbol": "BTCUSDT",
  "side": "buy",
  "type": "limit",
  "tif": "gtc",
  "price": "61995",
  "quantity": "0.25",
  "client_order_id": "bot-bid-0001"
}
```

#### 真实市价单

```json
{
  "symbol": "BTCUSDT",
  "side": "buy",
  "type": "market",
  "tif": "ioc",
  "quantity": "0.25",
  "client_order_id": "bot-mkt-0001"
}
```

#### 保护价市价单

```json
{
  "symbol": "BTCUSDT",
  "side": "buy",
  "type": "market_protected",
  "tif": "ioc",
  "quantity": "0.25",
  "protection_bps": 30,
  "client_order_id": "bot-protected-0001"
}
```

业务规则：

- `limit` 必须带 `price`
- `market` 和 `market_protected` 必须 `tif=ioc`
- `market_protected` 必须带 `protection_bps`
- 保护价市价单剩余未成交部分会立即取消，不会挂盘

### 6.2 撤单

`DELETE /orders/{order_id}`

```bash
curl -X DELETE \
  -H "X-API-KEY: mm1-demo-key" \
  http://localhost:5174/api/v1/orders/ord_xxx
```

### 6.3 改单

`PATCH /orders/{order_id}`

```json
{
  "price": "61996",
  "quantity": "0.20"
}
```

```bash
curl -X PATCH \
  -H "Content-Type: application/json" \
  -H "X-API-KEY: mm1-demo-key" \
  -d '{"price":"61996","quantity":"0.20"}' \
  http://localhost:5174/api/v1/orders/ord_xxx
```

业务规则：

- 仅支持 `gtc limit` 挂单改单
- 同价且减量：保留原排队优先级
- 改价或加量：原子执行 `remove + insert_to_tail`，失去原时间优先级
- `quantity` 表示改单后的订单总量，必须大于已成交量

### 6.4 全撤

`POST /orders/cancel-all`

```json
{
  "symbol": "BTCUSDT"
}
```

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-API-KEY: mm1-demo-key" \
  -d '{"symbol":"BTCUSDT"}' \
  http://localhost:5174/api/v1/orders/cancel-all
```

## 7. 公共 WebSocket

地址：

```text
ws://localhost:5174/ws/public
```

### 7.1 订阅订单簿

发送：

```json
{
  "op": "subscribe",
  "channel": "orderbook",
  "symbol": "BTCUSDT",
  "depth": 50
}
```

说明：

- 首包会返回 `snapshot`
- 后续订单簿更新也返回完整 `snapshot`
- 前端展示支持 `20 / 50` 档
- 深度统计不是按订阅档数算，而是按全盘口算

### 7.2 订阅成交

```json
{
  "op": "subscribe",
  "channel": "trades",
  "symbol": "BTCUSDT"
}
```

### 7.3 订阅 stats

```json
{
  "op": "subscribe",
  "channel": "stats",
  "symbol": "BTCUSDT"
}
```

### 7.4 订阅 K 线

```json
{
  "op": "subscribe",
  "channel": "kline",
  "symbol": "BTCUSDT",
  "interval": "1m"
}
```

## 8. 私有 WebSocket

地址：

```text
ws://localhost:5174/ws/private
```

### 8.1 鉴权

先发送：

```json
{
  "op": "auth",
  "api_key": "mm1-demo-key",
  "timestamp": 1710000000000,
  "signature": "hex_hmac_sha256"
}
```

鉴权成功返回：

```json
{
  "type": "auth_ok"
}
```

### 8.2 订阅余额

```json
{
  "op": "subscribe",
  "channel": "balances"
}
```

### 8.3 订阅订单

```json
{
  "op": "subscribe",
  "channel": "orders"
}
```

说明：

- 订阅后会先返回一份 snapshot
- 后续下单、成交、撤单时会推增量更新

## 9. Python 对接示例

### 9.1 REST 下限价单

```python
import requests

BASE = "http://localhost:5174/api/v1"
API_KEY = "mm1-demo-key"

payload = {
    "symbol": "BTCUSDT",
    "side": "buy",
    "type": "limit",
    "tif": "gtc",
    "price": "61990",
    "quantity": "0.2",
    "client_order_id": "mm1-bid-001",
}

resp = requests.post(
    f"{BASE}/orders",
    json=payload,
    headers={"X-API-KEY": API_KEY},
    timeout=5,
)
print(resp.status_code, resp.json())
```

### 9.2 私有 WS 签名示例

```python
import hashlib
import hmac
import json
import time

from websocket import create_connection

api_key = "mm1-demo-key"
secret = "mm1-demo-secret"
timestamp = int(time.time() * 1000)
payload = f"{api_key}:{timestamp}".encode()
signature = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

ws = create_connection("ws://localhost:5174/ws/private")
ws.send(json.dumps({
    "op": "auth",
    "api_key": api_key,
    "timestamp": timestamp,
    "signature": signature,
}))
print(ws.recv())
ws.send(json.dumps({"op": "subscribe", "channel": "orders"}))
print(ws.recv())
```

## 10. 调试建议

### 10.1 先从这几步开始

1. 先用 `GET /markets` 校验交易对和精度
2. 再拉 `ticker / orderbook / trades`
3. 再用单笔限价单验证下单和撤单
4. 再接私有 WS 观察订单和余额更新
5. 最后再上批量挂单与撤补单逻辑

### 10.2 重点关注字段

- `client_order_id`
- `order_id`
- `status`
- `filled_quantity`
- `remaining_quantity`
- `avg_price`
- `fee`
- `fee_asset`
- `liquidity_role`

### 10.3 常见误区

- 不要把 `market_protected` 当成可挂盘订单
- 不要把 `spread_pct` 当成 bps
- 不要把界面显示的 50 档理解成系统只能挂 50 档
- 不要依赖浏览器本地时区，界面和接口时间戳统一按毫秒时间戳处理

## 11. 后续最值得补的 API

建议 V2 优先增加：

- 批量下单
- 批量撤单
- 通过 `client_order_id` 查订单
- 更细粒度的私有 WS 事件
- 账户级风控与限流返回码
