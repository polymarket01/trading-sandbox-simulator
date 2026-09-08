"""Compatibility import; implementation belongs to the optional strategy package."""
import sys
from app.maker_strategies.simple_bbo import algorithm as _implementation
sys.modules[__name__] = _implementation
