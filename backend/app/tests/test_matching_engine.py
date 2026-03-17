from datetime import UTC, datetime
from decimal import Decimal

from app.core.constants import SIDE_BUY, SIDE_SELL
from app.services.matching_engine import BookOrder, MatchingEngine


def test_limit_order_matches_then_rests():
    engine = MatchingEngine()
    engine.load_resting_order(
        "BTCUSDT",
        BookOrder(
            order_id="maker-ask",
            user_id=2,
            side=SIDE_SELL,
            price=Decimal("62000"),
            remaining=Decimal("1"),
            created_at=datetime.now(tz=UTC),
        ),
    )
    result = engine.process_order(
        symbol="BTCUSDT",
        order_id="taker-buy",
        user_id=1,
        side=SIDE_BUY,
        quantity=Decimal("2"),
        created_at=datetime.now(tz=UTC),
        limit_price=Decimal("62010"),
        can_rest=True,
    )
    assert len(result.fills) == 1
    assert result.remaining_quantity == Decimal("1")
    snapshot = engine.snapshot("BTCUSDT", 10)
    assert snapshot["bids"][0] == ["62010", "1"]


def test_market_protected_never_rests():
    engine = MatchingEngine()
    engine.load_resting_order(
        "BTCUSDT",
        BookOrder(
            order_id="maker-ask",
            user_id=2,
            side=SIDE_SELL,
            price=Decimal("62050"),
            remaining=Decimal("1"),
            created_at=datetime.now(tz=UTC),
        ),
    )
    result = engine.process_order(
        symbol="BTCUSDT",
        order_id="protected-buy",
        user_id=1,
        side=SIDE_BUY,
        quantity=Decimal("1"),
        created_at=datetime.now(tz=UTC),
        can_rest=False,
        max_price=Decimal("62010"),
    )
    assert not result.fills
    assert result.remaining_quantity == Decimal("1")
    snapshot = engine.snapshot("BTCUSDT", 10)
    assert snapshot["bids"] == []


def test_amend_reduce_size_keeps_priority():
    engine = MatchingEngine()
    created_at = datetime.now(tz=UTC)
    engine.load_resting_order(
        "BTCUSDT",
        BookOrder(
            order_id="ask-a",
            user_id=2,
            side=SIDE_SELL,
            price=Decimal("62000"),
            remaining=Decimal("1"),
            created_at=created_at,
        ),
    )
    engine.load_resting_order(
        "BTCUSDT",
        BookOrder(
            order_id="ask-b",
            user_id=3,
            side=SIDE_SELL,
            price=Decimal("62000"),
            remaining=Decimal("1"),
            created_at=created_at,
        ),
    )

    amend = engine.amend_order("BTCUSDT", "ask-a", Decimal("62000"), Decimal("0.5"))
    assert amend is not None
    assert amend.kept_priority is True

    result = engine.process_order(
        symbol="BTCUSDT",
        order_id="taker-buy",
        user_id=1,
        side=SIDE_BUY,
        quantity=Decimal("1"),
        created_at=created_at,
        limit_price=Decimal("62000"),
        can_rest=False,
    )
    assert [fill.maker_order_id for fill in result.fills] == ["ask-a", "ask-b"]
    snapshot = engine.snapshot("BTCUSDT", 10)
    assert snapshot["asks"][0] == ["62000", "0.5"]


def test_amend_increase_size_moves_order_to_tail():
    engine = MatchingEngine()
    created_at = datetime.now(tz=UTC)
    engine.load_resting_order(
        "BTCUSDT",
        BookOrder(
            order_id="ask-a",
            user_id=2,
            side=SIDE_SELL,
            price=Decimal("62000"),
            remaining=Decimal("1"),
            created_at=created_at,
        ),
    )
    engine.load_resting_order(
        "BTCUSDT",
        BookOrder(
            order_id="ask-b",
            user_id=3,
            side=SIDE_SELL,
            price=Decimal("62000"),
            remaining=Decimal("1"),
            created_at=created_at,
        ),
    )

    amend = engine.amend_order("BTCUSDT", "ask-a", Decimal("62000"), Decimal("2"))
    assert amend is not None
    assert amend.kept_priority is False

    result = engine.process_order(
        symbol="BTCUSDT",
        order_id="taker-buy",
        user_id=1,
        side=SIDE_BUY,
        quantity=Decimal("1"),
        created_at=created_at,
        limit_price=Decimal("62000"),
        can_rest=False,
    )
    assert [fill.maker_order_id for fill in result.fills] == ["ask-b"]


def test_amend_price_change_moves_to_new_level_tail():
    engine = MatchingEngine()
    created_at = datetime.now(tz=UTC)
    engine.load_resting_order(
        "BTCUSDT",
        BookOrder(
            order_id="ask-a",
            user_id=2,
            side=SIDE_SELL,
            price=Decimal("62010"),
            remaining=Decimal("1"),
            created_at=created_at,
        ),
    )
    engine.load_resting_order(
        "BTCUSDT",
        BookOrder(
            order_id="ask-b",
            user_id=3,
            side=SIDE_SELL,
            price=Decimal("62000"),
            remaining=Decimal("1"),
            created_at=created_at,
        ),
    )

    amend = engine.amend_order("BTCUSDT", "ask-a", Decimal("62000"), Decimal("1"))
    assert amend is not None
    assert amend.kept_priority is False

    result = engine.process_order(
        symbol="BTCUSDT",
        order_id="taker-buy",
        user_id=1,
        side=SIDE_BUY,
        quantity=Decimal("1"),
        created_at=created_at,
        limit_price=Decimal("62000"),
        can_rest=False,
    )
    assert [fill.maker_order_id for fill in result.fills] == ["ask-b"]
