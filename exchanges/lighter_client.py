"""Lighter.xyz client using lighter-sdk for WebSocket + REST.

WebSocket for real-time orderbook, REST for funding rates and market discovery.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, Callable

from config import LIGHTER_API_URL, PAIRS
from exchanges.base import BaseExchangeClient
from models.snapshots import PriceSnapshot, MarkIndexSnapshot

logger = logging.getLogger(__name__)

# Lighter SDK imports (lighter-sdk package)
try:
    import lighter
    from lighter.api.order_api import OrderApi
    from lighter.api.funding_api import FundingApi
    from lighter.api_client import ApiClient
    from lighter.configuration import Configuration
    HAS_LIGHTER_SDK = True
except ImportError:
    HAS_LIGHTER_SDK = False
    logger.warning("lighter-sdk not installed. Install with: pip install lighter-sdk")


class LighterClient(BaseExchangeClient):
    def __init__(self):
        self._prices: dict[str, PriceSnapshot] = {}
        self._market_ids: dict[str, int] = {}  # pair_name -> market_id
        self._marketid_to_pair: dict[str, str] = {}  # str(market_id) -> pair_name
        self._ws_client: lighter.WsClient | None = None
        self._ws_task: asyncio.Task | None = None
        self._mark_index: dict[str, MarkIndexSnapshot] = {}
        self._running = False
        self._on_price_update: Callable[[str], None] | None = None
        self._api_client: ApiClient | None = None

    async def connect(self):
        """Discover markets and start WebSocket connection."""
        if not HAS_LIGHTER_SDK:
            logger.error("lighter-sdk not installed. Lighter client disabled.")
            return

        # Set default configuration host
        config = Configuration(host=LIGHTER_API_URL)
        Configuration.set_default(config)
        self._api_client = ApiClient(config)

        await self._discover_markets()
        self._running = True
        self._start_ws()
        logger.info("Lighter client connected")

    async def close(self):
        self._running = False
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._api_client:
            try:
                await self._api_client.close()
            except (RuntimeError, Exception):
                pass  # event loop may already be closed

    def get_latest_price(self, pair: str) -> Optional[PriceSnapshot]:
        return self._prices.get(pair)

    # ── Market Discovery ───────────────────────────────────────

    async def _discover_markets(self):
        """Find market_ids for WTI and BRENT on Lighter."""
        order_api = OrderApi(self._api_client)

        try:
            details = await order_api.order_book_details()
            all_symbols = [ob.symbol for ob in details.order_book_details]
            logger.info("Lighter available perps markets: %s", all_symbols)

            for ob in details.order_book_details:
                symbol = ob.symbol.upper()
                for pair_name, pair_cfg in PAIRS.items():
                    lighter_symbol = pair_cfg["lighter"].upper()
                    if symbol == lighter_symbol or lighter_symbol in symbol:
                        self._market_ids[pair_name] = ob.market_id
                        self._marketid_to_pair[str(ob.market_id)] = pair_name
                        logger.info(
                            "Lighter market discovered: %s -> market_id=%d (symbol=%s)",
                            pair_name, ob.market_id, symbol,
                        )
        except Exception as e:
            logger.error("Lighter SDK market discovery failed: %s", e, exc_info=True)

        if not self._market_ids:
            logger.warning(
                "No Lighter markets matched. Looking for symbols containing: %s",
                [p["lighter"] for p in PAIRS.values()],
            )

    # ── WebSocket (lighter SDK async) ──────────────────────────

    def _start_ws(self):
        """Start Lighter WebSocket as asyncio task using run_async()."""
        if not self._market_ids:
            logger.warning("Lighter WebSocket not started: no markets discovered")
            return

        market_ids = list(self._market_ids.values())

        self._ws_client = lighter.WsClient(
            order_book_ids=market_ids,
            on_order_book_update=self._on_orderbook_update,
        )
        self._patch_ws_client(market_ids)

        self._ws_task = asyncio.create_task(self._ws_loop())
        logger.info("Lighter WebSocket started for market_ids: %s", market_ids)

    async def _ws_loop(self):
        """Run WsClient.run_async() with reconnection."""
        while self._running:
            try:
                await self._ws_client.run_async()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Lighter WebSocket error: %s", e)
                if self._running:
                    logger.info("Lighter WebSocket reconnecting in 5s...")
                    await asyncio.sleep(5)
                    # Recreate client for reconnection
                    market_ids = list(self._market_ids.values())
                    self._ws_client = lighter.WsClient(
                        order_book_ids=market_ids,
                        on_order_book_update=self._on_orderbook_update,
                    )
                    self._patch_ws_client(market_ids)

    def _patch_ws_client(self, market_ids: list):
        """Patch WsClient to also subscribe to perps_market_stats channels."""
        import json as _json

        ws_client = self._ws_client
        original_handle_connected = ws_client.handle_connected
        original_handle_connected_async = ws_client.handle_connected_async
        client_ref = self

        def patched_handle_connected(ws):
            original_handle_connected(ws)
            for mid in market_ids:
                ws.send(_json.dumps({
                    "type": "subscribe",
                    "channel": f"perps_market_stats/{mid}",
                }))
            logger.info("Lighter WS subscribed to perps_market_stats for %s", market_ids)

        async def patched_handle_connected_async(ws):
            await original_handle_connected_async(ws)
            for mid in market_ids:
                await ws.send(_json.dumps({
                    "type": "subscribe",
                    "channel": f"perps_market_stats/{mid}",
                }))
            logger.info("Lighter WS subscribed to perps_market_stats for %s", market_ids)

        def patched_handle_unhandled(message):
            if not isinstance(message, dict):
                return
            msg_type = message.get("type", "")
            channel = message.get("channel", "")
            # Handle perps_market_stats subscribe + update
            if "perps_market_stats" in msg_type or "perps_market_stats" in channel or "market_stats" in msg_type or "market_stats" in channel:
                client_ref._handle_perps_market_stats(message)
            else:
                logger.info("Lighter WS unhandled: type=%s channel=%s keys=%s", msg_type, channel, list(message.keys()))

        ws_client.handle_connected = patched_handle_connected
        ws_client.handle_connected_async = patched_handle_connected_async
        ws_client.handle_unhandled_message = patched_handle_unhandled

    def _handle_perps_market_stats(self, message: dict):
        """Handle perps_market_stats WebSocket messages."""
        channel = message.get("channel", "")
        # Extract market_id from channel like "perps_market_stats:145"
        parts = channel.split(":")
        if len(parts) < 2:
            # Try from data
            data = message.get("perps_market_stats", message.get("data", message))
            market_id_str = str(data.get("market_id", ""))
        else:
            market_id_str = parts[1]
            data = message.get("perps_market_stats", message.get("data", message))

        pair_name = self._marketid_to_pair.get(market_id_str)
        if not pair_name:
            logger.debug("Lighter perps_market_stats unknown market_id: %s, msg keys: %s",
                        market_id_str, list(message.keys()))
            return

        mark_px = float(data.get("mark_price", 0))
        index_px = float(data.get("index_price", 0))

        if mark_px > 0 and index_px > 0:
            self._mark_index[pair_name] = MarkIndexSnapshot(
                exchange="lighter",
                pair=pair_name,
                mark_price=mark_px,
                index_price=index_px,
            )
            logger.info("Lighter mark-index %s: mark=$%.2f, index=$%.2f (via WS)",
                       pair_name, mark_px, index_px)
        else:
            logger.debug("Lighter perps_market_stats %s: mark=%s, index=%s, keys=%s",
                        pair_name, mark_px, index_px, list(data.keys()))

    def _on_orderbook_update(self, market_id, order_book):
        """Callback from Lighter WebSocket on orderbook update.

        Args:
            market_id: str market_id from channel split
            order_book: dict with "bids" and "asks" lists,
                        each entry has "price" and "size" keys
        """
        pair_name = self._marketid_to_pair.get(str(market_id))
        if not pair_name:
            logger.debug("Lighter unknown market_id: %s", market_id)
            return

        bids = order_book.get("bids", [])
        asks = order_book.get("asks", [])

        if not bids or not asks:
            return

        # Sort bids descending, asks ascending by price
        try:
            sorted_bids = sorted(bids, key=lambda x: float(x["price"]), reverse=True)
            sorted_asks = sorted(asks, key=lambda x: float(x["price"]))
        except (KeyError, ValueError):
            return

        best_bid = float(sorted_bids[0]["price"])
        best_ask = float(sorted_asks[0]["price"])
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
        logger.debug(
            "Lighter price update: %s bid=$%.2f ask=$%.2f",
            pair_name, best_bid, best_ask,
        )
        self._notify_price_update(pair_name)

    # ── REST (funding rate) ────────────────────────────────────

    async def fetch_funding_rate(self, pair: str) -> Optional[float]:
        market_id = self._market_ids.get(pair)
        if market_id is None or not self._api_client:
            return None

        try:
            funding_api = FundingApi(self._api_client)
            result = await funding_api.funding_rates()
            if result and result.funding_rates:
                # Filter by market_id and exchange="lighter"
                for fr in result.funding_rates:
                    if fr.market_id == market_id and fr.exchange == "lighter":
                        rate = float(fr.rate)
                        if pair in self._prices:
                            self._prices[pair].funding_rate = rate
                        logger.debug("Lighter funding %s: %s", pair, rate)
                        return rate
                # Fallback: any exchange for this market_id
                for fr in result.funding_rates:
                    if fr.market_id == market_id:
                        rate = float(fr.rate)
                        if pair in self._prices:
                            self._prices[pair].funding_rate = rate
                        logger.debug("Lighter funding %s (exchange=%s): %s", pair, fr.exchange, rate)
                        return rate
        except Exception as e:
            logger.error("Lighter funding fetch error for %s: %s", pair, e)
        return None

    async def fetch_mark_index(self, pair: str) -> Optional[MarkIndexSnapshot]:
        """Return cached mark-index from WebSocket perps_market_stats channel."""
        return self._mark_index.get(pair)
