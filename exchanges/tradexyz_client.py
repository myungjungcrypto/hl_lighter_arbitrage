"""trade.xyz client via Hyperliquid API (dex='xyz').

Uses WebSocket for real-time L2 book updates, REST fallback for funding rates.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional, Callable

import aiohttp
import websockets

from config import TRADEXYZ_API_URL, TRADEXYZ_WS_URL, PAIRS
from exchanges.base import BaseExchangeClient
from models.snapshots import PriceSnapshot

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)
WS_RECONNECT_DELAY = 5


class TradeXYZClient(BaseExchangeClient):
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._ws_task: asyncio.Task | None = None
        self._prices: dict[str, PriceSnapshot] = {}
        self._running = False
        self._on_price_update: Callable[[str], None] | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)
        return self._session

    async def connect(self):
        self._running = True
        self._ws_task = asyncio.create_task(self._ws_loop())
        logger.info("trade.xyz client connected (WebSocket mode)")

    async def close(self):
        self._running = False
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()

    def get_latest_price(self, pair: str) -> Optional[PriceSnapshot]:
        return self._prices.get(pair)

    # ── WebSocket ──────────────────────────────────────────────

    async def _ws_loop(self):
        """WebSocket connection loop with auto-reconnect."""
        while self._running:
            try:
                async with websockets.connect(TRADEXYZ_WS_URL) as ws:
                    logger.info("trade.xyz WebSocket connected")
                    await self._subscribe_all(ws)
                    async for msg in ws:
                        self._handle_ws_message(msg)
            except websockets.ConnectionClosed as e:
                logger.warning("trade.xyz WebSocket closed: %s", e)
            except Exception as e:
                logger.error("trade.xyz WebSocket error: %s", e)

            if self._running:
                logger.info("trade.xyz WebSocket reconnecting in %ds...", WS_RECONNECT_DELAY)
                await asyncio.sleep(WS_RECONNECT_DELAY)

    async def _subscribe_all(self, ws):
        for pair_name, pair_cfg in PAIRS.items():
            coin = pair_cfg["tradexyz"]
            sub_msg = {
                "method": "subscribe",
                "subscription": {
                    "type": "l2Book",
                    "coin": coin,
                    "nSigFigs": 5,
                },
            }
            await ws.send(json.dumps(sub_msg))
            logger.info("Subscribed to trade.xyz l2Book: %s (%s)", pair_name, coin)

    def _handle_ws_message(self, raw: str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        channel = data.get("channel")
        if channel != "l2Book":
            logger.debug("trade.xyz WS non-l2Book msg: channel=%s keys=%s", channel, list(data.keys()))
            return

        msg_data = data.get("data", {})
        coin = msg_data.get("coin", "")
        levels = msg_data.get("levels")

        logger.debug("trade.xyz l2Book: coin=%s, has_levels=%s", coin, levels is not None)

        if not levels or len(levels) < 2:
            logger.debug("trade.xyz l2Book no levels, data keys: %s", list(msg_data.keys()))
            return

        pair_name = self._coin_to_pair(coin)
        if not pair_name:
            logger.debug("trade.xyz l2Book unknown coin: %s", coin)
            return

        bids = levels[0]
        asks = levels[1]
        if not bids or not asks:
            logger.debug("trade.xyz empty bids/asks for %s", pair_name)
            return

        best_bid = float(bids[0].get("px", 0))
        best_ask = float(asks[0].get("px", 0))
        if best_bid == 0 or best_ask == 0:
            logger.debug("trade.xyz zero price for %s: bid=%s ask=%s", pair_name, best_bid, best_ask)
            return

        logger.debug("trade.xyz price update: %s bid=$%.2f ask=$%.2f", pair_name, best_bid, best_ask)

        self._prices[pair_name] = PriceSnapshot(
            exchange="tradexyz",
            pair=pair_name,
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=(best_bid + best_ask) / 2,
            funding_rate=self._prices.get(pair_name, PriceSnapshot("tradexyz", pair_name)).funding_rate,
            timestamp=time.time(),
        )
        self._notify_price_update(pair_name)

    def _coin_to_pair(self, coin: str) -> Optional[str]:
        for pair_name, pair_cfg in PAIRS.items():
            if pair_cfg["tradexyz"] == coin:
                return pair_name
        return None

    # ── REST (funding rate + fallback) ─────────────────────────

    async def _post_info(self, payload: dict) -> dict | list | None:
        session = await self._get_session()
        url = f"{TRADEXYZ_API_URL}/info"
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    logger.error("trade.xyz info request failed: HTTP %s", resp.status)
                    return None
                return await resp.json()
        except aiohttp.ClientError as e:
            logger.error("trade.xyz info request error: %s", e)
            return None

    async def fetch_funding_rate(self, pair: str) -> Optional[float]:
        pair_cfg = PAIRS.get(pair)
        if not pair_cfg:
            return None

        coin = pair_cfg["tradexyz"]
        # Use metaAndAssetCtxs with dex parameter
        data = await self._post_info({"type": "metaAndAssetCtxs", "dex": "xyz"})
        if not data or not isinstance(data, list) or len(data) < 2:
            return None

        universe = data[0].get("universe", [])
        asset_ctxs = data[1]

        coin_name = coin.split(":")[-1] if ":" in coin else coin

        all_names = [meta.get("name", "") for meta in universe]
        logger.info("trade.xyz universe names (first 20): %s", all_names[:20])

        for meta, ctx in zip(universe, asset_ctxs):
            name = meta.get("name", "").upper()
            # Try full match (e.g. "CL"), then with prefix (e.g. "xyz:CL")
            if name == coin_name.upper() or name == coin.upper():
                rate = float(ctx.get("funding", 0))
                if pair in self._prices:
                    self._prices[pair].funding_rate = rate
                logger.info("trade.xyz funding %s: %s (matched name=%s)", pair, rate, meta.get("name"))
                return rate

        logger.warning(
            "trade.xyz funding not found for %s (looking for '%s' or '%s' in %s)",
            pair, coin_name, coin, all_names[:10],
        )
        return None

    async def fetch_price_rest(self, pair: str) -> Optional[PriceSnapshot]:
        """REST fallback for fetching prices when WebSocket is unavailable."""
        pair_cfg = PAIRS.get(pair)
        if not pair_cfg:
            return None

        coin = pair_cfg["tradexyz"]
        data = await self._post_info({
            "type": "l2Book",
            "coin": coin,
            "nSigFigs": 5,
        })
        if not data or not isinstance(data, dict):
            return None

        levels = data.get("levels")
        if not levels or len(levels) < 2:
            return None

        bids = levels[0]
        asks = levels[1]
        if not bids or not asks:
            return None

        best_bid = float(bids[0].get("px", 0))
        best_ask = float(asks[0].get("px", 0))
        if best_bid == 0 or best_ask == 0:
            return None

        snapshot = PriceSnapshot(
            exchange="tradexyz",
            pair=pair,
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=(best_bid + best_ask) / 2,
            timestamp=time.time(),
        )
        self._prices[pair] = snapshot
        return snapshot
