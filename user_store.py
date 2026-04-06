"""SQLite-based multi-user settings persistence."""
from __future__ import annotations

import logging
import os
from typing import List

import aiosqlite

from config import DB_PATH, DEFAULT_THRESHOLD, DEFAULT_COOLDOWN, PAIRS
from models.user import UserSettings, PairSettings

logger = logging.getLogger(__name__)


class UserStore:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

    async def initialize(self):
        """Create tables if they don't exist."""
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    chat_id INTEGER PRIMARY KEY,
                    username TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute(f"""
                CREATE TABLE IF NOT EXISTS settings (
                    chat_id INTEGER NOT NULL,
                    pair TEXT NOT NULL,
                    threshold REAL DEFAULT {DEFAULT_THRESHOLD},
                    cooldown INTEGER DEFAULT {DEFAULT_COOLDOWN},
                    muted INTEGER DEFAULT 0,
                    last_alert_time REAL DEFAULT 0.0,
                    PRIMARY KEY (chat_id, pair),
                    FOREIGN KEY (chat_id) REFERENCES users(chat_id)
                )
            """)
            await db.commit()
        logger.info("UserStore initialized: %s", self.db_path)

    async def register_user(self, chat_id: int, username: str = "") -> bool:
        """Register a new user. Returns True if newly created, False if already existed."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT chat_id FROM users WHERE chat_id = ?", (chat_id,)
            )
            if await cursor.fetchone():
                return False

            await db.execute(
                "INSERT INTO users (chat_id, username) VALUES (?, ?)",
                (chat_id, username),
            )
            # Initialize default settings for each pair
            for pair_name in PAIRS:
                await db.execute(
                    "INSERT INTO settings (chat_id, pair, threshold, cooldown) VALUES (?, ?, ?, ?)",
                    (chat_id, pair_name, DEFAULT_THRESHOLD, DEFAULT_COOLDOWN),
                )
            await db.commit()
        logger.info("New user registered: %d (%s)", chat_id, username)
        return True

    async def get_all_users(self) -> List[UserSettings]:
        """Get all registered users with their settings."""
        users = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT chat_id, username FROM users")
            user_rows = await cursor.fetchall()

            for row in user_rows:
                user = UserSettings(
                    chat_id=row["chat_id"],
                    username=row["username"],
                )
                settings_cursor = await db.execute(
                    "SELECT pair, threshold, cooldown, muted, last_alert_time "
                    "FROM settings WHERE chat_id = ?",
                    (row["chat_id"],),
                )
                settings_rows = await settings_cursor.fetchall()
                for s in settings_rows:
                    user.cooldown = s["cooldown"]
                    user.pair_settings[s["pair"]] = PairSettings(
                        threshold=s["threshold"],
                        muted=bool(s["muted"]),
                        last_alert_time=s["last_alert_time"],
                    )
                users.append(user)
        return users

    async def get_user_settings(self, chat_id: int) -> UserSettings | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT chat_id, username FROM users WHERE chat_id = ?", (chat_id,)
            )
            row = await cursor.fetchone()
            if not row:
                return None

            user = UserSettings(chat_id=row["chat_id"], username=row["username"])
            settings_cursor = await db.execute(
                "SELECT pair, threshold, cooldown, muted, last_alert_time "
                "FROM settings WHERE chat_id = ?",
                (chat_id,),
            )
            for s in await settings_cursor.fetchall():
                user.cooldown = s["cooldown"]
                user.pair_settings[s["pair"]] = PairSettings(
                    threshold=s["threshold"],
                    muted=bool(s["muted"]),
                    last_alert_time=s["last_alert_time"],
                )
            return user

    async def set_threshold(self, chat_id: int, pair: str, value: float):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE settings SET threshold = ? WHERE chat_id = ? AND pair = ?",
                (value, chat_id, pair),
            )
            await db.commit()

    async def set_cooldown(self, chat_id: int, seconds: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE settings SET cooldown = ? WHERE chat_id = ?",
                (seconds, chat_id),
            )
            await db.commit()

    async def set_mute(self, chat_id: int, pair: str, muted: bool):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE settings SET muted = ? WHERE chat_id = ? AND pair = ?",
                (int(muted), chat_id, pair),
            )
            await db.commit()

    async def update_last_alert_time(self, chat_id: int, pair: str, timestamp: float):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE settings SET last_alert_time = ? WHERE chat_id = ? AND pair = ?",
                (timestamp, chat_id, pair),
            )
            await db.commit()
