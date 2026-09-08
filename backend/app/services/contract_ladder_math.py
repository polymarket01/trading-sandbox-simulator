"""Compatibility import; implementation belongs to the optional strategy package."""
import sys
from app.maker_strategies.contract_ladder import math as _implementation
sys.modules[__name__] = _implementation
