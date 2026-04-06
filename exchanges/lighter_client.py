"""Lighter.xyz client using lighter-sdk for WebSocket + REST.

WebSocket for real-time orderbook, REST for funding rates and market discovery.
"""
from __future__ import annotations

import asyncio
import logging
import time
import threading
from typing import Optional, Callable

from config import LIGHTER_API_URL, PAIRS
from exchanges.base import BaseExchangeClient
from models.snapshots import PriceSnapshot

logger = logging.getLogger(__name__)

# Lighter SDK imports (lighter-sdk package)
try:
    import lighter
    from lighter.api.order_api import OrderApi
    from lighter.api.candlestick_api import CandlestickApi
    from lighter.api_client import ApiClient
    from lighter.configuration import Configuration
    HAS_LIGHTER_SDK = True
except ImportError:
    HAS_LIGHTER_SDK = False
    logger.warning("lighter-sdk not installed. Install with: pip install lighter-sdk")

# Fallback: direct REST via aiohttp
try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False


class LighterClient(BaseExchangeClient):
    def __init__(self):
        self._prices: dict[str, PriceSnapshot] = {}
        self._market_ids: dict[str, int] = {}  # pair_name -> market_id
        self._symbol_to_pair: dict[str, str] = {}  # lighter symbol -> pair_name
        self._ws_client = None
        self._ws_thread: threading.Thread | None = None
        self._running = False
        self._on_price_update: Callable[[str], None] | None = None
        self._api_client = None
        self._aiohttp_session: aiohttp.ClientSession | None = None

    async def connect(self):
        """Discover markets and start WebSocket connection."""
        await self._discover_markets()
        self._running = True
        self._start_ws()
        logger.info("Lighter client connected (WebSocket mode)")

    async def close(self):
        self._running = False
        if self._ws_client:
            try:
                self._ws_client = None
            except Exception:
                pass
        if self._api_client:
            try:
                await self._api_client.close()
            except Exception:
                pass
        if self._aiohttp_session and not self._aiohttp_session.closed:
            await self._aiohttp_session.close()

    def get_latest_price(self, pair: str) -> Optional[PriceSnapshot]:
        return self._prices.get(pair)

    # ── Market Discovery ───────────────────────────────────────

    async def _discover_markets(self):
        """Find market_ids for WTI and BRENT on Lighter."""
        if HAS_LIGHTER_SDK:
            await self._discover_markets_sdk()
        elif HAS_AIOHTTP:
            await self._discover_markets_rest()
        else:
            raise RuntimeError("Neither lighter-sdk nor aiohttp available")

    async def _discover_markets_sdk(self):
        config = Configuration(host=LIGHTER_API_URL)
        self._api_client = ApiClient(config)
        order_api = OrderApi(self._api_client)

        try:
            details = await order_api.order_book_details()
            for ob in details.order_book_details:
                symbol = ob.symbol.upper()
                for pair_name, pair_cfg in PAIRS.items():
                    lighter_symbol = pair_cfg["lighter"].upper()
                    if symbol == lighter_symbol:
                        self._market_ids[pair_name] = ob.market_id
                        self._symbol_to_pair[str(ob.market_id)] = pair_name
                        logger.info(
                            "Lighter market discovered: %s -> market_id=%d (symbol=%s)",
                            pair_name, ob.market_id, symbol
                        )
        except Exception as e:
            logger.error("Lighter SDK market discovery failed: %s", e)
            if HAS_AIOHTTP:
                logger.info("Falling back to REST for market discovery")
                await self._discover_markets_rest()
            else:
                raise

        if not self._market_ids:
            logger.warning("No Lighter markets discovered. Available pairs may differ.")

    async def _discover_markets_rest(self):
        """Fallback market discovery via direct REST."""
        session = await self._get_aiohttp_session()
        url = f"{LIGHTER_API_URL}/api/v1/orderBookDetails"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.error("Lighter REST market discovery failed: HTTP %s", resp.status)
                    return
                data = await resp.json()
                for ob in data.get("order_book_details", []):
                    symbol = ob.get("symbol", "").upper()
                    market_id = ob.get("market_id")
                    for pair_name, pair_cfg in PAIRS.items():
                        lighter_symbol = pair_cfg["lighter"].upper()
                        if symbol == lighter_symbol:
                            self._market_ids[pair_name] = market_id
                            self._symbol_to_pair[str(market_id)] = pair_name
                            logger.info(
                                "Lighter market (REST): %s -> market_id=%d",
                                pair_name, market_id,
                            )
        except Exception as e:
            logger.error("Lighter REST market discovery error: %s", e)

    async def _get_aiohttp_session(self) -> aiohttp.ClientSession:
        if self._aiohttp_session is None or self._aiohttp_session.closed:
            timeout = aiohttp.ClientTimeout(total=10)
            self._aiohttp_session = aiohttp.ClientSession(timeout=timeout)
        return self._aiohttp_session

    # ── WebSocket (lighter SDK) ────────────────────────────────

    def _start_ws(self):
        """Start Lighter WebSocket in a separate thread (SDK uses sync websockets)."""
        if not HAS_LIGHTER_SDK or not self._market_ids:
            logger.warning("Lighter WebSocket not started: SDK unavailable or no markets")
            return

        market_ids = list(self._market_ids.values())

        def run_ws():
            try:
                self._ws_client = lighter.WsClient(
                    order_book_ids=market_ids,
                    on_order_book_update=self._on_orderbook_update,
                )
                self._ws_client.run()
            except Exception as e:
                if self._running:
                    logger.error("Lighter WebSocket error: %s", e)

        self._ws_thread = threading.Thread(target=run_ws, daemon=True)
        self._ws_thread.start()
        logger.info("Lighter WebSocket started for market_ids: %s", market_ids)

    def _on_orderbook_update(self, market_id, order_book):
        """Callback from Lighter WebSocket on orderbook update."""
        pair_name = self._symbol_to_pair.get(str(market_id))
        if not pair_name:
            return

        bids = order_book.get("bids", [])
        asks = order_book.get("asks", [])

        if not bids or not asks:
            return

        best_bid = float(bids[0].get("price", 0))
        best_ask = float(asks[0].get("price", 0))
        if best_bid == 0 or best_ask == 0:
            return

        existing = self._prices.get(pair_name, PriceSnapshot("lighter", pair_name))
        self._prices[pair_name] = PriceSnapshot(
            exchange="lighter",
            pair=pair_name,
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=(best_bid + best_ask) / 2,
            funding_rate=existing.funding_rate,
            timestamp=time.time(),
        )
        self._notify_price_update(pair_name)

    # ── REST (funding rate + fallback) ─────────────────────────

    async def fetch_funding_rate(self, pair: str) -> Optional[float]:
        market_id = self._market_ids.get(pair)
        if market_id is None:
            return None

        if HAS_LIGHTER_SDK and self._api_client:
            return await self._fetch_funding_sdk(pair, market_id)
        elif HAS_AIOHTTP:
            return await self._fetch_funding_rest(pair, market_id)
        return None

    async def _fetch_funding_sdk(self, pair: str, market_id: int) -> Optional[float]:
        try:
            candle_api = CandlestickApi(self._api_client)
            now = int(time.time() * 1000)
            one_hour_ago = now - 3600 * 1000
            fundings = await candle_api.fundings(
                market_id=market_id,
                resolution="1h",
                start_timestamp=one_hour_ago,
                end_timestamp=now,
                count_back=1,
            )
            if fundings and hasattr(fundings, 'fundings') and fundings.fundings:
                rate = float(fundings.fundings[-1].get("rate", 0))
                if pair in self._prices:
                    self._prices[pair].funding_rate = rate
                return rate
        except Exception as e:
            logger.error("Lighter SDK funding fetch error for %s: %s", pair, e)
        return None

    async def _fetch_funding_rest(self, pair: str, market_id: int) -> Optional[float]:
        session = await self._get_aiohttp_session()
        now = int(time.time() * 1000)
        one_hour_ago = now - 3600 * 1000
        url = (
            f"{LIGHTER_API_URL}/api/v1/fundings"
            f"?market_id={market_id}&resolution=1h"
            f"&start_timestamp={one_hour_ago}&end_timestamp={now}&count_back=1"
        )
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                fundings = data.get("fundings", [])
                if fundings:
                    rate = float(fundings[-1].get("rate", 0))
                    if pair in self._prices:
                        self._prices[pair].funding_rate = rate
                    return rate
        except Exception as e:
            logger.error("Lighter REST funding error for %s: %s", pair, e)
        return None

    async def fetch_price_rest(self, pair: str) -> Optional[PriceSnapshot]:
        """REST fallback for fetching prices."""
        market_id = self._market_ids.get(pair)
        if market_id is None:
            return None

        session = await self._get_aiohttp_session()
        url = f"{LIGHTER_API_URL}/api/v1/orderBookOrders?market_id={market_id}&limit=5"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                bids = data.get("bids", [])
                asks = data.get("asks", [])
                if not bids or not asks:
                    return None

                best_bid = float(bids[0].get("price", 0))
                best_ask = float(asks[0].get("price", 0))
                if best_bid == 0 or best_ask == 0:
                    return None

                snapshot = PriceSnapshot(
                    exchange="lighter",
                    pair=pair,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    mid_price=(best_bid + best_ask) / 2,
                    timestamp=time.time(),
                )
                self._prices[pair] = snapshot
                return snapshot
        except Exception as e:
            logger.error("Lighter REST price error for %s: %s", pair, e)
        return None
