from __future__ import annotations

import asyncio
from collections import defaultdict

from app.services.account_service import AccountService
from app.services.market_data_service import MarketDataService
from app.services.matching_engine import MatchingEngine
from app.ws.manager import WebSocketManager


class AppRuntime:
    def __init__(self) -> None:
        self.engine = MatchingEngine()
        self.account_service = AccountService()
        self.market_data = MarketDataService()
        self.ws = WebSocketManager()
        self.market_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.bot_metrics: dict[str, dict] = defaultdict(dict)
        self.liquidity_metrics: dict[str, dict] = defaultdict(dict)
