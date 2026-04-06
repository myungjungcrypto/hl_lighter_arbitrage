"""Entry point for Brent/WTI arbitrage alert system.

trade.xyz vs Lighter.xyz spread monitoring with multi-user Telegram alerts.
"""
import asyncio
import logging

from config import TELEGRAM_BOT_TOKEN, setup_logging
from exchanges.tradexyz_client import TradeXYZClient
from exchanges.lighter_client import LighterClient
from monitor import SpreadMonitor
from alerts.telegram_bot import TelegramAlertBot
from user_store import UserStore

setup_logging()
logger = logging.getLogger(__name__)


async def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error(
            "TELEGRAM_BOT_TOKEN not set. Copy .env.example to .env and fill in values."
        )
        return

    # Initialize components
    user_store = UserStore()
    await user_store.initialize()

    tradexyz = TradeXYZClient()
    lighter = LighterClient()

    telegram = TelegramAlertBot(user_store)
    monitor = SpreadMonitor(tradexyz, lighter, user_store, telegram)

    # Wire up: Telegram bot can query live snapshots via monitor
    telegram.get_snapshot_fn = monitor.get_snapshot_async
    telegram.get_mark_index_fn = monitor.get_mark_index

    # Wire up: exchange clients notify monitor on price updates
    tradexyz.set_on_price_update(monitor.on_price_update)
    lighter.set_on_price_update(monitor.on_price_update)

    # Build Telegram app
    app = telegram.build_app()

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        try:
            # Connect exchanges (WebSocket + market discovery)
            await tradexyz.connect()
            await lighter.connect()

            logger.info("All exchange connections established")
            await telegram.broadcast_startup()

            # Run alert processor, funding fetcher, and mark-index processor
            await asyncio.gather(
                monitor.run_alert_processor(),
                monitor.run_funding_fetcher(),
                monitor.run_mark_index_processor(),
            )

        except (asyncio.CancelledError, KeyboardInterrupt):
            logger.info("Shutting down...")
        except Exception as e:
            logger.error("Unexpected error: %s", e, exc_info=True)
        finally:
            logger.info("Cleaning up...")
            try:
                await app.updater.stop()
            except Exception:
                pass
            try:
                await app.stop()
            except Exception:
                pass
            try:
                await tradexyz.close()
            except Exception:
                pass
            try:
                await lighter.close()
            except Exception:
                pass
            logger.info("Cleanup complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Stopped.")
