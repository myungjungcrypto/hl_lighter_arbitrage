"""Multi-user Telegram bot with individual DM alerts."""
from __future__ import annotations

import logging
import re
from typing import Optional

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from config import TELEGRAM_BOT_TOKEN, PAIRS
from models.snapshots import SpreadSnapshot
from models.user import UserSettings
from user_store import UserStore

logger = logging.getLogger(__name__)


class TelegramAlertBot:
    def __init__(self, user_store: UserStore, get_snapshot_fn=None):
        """
        Args:
            user_store: SQLite user store for per-user settings
            get_snapshot_fn: async callable(pair) -> SpreadSnapshot | None
        """
        self.user_store = user_store
        self.get_snapshot_fn = get_snapshot_fn
        self._app: Application | None = None

    def build_app(self) -> Application:
        self._app = (
            Application.builder()
            .token(TELEGRAM_BOT_TOKEN)
            .build()
        )
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("threshold", self._cmd_threshold))
        self._app.add_handler(CommandHandler("cooldown", self._cmd_cooldown))
        self._app.add_handler(CommandHandler("mute", self._cmd_mute))
        self._app.add_handler(CommandHandler("unmute", self._cmd_unmute))
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        return self._app

    # ── Commands ───────────────────────────────────────────────

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        username = update.effective_user.username or ""
        is_new = await self.user_store.register_user(chat_id, username)

        if is_new:
            text = (
                "<b>Oil Arbitrage Alert Bot</b>\n\n"
                "trade.xyz vs Lighter.xyz 가격 차이 알림 봇입니다.\n\n"
                "기본 설정:\n"
                "- WTI / BRENT 알림 임계값: $0.50\n"
                "- 쿨다운: 300초\n\n"
                "/help 로 커맨드 목록을 확인하세요."
            )
        else:
            text = "이미 등록된 사용자입니다. /status 로 현재 상태를 확인하세요."

        await update.message.reply_text(text, parse_mode="HTML")

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        user = await self.user_store.get_user_settings(chat_id)
        if not user:
            await update.message.reply_text("먼저 /start 로 등록하세요.")
            return

        lines = ["<b>현재 상태</b>\n"]

        for pair_name in PAIRS:
            ps = user.get_pair(pair_name)
            mute_icon = "🔇" if ps.muted else "🔔"

            # Try to get live snapshot
            snapshot = None
            if self.get_snapshot_fn:
                try:
                    snapshot = await self.get_snapshot_fn(pair_name)
                except Exception:
                    pass

            if snapshot and snapshot.is_valid():
                direction_text = _direction_label(snapshot)
                lines.append(
                    f"\n<b>{pair_name}</b> {mute_icon} ({direction_text})\n"
                    f"  trade.xyz: bid ${snapshot.tradexyz.best_bid:.2f} / ask ${snapshot.tradexyz.best_ask:.2f}\n"
                    f"  Lighter: bid ${snapshot.lighter.best_bid:.2f} / ask ${snapshot.lighter.best_ask:.2f}\n"
                    f"  스프레드: <b>${snapshot.best_spread:+.2f}</b> ({snapshot.spread_pct:+.2f}%)"
                )
                funding_line = _funding_line(snapshot)
                if funding_line:
                    lines.append(f"\n  {funding_line}")
                be_line = _breakeven_line(snapshot)
                if be_line:
                    lines.append(f"\n  {be_line} | 임계값: ${ps.threshold:.2f} | 쿨다운: {user.cooldown}s")
                else:
                    lines.append(f"\n  임계값: ${ps.threshold:.2f} | 쿨다운: {user.cooldown}s")
            else:
                lines.append(f"\n<b>{pair_name}</b> {mute_icon} — 가격 데이터 없음\n")
                lines.append(f"  임계값: ${ps.threshold:.2f} | 쿨다운: {user.cooldown}s")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _cmd_threshold(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        args = context.args

        if not args or len(args) < 2:
            await update.message.reply_text(
                "사용법: /threshold <pair> <value>\n"
                "예: /threshold WTI 0.30\n\n"
                f"가능한 페어: {', '.join(PAIRS.keys())}"
            )
            return

        pair = args[0].upper()
        if pair not in PAIRS:
            await update.message.reply_text(f"잘못된 페어: {pair}. 가능: {', '.join(PAIRS.keys())}")
            return

        try:
            value = float(args[1])
        except ValueError:
            await update.message.reply_text("임계값은 숫자여야 합니다.")
            return

        await self.user_store.set_threshold(chat_id, pair, value)
        await update.message.reply_text(f"{pair} 알림 임계값: ${value:.2f} 로 설정됨")

    async def _cmd_cooldown(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        args = context.args

        if not args:
            await update.message.reply_text("사용법: /cooldown <seconds>\n예: /cooldown 180")
            return

        try:
            seconds = int(args[0])
        except ValueError:
            await update.message.reply_text("쿨다운은 정수(초)여야 합니다.")
            return

        await self.user_store.set_cooldown(chat_id, seconds)
        await update.message.reply_text(f"알림 쿨다운: {seconds}초로 설정됨")

    async def _cmd_mute(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        args = context.args

        if not args:
            await update.message.reply_text(
                f"사용법: /mute <pair>\n가능: {', '.join(PAIRS.keys())}"
            )
            return

        pair = args[0].upper()
        if pair not in PAIRS:
            await update.message.reply_text(f"잘못된 페어: {pair}")
            return

        await self.user_store.set_mute(chat_id, pair, True)
        await update.message.reply_text(f"🔇 {pair} 알림 음소거됨")

    async def _cmd_unmute(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        args = context.args

        if not args:
            await update.message.reply_text(
                f"사용법: /unmute <pair>\n가능: {', '.join(PAIRS.keys())}"
            )
            return

        pair = args[0].upper()
        if pair not in PAIRS:
            await update.message.reply_text(f"잘못된 페어: {pair}")
            return

        await self.user_store.set_mute(chat_id, pair, False)
        await update.message.reply_text(f"🔔 {pair} 알림 해제됨")

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (
            "<b>Oil Arbitrage Alert Bot</b>\n"
            "trade.xyz vs Lighter.xyz\n\n"
            "<b>커맨드:</b>\n"
            "/start — 봇 등록\n"
            "/status — 현재 가격/스프레드/설정 확인\n"
            "/threshold &lt;pair&gt; &lt;value&gt; — 알림 임계값 설정 ($)\n"
            "/cooldown &lt;seconds&gt; — 알림 쿨다운 설정\n"
            "/mute &lt;pair&gt; — 알림 끄기\n"
            "/unmute &lt;pair&gt; — 알림 켜기\n"
            "/help — 도움말\n\n"
            f"<b>페어:</b> {', '.join(PAIRS.keys())}"
        )
        await update.message.reply_text(text, parse_mode="HTML")

    # ── Alert Sending ──────────────────────────────────────────

    async def send_alert(self, chat_id: int, snapshot: SpreadSnapshot, alert_type: str = "entry"):
        """Send a spread alert DM to a specific user."""
        if not self._app:
            return

        if alert_type == "entry":
            icon = "🔔"
            header = "스프레드 알림"
        else:
            icon = "⚠️"
            header = "방향 전환 알림"

        direction_text = _direction_label(snapshot)
        text = (
            f"{icon} <b>{header}: {snapshot.pair}</b> ({direction_text})\n\n"
            f"  trade.xyz: bid ${snapshot.tradexyz.best_bid:.2f} / ask ${snapshot.tradexyz.best_ask:.2f}\n"
            f"  Lighter: bid ${snapshot.lighter.best_bid:.2f} / ask ${snapshot.lighter.best_ask:.2f}\n"
            f"  스프레드: <b>${snapshot.best_spread:+.2f}</b> ({snapshot.spread_pct:+.2f}%)\n"
        )

        funding_line = _funding_line(snapshot)
        if funding_line:
            text += f"  {funding_line}\n"
        be_line = _breakeven_line(snapshot)
        if be_line:
            text += f"  {be_line}\n"

        try:
            await self._app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
            )
        except Exception as e:
            safe_err = _sanitize_token(str(e))
            logger.error("Failed to send alert to %d: %s", chat_id, safe_err)

    async def broadcast_startup(self):
        """Send startup notification to all registered users."""
        users = await self.user_store.get_all_users()
        for user in users:
            try:
                await self._app.bot.send_message(
                    chat_id=user.chat_id,
                    text="🚀 Oil Arbitrage Monitor 시작됨\ntrade.xyz vs Lighter.xyz",
                    parse_mode="HTML",
                )
            except Exception:
                pass


def _sanitize_token(text: str) -> str:
    """Remove bot token from error messages."""
    return re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot***:***", text)


def _direction_label(snapshot: SpreadSnapshot) -> str:
    """e.g. 'Lighter 숏 + trade.xyz 롱'"""
    if snapshot.best_direction == "LIGHTER_CHEAP":
        return "Lighter 롱 + trade.xyz 숏"
    return "trade.xyz 롱 + Lighter 숏"


def _funding_line(snapshot: SpreadSnapshot) -> str:
    """Format funding rates like: 펀딩(1h) trade.xyz: +0.0794% | Lighter: +0.0003%"""
    txyz_fr = snapshot.tradexyz.funding_rate
    ltr_fr = snapshot.lighter.funding_rate
    if txyz_fr is None and ltr_fr is None:
        return ""
    txyz_str = f"{txyz_fr * 100:+.4f}%" if txyz_fr is not None else "N/A"
    ltr_str = f"{ltr_fr * 100:+.4f}%" if ltr_fr is not None else "N/A"
    return f"펀딩(1h) trade.xyz: {txyz_str} | Lighter: {ltr_str}"


def _breakeven_line(snapshot: SpreadSnapshot) -> str:
    """Format breakeven hours like: 손익분기: ⏰ 7h38m"""
    beh = snapshot.breakeven_hours
    if beh is None:
        return ""
    hours = int(beh)
    minutes = int((beh - hours) * 60)
    return f"손익분기: ⏰ {hours}h{minutes:02d}m"
