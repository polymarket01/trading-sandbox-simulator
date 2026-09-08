from __future__ import annotations

from datetime import datetime, timezone
from itertools import count

_order_counter = count(1000)
_trade_counter = count(10000)
_ledger_counter = count(50000)
_liquidation_counter = count(60000)
_funding_settlement_counter = count(70000)
_funding_job_counter = count(80000)
_contract_ledger_counter = count(90000)
_contract_insurance_counter = count(100000)
_contract_adl_counter = count(110000)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")


def next_order_id() -> str:
    return f"o_{_stamp()}_{next(_order_counter)}"


def next_trade_id() -> str:
    return f"t_{_stamp()}_{next(_trade_counter)}"


def next_ledger_id() -> str:
    return f"l_{_stamp()}_{next(_ledger_counter)}"


def next_contract_ledger_id() -> str:
    return f"cl_{_stamp()}_{next(_contract_ledger_counter)}"


def next_contract_insurance_event_id() -> str:
    return f"if_{_stamp()}_{next(_contract_insurance_counter)}"


def next_contract_adl_event_id() -> str:
    return f"adl_{_stamp()}_{next(_contract_adl_counter)}"


def next_liquidation_id() -> str:
    return f"liq_{_stamp()}_{next(_liquidation_counter)}"


def next_funding_settlement_id() -> str:
    return f"fs_{_stamp()}_{next(_funding_settlement_counter)}"


def next_funding_job_id() -> str:
    return f"fj_{_stamp()}_{next(_funding_job_counter)}"
