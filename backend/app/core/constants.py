from decimal import Decimal

ORDER_STATUS_NEW = "new"
ORDER_STATUS_PARTIALLY_FILLED = "partially_filled"
ORDER_STATUS_FILLED = "filled"
ORDER_STATUS_CANCELED = "canceled"
ORDER_STATUS_REJECTED = "rejected"

ORDER_TYPE_LIMIT = "limit"
ORDER_TYPE_MARKET = "market"
ORDER_TYPE_MARKET_PROTECTED = "market_protected"

SIDE_BUY = "buy"
SIDE_SELL = "sell"

TIF_GTC = "gtc"
TIF_IOC = "ioc"

ROLE_MANUAL = "manual_user"
ROLE_BOT = "mm_bot"
ROLE_ADMIN = "admin"

ZERO = Decimal("0")
