"""Spread monitor: event-driven spread calculation and alert dispatch."""
from __future__ import annotations

import asyncio
import csv
import logging
import os
import time
from typing import Optional

from config import PAIRS, FUNDING_FETCH_INTERVAL
from exchanges.base import BaseExchangeClient
from models.snapshots import PriceSnapshot, SpreadSnapshot, MarkIndexSnapshot
from user_store import UserStore
from alerts.telegram_bot import TelegramAlertBot

logger = logging.getLogger(__name__)

CSV_LOG = "spread_log.csv"


class SpreadMonitor:
    def __init__(
        self,
        tradexyz: BaseExchangeClient,
        lighter: BaseExchangeClient,
        user_store: UserStore,
        telegram: TelegramAlertBot,
    ):
        self.tradexyz = tradexyz
        self.lighter = lighter
        self.user_store = user_store
        self.telegram = telegram
        self._last_direction: dict[str, str] = {}  # pair -> direction
        # Track which (user, pair) combos have an active entry position
        # Key: (chat_id, pair), Value: direction at entry time
        self._active_entries: dict[tuple[int, str], str] = {}
        self._alert_queue: asyncio.Queue = asyncio.Queue()
        self._mark_index_queue: asyncio.Queue = asyncio.Queue()
        self._last_mark_index: dict[str, MarkIndexSnapshot] = {}  # "exchange:pair" -> snapshot

    def get_snapshot(self, pair: str) -> Optional[SpreadSnapshot]:
        """Build a SpreadSnapshot from cached prices (sync, for status queries)."""
        txyz = self.tradexyz.get_latest_price(pair)
        ltr = self.lighter.get_latest_price(pair)

        if not txyz or not ltr or not txyz.is_valid() or not ltr.is_valid():
            return None

        return SpreadSnapshot(
            pair=pair,
            tradexyz=txyz,
            lighter=ltr,
            timestamp=time.time(),
        )

    async def get_snapshot_async(self, pair: str) -> Optional[SpreadSnapshot]:
        """Async wrapper for get_snapshot (used by Telegram bot)."""
        return self.get_snapshot(pair)

    def get_mark_index(self, exchange: str, pair: str) -> Optional[MarkIndexSnapshot]:
        key = f"{exchange}:{pair}"
        return self._last_mark_index.get(key)

    # ── Event-Driven Price Update ──────────────────────────────

    def on_price_update(self, pair: str):
        """Called by exchange clients when a price update is received."""
        snapshot = self.get_snapshot(pair)
        if snapshot and snapshot.is_valid():
            # Non-blocking: put into queue for async processing
            try:
                self._alert_queue.put_nowait(snapshot)
            except asyncio.QueueFull:
                pass  # Drop if queue is full (shouldn't happen)

    # ── Alert Processing Loop ──────────────────────────────────

    async def run_alert_processor(self):
        """Process alert queue: check user thresholds and send DMs."""
        logger.info("Alert processor started")
        while True:
            snapshot = await self._alert_queue.get()
            try:
                await self._process_snapshot(snapshot)
            except Exception as e:
                logger.error("Error processing snapshot for %s: %s", snapshot.pair, e)

    async def _process_snapshot(self, snapshot: SpreadSnapshot):
        """Evaluate snapshot against all users' settings and send alerts.

        Alert logic:
        1. Entry alert: spread exceeds user threshold → send entry alert, mark position active
        2. Exit alert: direction reverses ONLY IF user has an active entry → send exit alert, clear position
        """
        pair = snapshot.pair
        direction = snapshot.best_direction
        now = time.time()

        # Log to CSV
        self._log_csv(snapshot)

        # Track direction
        self._last_direction[pair] = direction

        # Get all users and check alert conditions
        users = await self.user_store.get_all_users()

        for user in users:
            pair_settings = user.pair_settings.get(pair)
            if not pair_settings:
                continue
            if pair_settings.muted:
                continue

            key = (user.chat_id, pair)
            time_since_last = now - pair_settings.last_alert_time

            # Check if user has active entry for this pair
            active_direction = self._active_entries.get(key)

            # Exit alert: direction reversed AND user had an active entry
            if active_direction is not None and active_direction != direction:
                await self.telegram.send_alert(user.chat_id, snapshot, alert_type="exit")
                await self.user_store.update_last_alert_time(user.chat_id, pair, now)
                del self._active_entries[key]  # Clear active position
                logger.info("Exit alert sent for %s to %d (was %s, now %s)",
                           pair, user.chat_id, active_direction, direction)
                continue

            # Entry alert: spread exceeds threshold + cooldown elapsed
            if abs(snapshot.best_spread) >= pair_settings.threshold:
                if time_since_last >= user.cooldown:
                    await self.telegram.send_alert(user.chat_id, snapshot, alert_type="entry")
                    await self.user_store.update_last_alert_time(user.chat_id, pair, now)
                    self._active_entries[key] = direction  # Mark entry active
                    logger.info("Entry alert sent for %s to %d (direction=%s, spread=$%.2f)",
                               pair, user.chat_id, direction, snapshot.best_spread)

    # ── Funding Rate Fetcher ───────────────────────────────────

    async def run_funding_fetcher(self):
        """Periodically fetch funding rates from both exchanges."""
        logger.info("Funding rate fetcher started (interval: %ds)", FUNDING_FETCH_INTERVAL)
        while True:
            for pair_name in PAIRS:
                try:
                    results = await asyncio.gather(
                        self.tradexyz.fetch_funding_rate(pair_name),
                        self.lighter.fetch_funding_rate(pair_name),
                        return_exceptions=True,
                    )
                    txyz_rate, ltr_rate = results
                    if isinstance(txyz_rate, Exception):
                        logger.error("trade.xyz funding error for %s: %s", pair_name, txyz_rate)
                        txyz_rate = None
                    if isinstance(ltr_rate, Exception):
                        logger.error("Lighter funding error for %s: %s", pair_name, ltr_rate)
                        ltr_rate = None
                    logger.info(
                        "Funding rates %s: trade.xyz=%s, Lighter=%s",
                        pair_name, txyz_rate, ltr_rate,
                    )
                except Exception as e:
                    logger.error("Funding fetch error for %s: %s", pair_name, e)

                # Fetch mark-index prices (trade.xyz already extracted during funding fetch)
                try:
                    mi_results = await asyncio.gather(
                        self.tradexyz.fetch_mark_index(pair_name),
                        self.lighter.fetch_mark_index(pair_name),
                        return_exceptions=True,
                    )
                    for mi in mi_results:
                        if isinstance(mi, Exception):
                            logger.error("Mark-index fetch exception for %s: %s", pair_name, mi)
                        elif isinstance(mi, MarkIndexSnapshot) and mi.is_valid():
                            key = f"{mi.exchange}:{mi.pair}"
                            self._last_mark_index[key] = mi
                            self._mark_index_queue.put_nowait(mi)
                        elif mi is not None:
                            logger.warning("Mark-index invalid for %s: %s", pair_name, mi)
                except Exception as e:
                    logger.error("Mark-index fetch error for %s: %s", pair_name, e)

            await asyncio.sleep(FUNDING_FETCH_INTERVAL)

    # ── Mark-Index Alert Processor ───────────────────────────────

    async def run_mark_index_processor(self):
        """Process mark-index queue: check user thresholds and send alerts."""
        logger.info("Mark-index alert processor started")
        while True:
            snapshot = await self._mark_index_queue.get()
            try:
                await self._process_mark_index(snapshot)
            except Exception as e:
                logger.error("Error processing mark-index for %s/%s: %s",
                            snapshot.exchange, snapshot.pair, e)

    async def _process_mark_index(self, snapshot: MarkIndexSnapshot):
        gap_pct = snapshot.gap_pct
        pair = snapshot.pair
        now = time.time()

        users = await self.user_store.get_all_users()
        for user in users:
            mi = user.mark_index_settings.get(pair)
            if not mi or mi.muted:
                continue

            time_since_last = now - mi.last_alert_time

            # Check "above" threshold: gap exceeds threshold
            if mi.above_threshold is not None and gap_pct > mi.above_threshold:
                if time_since_last >= mi.cooldown:
                    await self.telegram.send_mark_index_alert(
                        user.chat_id, snapshot, "above", mi.above_threshold,
                    )
                    await self.user_store.update_mark_index_alert_time(user.chat_id, pair, now)

            # Check "below" threshold: gap narrows below threshold
            elif mi.below_threshold is not None and gap_pct < mi.below_threshold:
                if time_since_last >= mi.cooldown:
                    await self.telegram.send_mark_index_alert(
                        user.chat_id, snapshot, "below", mi.below_threshold,
                    )
                    await self.user_store.update_mark_index_alert_time(user.chat_id, pair, now)

    # ── CSV Logging ────────────────────────────────────────────

    def _log_csv(self, s: SpreadSnapshot):
        file_exists = os.path.exists(CSV_LOG)
        try:
            with open(CSV_LOG, "a", newline="") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow([
                        "timestamp", "pair",
                        "txyz_bid", "txyz_ask", "txyz_mid",
                        "ltr_bid", "ltr_ask", "ltr_mid",
                        "spread", "spread_pct", "direction",
                        "txyz_funding", "ltr_funding",
                    ])
                writer.writerow([
                    f"{s.timestamp:.3f}", s.pair,
                    f"{s.tradexyz.best_bid:.2f}", f"{s.tradexyz.best_ask:.2f}",
                    f"{s.tradexyz.mid_price:.2f}",
                    f"{s.lighter.best_bid:.2f}", f"{s.lighter.best_ask:.2f}",
                    f"{s.lighter.mid_price:.2f}",
                    f"{s.best_spread:+.4f}", f"{s.spread_pct:+.4f}",
                    s.best_direction,
                    s.tradexyz.funding_rate or "",
                    s.lighter.funding_rate or "",
                ])
        except Exception as e:
            logger.error("CSV log error: %s", e)
