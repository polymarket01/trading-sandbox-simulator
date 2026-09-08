"""Bounded 1 Hz price-window signal; no network, database or numerical dependencies."""
from collections import deque
import math


class FlowActivity:
    def __init__(self):
        self.samples = deque(maxlen=62)
        self.source = None
        self.multiplier = 1.0
        self.result = {'multiplier': 1.0, 'status': 'warming_up', 'return_bps': 0.0, 'range_bps': 0.0}

    def update(self, now, price, source, config):
        enabled = config.volatility_enabled and (config.mode == 'virtual_volume' or config.real_ioc_volatility_enabled)
        if price is None or not math.isfinite(price) or price <= 0:
            self.samples.clear()
            self.multiplier = 1.0
            self.result = {'multiplier': 1.0, 'status': 'unavailable', 'return_bps': 0.0, 'range_bps': 0.0}
            return self.result
        if source != self.source or (self.samples and now - self.samples[-1][0] > 2.5):
            self.samples.clear()
            self.multiplier = 1.0
        self.source = source
        if self.samples and now - self.samples[-1][0] < 1:
            return self.result
        self.samples.append((now, price))
        cutoff = now - config.volatility_window_seconds
        while len(self.samples) > 1 and self.samples[1][0] <= cutoff:
            self.samples.popleft()
        ready = now - self.samples[0][0] >= config.volatility_window_seconds
        reference = self.samples[0][1]
        prices = [value for _, value in self.samples]
        signed_change = (price / reference - 1) * 10000
        change = abs(signed_change)
        direction = ('buy' if signed_change > 0 else 'sell') if ready and abs(signed_change) > 1e-9 else None
        # Latest non-flat movement prevents a reversal burst following the old trend.
        movement = next((b-a for a, b in reversed(list(zip(prices, prices[1:]))) if b != a), 0) if ready else 0
        burst_direction = ('buy' if movement > 0 else 'sell') if movement else None
        amplitude = (max(prices) - min(prices)) / reference * 10000
        ratio = max(change / config.volatility_return_bps, amplitude / config.volatility_range_bps) if ready else 0
        target = min(config.volatility_max_multiplier, max(1.0, ratio * ratio))
        # Follow the current window directly; no decay or hold after the trigger clears.
        self.multiplier = target if ready and enabled and burst_direction else 1.0
        self.result = {'multiplier': round(self.multiplier, 4), 'status': 'active' if self.multiplier > 1 else 'normal' if ready else 'warming_up',
                       'direction': direction, 'burst_direction': burst_direction, 'signed_return_bps': round(signed_change, 4), 'return_bps': round(change, 4), 'range_bps': round(amplitude, 4), 'source': source, 'samples': len(self.samples)}
        return self.result
