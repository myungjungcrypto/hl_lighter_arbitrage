import os
import logging
from dotenv import load_dotenv

load_dotenv()

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Exchange API URLs
TRADEXYZ_API_URL = os.getenv("TRADEXYZ_API_URL", "https://api.hyperliquid.xyz")
TRADEXYZ_WS_URL = os.getenv("TRADEXYZ_WS_URL", "wss://api.hyperliquid.xyz/ws")
LIGHTER_API_URL = os.getenv("LIGHTER_API_URL", "https://mainnet.zklighter.elliot.ai")

# Trading Pairs: {internal_name: (tradexyz_symbol, lighter_symbol)}
PAIRS = {
    "WTI": {"tradexyz": "xyz:CL", "lighter": "CL"},
    "BRENT": {"tradexyz": "xyz:BRENTOIL", "lighter": "BZ"},
}

# Alert Defaults
DEFAULT_THRESHOLD = float(os.getenv("DEFAULT_THRESHOLD", "0.50"))
DEFAULT_COOLDOWN = int(os.getenv("DEFAULT_COOLDOWN", "300"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))
FUNDING_FETCH_INTERVAL = int(os.getenv("FUNDING_FETCH_INTERVAL", "60"))

# Database
DB_PATH = os.getenv("DB_PATH", "data/users.db")

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

def setup_logging():
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Suppress noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
