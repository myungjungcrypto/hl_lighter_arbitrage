from dataclasses import dataclass, field
from typing import Optional
import time


@dataclass
class PriceSnapshot:
    exchange: str  # "tradexyz" or "lighter"
    pair: str  # "WTI" or "BRENT"
    best_bid: float = 0.0
    best_ask: float = 0.0
    mid_price: float = 0.0
    funding_rate: Optional[float] = None
    timestamp: float = field(default_factory=time.time)

    def is_valid(self) -> bool:
        return self.best_bid > 0 and self.best_ask > 0


@dataclass
class SpreadSnapshot:
    pair: str
    tradexyz: PriceSnapshot
    lighter: PriceSnapshot
    timestamp: float = field(default_factory=time.time)

    @property
    def lighter_cheap_spread(self) -> float:
        """Spread when Lighter is cheaper: buy Lighter, sell trade.xyz"""
        return self.tradexyz.best_bid - self.lighter.best_ask

    @property
    def tradexyz_cheap_spread(self) -> float:
        """Spread when trade.xyz is cheaper: buy trade.xyz, sell Lighter"""
        return self.lighter.best_bid - self.tradexyz.best_ask

    @property
    def best_spread(self) -> float:
        return max(self.lighter_cheap_spread, self.tradexyz_cheap_spread)

    @property
    def best_direction(self) -> str:
        if self.lighter_cheap_spread >= self.tradexyz_cheap_spread:
            return "LIGHTER_CHEAP"
        return "TRADEXYZ_CHEAP"

    @property
    def spread_pct(self) -> float:
        avg_mid = (self.tradexyz.mid_price + self.lighter.mid_price) / 2
        if avg_mid == 0:
            return 0.0
        return (self.best_spread / avg_mid) * 100

    @property
    def signal_text(self) -> str:
        if self.best_direction == "LIGHTER_CHEAP":
            return "Lighter 롱 + trade.xyz 숏"
        return "trade.xyz 롱 + Lighter 숏"

    @property
    def funding_diff(self) -> Optional[float]:
        if self.tradexyz.funding_rate is not None and self.lighter.funding_rate is not None:
            return self.tradexyz.funding_rate - self.lighter.funding_rate
        return None

    @property
    def funding_cost_per_hour(self) -> Optional[float]:
        """Net funding cost per hour (positive = paying, negative = receiving)"""
        fd = self.funding_diff
        if fd is None:
            return None
        avg_mid = (self.tradexyz.mid_price + self.lighter.mid_price) / 2
        if self.best_direction == "LIGHTER_CHEAP":
            # Long Lighter (pay lighter funding), Short trade.xyz (receive tradexyz funding)
            return (self.lighter.funding_rate - self.tradexyz.funding_rate) * avg_mid
        else:
            return (self.tradexyz.funding_rate - self.lighter.funding_rate) * avg_mid

    @property
    def breakeven_hours(self) -> Optional[float]:
        cost = self.funding_cost_per_hour
        if cost is None or cost <= 0:
            return None
        spread = self.best_spread
        if spread <= 0:
            return None
        return spread / cost

    def is_valid(self) -> bool:
        return self.tradexyz.is_valid() and self.lighter.is_valid()
