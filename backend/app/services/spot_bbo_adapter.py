"""Four-level spot execution through the normal funded spot order service."""
from decimal import Decimal
from sqlalchemy import select
from app.models.market import Market
from app.models.user import User
from app.models.order import Order
from app.models.market_bot_account import MarketBotAccount
from app.schemas.api import OrderCreateRequest
from app.services.contract_ladder_adapter import ContractLadderAdapter
from app.services.order_service import OrderValidationError


class SpotBBOAdapter(ContractLadderAdapter):
    # Spot uses its own funded service and four-argument execution override;
    # it does not implement LADDER's optional pre-match quote-check protocol.
    place_checked = None

    def invalidate_identity(self, symbol, market_id):
        self.invalidate_metadata(symbol)

    async def _identity(self, session, symbol, uid, *, placing=False):
        market = await session.scalar(select(Market).where(Market.symbol == symbol))
        user = await session.get(User, uid)
        binding = await session.scalar(select(MarketBotAccount).where(MarketBotAccount.market_id == market.id,
            MarketBotAccount.user_id == uid, MarketBotAccount.role == 'maker', MarketBotAccount.strategy_role == 'CONTRACT_LADDER')) if market else None
        if not market or market.product_type != 'SPOT' or not user or user.role != 'mm_bot' or not binding:
            raise OrderValidationError('spot SIMPLE_BBO server maker binding required')
        if placing and (not binding.is_enabled or not user.is_active):
            raise OrderValidationError('spot maker identity is disabled')
        if placing:
            from app.models.balance import Balance
            async with self.runtime.clearinghouse.global_lock:
                for asset in (market.base_asset, market.quote_asset):
                    if self.runtime.clearinghouse.spot_snapshot(uid, asset) is None:
                        balance = await session.scalar(select(Balance).where(Balance.user_id == uid, Balance.asset == asset))
                        if balance is not None:
                            self.runtime.clearinghouse.set_spot_balance(uid, asset, available=balance.available, frozen=balance.frozen)
        return market, user

    async def _place(self, key, side, price, quantity):
        symbol, uid, cid = key
        async with self.session_factory() as session:
            _, user = await self._identity(session, symbol, uid, placing=True)
            payload = OrderCreateRequest(symbol=symbol, side=side, type='limit', tif='gtc', price=Decimal(price), quantity=Decimal(quantity), client_order_id=cid)
            result = await self.service.place_order_fast(session, user, payload)
            return result if result is not None else await self.service.place_order(session, user, payload)

    async def _cancel(self, symbol, maker_uid, order_id):
        async with self.session_factory() as session:
            market, user = await self._identity(session, symbol, maker_uid)
            snap = self.service._fast_orders.get(order_id)
            row = await session.scalar(select(Order).where(Order.order_id == order_id)) if snap is None else None
            if snap is not None:
                if int(snap['user_id']) != maker_uid or int(snap['market_id']) != market.id:
                    raise OrderValidationError('cannot cancel others order')
            elif row is None or row.user_id != maker_uid or row.market_id != market.id:
                raise OrderValidationError('owned spot order not found')
            result = await self.service.cancel_order_fast(session, user, order_id)
            return result if result is not None else await self.service.cancel_order(session, user, order_id)

    async def own_orders(self, symbol, maker_uid):
        async with self.session_factory() as session:
            market, _ = await self._identity(session, symbol, maker_uid)
            async with self.runtime.market_locks[symbol]:
                live = list(self.runtime.engine.ensure_market(symbol).orders.values())
                # The matching engine owns remaining quantities, including recent fills.
                return [{'order_id': o.order_id, 'user_id': maker_uid, 'symbol': symbol, 'side': o.side,
                         'price': str(o.price), 'quantity': str(o.remaining), 'remaining_quantity': str(o.remaining),
                         'status': 'new'} for o in live if o.user_id == maker_uid]

    async def query(self, symbol, maker_uid, *, order_id=None, client_order_id=None):
        key = (symbol, maker_uid, str(client_order_id))
        if key in self._inflight or (symbol, maker_uid, order_id) in self._cancel_inflight:
            return {'status': 'ACCEPTED', 'pending': True}
        async with self.session_factory() as session:
            market, _ = await self._identity(session, symbol, maker_uid)
            for snap in self.service._fast_orders.values():
                if int(snap['user_id']) == maker_uid and int(snap['market_id']) == market.id and ((order_id and snap['order_id'] == order_id) or (client_order_id and snap.get('client_order_id') == client_order_id)):
                    return {'order': self.service._fast_serialize_order(market, snap)}
            statement = select(Order).where(Order.user_id == maker_uid, Order.market_id == market.id)
            statement = statement.where(Order.order_id == order_id) if order_id else statement.where(Order.client_order_id == client_order_id)
            row = await session.scalar(statement)
            if row is not None:
                return {'order': await self.service.serialize_order(session, row, symbol)}
        return self._requests.get(key, {'status': 'NOT_FOUND_FINAL', 'pending': False})
