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
from models.snapshots import SpreadSnapshot, MarkIndexSnapshot
from models.user import UserSettings
from user_store import UserStore

logger = logging.getLogger(__name__)


class TelegramAlertBot:
    def __init__(self, user_store: UserStore, get_snapshot_fn=None, get_mark_index_fn=None):
        """
        Args:
            user_store: SQLite user store for per-user settings
            get_snapshot_fn: async callable(pair) -> SpreadSnapshot | None
            get_mark_index_fn: callable(exchange, pair) -> MarkIndexSnapshot | None
        """
        self.user_store = user_store
        self.get_snapshot_fn = get_snapshot_fn
        self.get_mark_index_fn = get_mark_index_fn
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
        self._app.add_handler(CommandHandler("mi", self._cmd_mi))
        self._app.add_handler(CommandHandler("mi_mute", self._cmd_mi_mute))
        self._app.add_handler(CommandHandler("mi_unmute", self._cmd_mi_unmute))
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

            # Mark vs Index section
            mi_lines = _mark_index_status_lines(pair_name, user, self.get_mark_index_fn)
            if mi_lines:
                lines.append(mi_lines)

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
        await update.message.reply_text(f"🔇 {pair} 스프레드 알림 음소거됨")

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
        await update.message.reply_text(f"🔔 {pair} 스프레드 알림 해제됨")

    # ── Mark-Index Commands ────────────────────────────────────

    async def _cmd_mi(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set mark-index threshold: /mi <pair> <above|below> <value|off>"""
        chat_id = update.effective_chat.id
        args = context.args

        if not args or len(args) < 3:
            await update.message.reply_text(
                "사용법: /mi &lt;pair&gt; &lt;above|below&gt; &lt;value|off&gt;\n\n"
                "예시:\n"
                "  /mi WTI above 0.3 — WTI 마크-인덱스 갭 &gt; 0.3% 시 알림\n"
                "  /mi BRENT below 0.2 — BRENT 갭 &lt; 0.2% 시 알림\n"
                "  /mi WTI above off — above 알림 비활성화\n\n"
                f"가능한 페어: {', '.join(PAIRS.keys())}",
                parse_mode="HTML",
            )
            return

        pair = args[0].upper()
        if pair not in PAIRS:
            await update.message.reply_text(f"잘못된 페어: {pair}. 가능: {', '.join(PAIRS.keys())}")
            return

        direction = args[1].lower()
        if direction not in ("above", "below"):
            await update.message.reply_text("방향: above 또는 below")
            return

        value_str = args[2].lower()
        if value_str == "off":
            await self.user_store.set_mark_index_threshold(chat_id, pair, direction, None)
            label = "갭 확대" if direction == "above" else "갭 축소"
            await update.message.reply_text(f"📊 {pair} Mark-Index {label} 알림 비활성화됨")
        else:
            try:
                value = float(value_str)
            except ValueError:
                await update.message.reply_text("값은 숫자 또는 'off'여야 합니다.")
                return
            await self.user_store.set_mark_index_threshold(chat_id, pair, direction, value)
            if direction == "above":
                await update.message.reply_text(
                    f"📈 {pair} Mark-Index 알림: 갭 > {value}% 시 알림"
                )
            else:
                await update.message.reply_text(
                    f"📉 {pair} Mark-Index 알림: 갭 < {value}% 시 알림"
                )

    async def _cmd_mi_mute(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        args = context.args

        if not args:
            await update.message.reply_text(
                f"사용법: /mi_mute <pair>\n가능: {', '.join(PAIRS.keys())}"
            )
            return

        pair = args[0].upper()
        if pair not in PAIRS:
            await update.message.reply_text(f"잘못된 페어: {pair}")
            return

        await self.user_store.set_mark_index_mute(chat_id, pair, True)
        await update.message.reply_text(f"🔇 {pair} Mark-Index 알림 음소거됨")

    async def _cmd_mi_unmute(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        args = context.args

        if not args:
            await update.message.reply_text(
                f"사용법: /mi_unmute <pair>\n가능: {', '.join(PAIRS.keys())}"
            )
            return

        pair = args[0].upper()
        if pair not in PAIRS:
            await update.message.reply_text(f"잘못된 페어: {pair}")
            return

        await self.user_store.set_mark_index_mute(chat_id, pair, False)
        await update.message.reply_text(f"🔔 {pair} Mark-Index 알림 해제됨")

    # ── Help ───────────────────────────────────────────────────

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (
            "<b>Oil Arbitrage Alert Bot</b>\n"
            "trade.xyz vs Lighter.xyz\n\n"
            "<b>스프레드 알림:</b>\n"
            "/start — 봇 등록\n"
            "/status — 현재 가격/스프레드/설정 확인\n"
            "/threshold &lt;pair&gt; &lt;$&gt; — 스프레드 임계값 설정\n"
            "  예: /threshold WTI 0.30\n"
            "/cooldown &lt;초&gt; — 스프레드 알림 쿨다운\n"
            "  예: /cooldown 180\n"
            "/mute &lt;pair&gt; — 스프레드 알림 끄기\n"
            "/unmute &lt;pair&gt; — 스프레드 알림 켜기\n\n"
            "<b>Mark vs Index 알림:</b>\n"
            "/mi &lt;pair&gt; &lt;above|below&gt; &lt;%&gt; — 마크-인덱스 갭 알림 설정\n"
            "  예: /mi WTI above 0.3 — 갭 &gt; 0.3% 시 알림\n"
            "  예: /mi BRENT below 0.2 — 갭 &lt; 0.2% 시 알림\n"
            "  예: /mi WTI above off — 해당 방향 비활성화\n"
            "/mi_mute &lt;pair&gt; — Mark-Index 알림 끄기\n"
            "/mi_unmute &lt;pair&gt; — Mark-Index 알림 켜기\n\n"
            "/help — 이 도움말\n\n"
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

    async def send_mark_index_alert(
        self, chat_id: int, snapshot: MarkIndexSnapshot, direction: str, threshold: float
    ):
        """Send a mark-index alert DM."""
        if not self._app:
            return

        exchange_label = "trade.xyz" if snapshot.exchange == "tradexyz" else "Lighter"
        if direction == "above":
            icon = "📈"
            desc = f"갭 확대: {snapshot.gap_pct:.3f}% > {threshold}%"
        else:
            icon = "📉"
            desc = f"갭 축소: {snapshot.gap_pct:.3f}% < {threshold}%"

        text = (
            f"{icon} <b>Mark-Index 알림: {snapshot.pair}</b> ({exchange_label})\n\n"
            f"  Mark: ${snapshot.mark_price:.2f}\n"
            f"  Index: ${snapshot.index_price:.2f}\n"
            f"  갭: {snapshot.gap_signed_pct:+.3f}%\n"
            f"  {desc}"
        )

        try:
            await self._app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
            )
        except Exception as e:
            safe_err = _sanitize_token(str(e))
            logger.error("Failed to send mark-index alert to %d: %s", chat_id, safe_err)

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


# ── Helper Functions ───────────────────────────────────────

def _sanitize_token(text: str) -> str:
    """Remove bot token from error messages."""
    return re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot***:***", text)


def _direction_label(snapshot: SpreadSnapshot) -> str:
    if snapshot.best_direction == "LIGHTER_CHEAP":
        return "Lighter 롱 + trade.xyz 숏"
    return "trade.xyz 롱 + Lighter 숏"


def _funding_line(snapshot: SpreadSnapshot) -> str:
    txyz_fr = snapshot.tradexyz.funding_rate
    ltr_fr = snapshot.lighter.funding_rate
    if txyz_fr is None and ltr_fr is None:
        return ""
    txyz_str = f"{txyz_fr * 100:+.4f}%" if txyz_fr is not None else "N/A"
    ltr_str = f"{ltr_fr * 100:+.4f}%" if ltr_fr is not None else "N/A"
    return f"펀딩(1h) trade.xyz: {txyz_str} | Lighter: {ltr_str}"


def _breakeven_line(snapshot: SpreadSnapshot) -> str:
    beh = snapshot.breakeven_hours
    if beh is None:
        return ""
    hours = int(beh)
    minutes = int((beh - hours) * 60)
    return f"손익분기: ⏰ {hours}h{minutes:02d}m"


def _mark_index_status_lines(pair_name: str, user, get_mark_index_fn) -> str:
    """Build mark vs index status lines for /status command."""
    mi_settings = user.get_mark_index(pair_name)
    lines = []
    lines.append(f"\n  <b>Mark vs Index:</b>")

    # trade.xyz mark/index
    mi = get_mark_index_fn("tradexyz", pair_name) if get_mark_index_fn else None
    if mi and mi.is_valid():
        lines.append(
            f"    trade.xyz: mark ${mi.mark_price:.2f} / index ${mi.index_price:.2f} "
            f"(갭 {mi.gap_signed_pct:+.3f}%)"
        )
    else:
        lines.append(f"    trade.xyz: 데이터 없음")
    # Lighter: public API doesn't expose mark/index
    lines.append(f"    Lighter: 미지원 (공개 API 없음)")

    # Show settings
    mute_icon = "🔇" if mi_settings.muted else ""
    above_str = f"above {mi_settings.above_threshold}% ON" if mi_settings.above_threshold is not None else "above: OFF"
    below_str = f"below {mi_settings.below_threshold}% ON" if mi_settings.below_threshold is not None else "below: OFF"
    lines.append(f"    설정: {above_str} | {below_str} {mute_icon}")

    return "\n".join(lines)
