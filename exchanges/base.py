from abc import ABC, abstractmethod
from typing import Optional, Callable
from models.snapshots import PriceSnapshot, MarkIndexSnapshot


class BaseExchangeClient(ABC):
    """Abstract base class for exchange clients."""

    @abstractmethod
    async def connect(self):
        """Initialize connections (WebSocket, REST sessions, etc.)."""
        ...

    @abstractmethod
    async def close(self):
        """Clean up connections."""
        ...

    @abstractmethod
    def get_latest_price(self, pair: str) -> Optional[PriceSnapshot]:
        """Get the latest cached price snapshot for a pair."""
        ...

    @abstractmethod
    async def fetch_funding_rate(self, pair: str) -> Optional[float]:
        """Fetch the current funding rate for a pair via REST."""
        ...

    async def fetch_mark_index(self, pair: str) -> Optional[MarkIndexSnapshot]:
        """Fetch mark price and index price for a pair via REST."""
        return None

    def get_latest_mark_index(self, pair: str) -> Optional[MarkIndexSnapshot]:
        """Get cached mark-index snapshot."""
        return getattr(self, '_mark_index', {}).get(pair)

    def set_on_price_update(self, callback: Callable[[str], None]):
        """Set callback to be invoked when a price update is received.

        Args:
            callback: function(pair_name) called when price for a pair is updated
        """
        self._on_price_update = callback

    def _notify_price_update(self, pair: str):
        if hasattr(self, '_on_price_update') and self._on_price_update:
            self._on_price_update(pair)
