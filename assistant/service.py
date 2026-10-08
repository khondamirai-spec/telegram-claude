"""Core logic: decide whether to answer a business message and draft the reply."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BusinessConnection, Message

from .claude_cli import ClaudeCLI, ClaudeError, ClaudeLimitError
from .config import Settings
from .language import detect_language
from .prompts import SKIP_TOKEN, build_system_prompt, build_user_prompt
from .store import HistoryStore, RuntimeState, SeenMessages, append_private_line, ensure_private_dir

log = logging.getLogger(__name__)

TELEGRAM_CHUNK = 4000
ERROR_ALERT_INTERVAL = 600.0


class Outcome(str, Enum):
    PREVIEWED = "previewed"
    SENT = "sent"
    DUPLICATE = "duplicate"
    IGNORED_BOT = "ignored_bot"
    NO_CONNECTION = "no_connection"
    FOREIGN_CONNECTION = "foreign_connection"
    CONNECTION_DISABLED = "connection_disabled"
    UNSUPPORTED = "unsupported"
    OWNER_MESSAGE = "owner_message"
    OWNER_ACTIVE = "owner_active"
    PAUSED = "paused"
    LIMITED = "limited"
    SKIPPED = "skipped"
    PERMISSION_DENIED = "permission_denied"
    ERROR = "error"


@dataclass
class Preview:
    connection_id: str
    chat_id: int
    chat_name: str
    language: Optional[str]
    incoming: str
    reply: str
    note: str = ""


def print_preview(preview: Preview) -> None:
    lines = [
        "----- PREVIEW, not sent -----",
        f"Chat:     {preview.chat_name} ({preview.chat_id})",
        f"Language: {preview.language or 'unknown'}",
        f"Incoming: {preview.incoming}",
        f"Reply:    {preview.reply}",
    ]
    if preview.note:
        lines.append(f"Note:     {preview.note}")
    lines.append("-----------------------------")
    log.info("\n".join(lines))


def describe(message: Message) -> Optional[str]:
    """Text for the conversation history; None for service messages."""
    if message.text:
        return message.text
    caption = (message.caption or "").strip()

    def media(label: str) -> str:
        return f"[{label}] {caption}" if caption else f"[{label}]"

    if message.sticker:
        return f"[sticker] {message.sticker.emoji or ''}".strip()
    if message.voice:
        return media("voice message")
    if message.video_note:
        return "[video message]"
    if message.photo:
        return media("photo")
    if message.video:
        return media("video")
    if message.animation:
        return media("GIF")
    if message.audio:
        return media("audio")
    if message.document:
        return media("file")
    if message.contact:
        return "[contact]"
    if message.venue:
        return f"[place] {message.venue.title}"
    if message.location:
        return "[location]"
    if message.poll:
        return f"[poll] {message.poll.question}"
    if message.dice:
        return f"[dice] {message.dice.emoji}"
    return None


def can_reply(connection: BusinessConnection) -> bool:
    rights = getattr(connection, "rights", None)
    if rights is not None:
        return bool(getattr(rights, "can_reply", False))
    return bool(getattr(connection, "can_reply", False))


def read_profile(path: Path) -> str:
    """Read profile.md fresh for every reply, without <!-- comments -->."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip()


def profile_is_filled(profile: str) -> bool:
    """False for the empty template (only headings and empty "- Key:" lines)."""
    for line in profile.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- ") and line.endswith(":"):
            continue
        return True
    return False


def _format_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


class Assistant:
    def __init__(
        self,
        settings: Settings,
        generator: Any,
        *,
        clock: Callable[[], float] = time.time,
        preview_sink: Callable[[Preview], None] = print_preview,
    ):
        self.settings = settings
        self.generator = generator
        self.clock = clock
        self.preview_sink = preview_sink
        data_dir = ensure_private_dir(settings.data_dir)
        self.history = HistoryStore(data_dir / "history.json", settings.history_limit)
        self.seen = SeenMessages(data_dir / "seen.json")
        self.state = RuntimeState(data_dir / "state.json")
        self.previews_path = data_dir / "previews.jsonl"
        self.connections: Dict[str, BusinessConnection] = {}
        self.outcomes: Deque[Outcome] = deque(maxlen=100)
        self._takeover_until: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._last_error_alert: Optional[float] = None

    # ----------------------------------------------------------------- updates

    async def on_business_connection(self, connection: BusinessConnection) -> None:
        self.connections[connection.id] = connection
        if connection.user.id != self.settings.owner_id:
            log.warning(
                "Business connection %s belongs to user %s, not OWNER_ID; it will be ignored",
                connection.id, connection.user.id,
            )
            return
        log.info(
            "Business connection %s: user=%s enabled=%s can_reply=%s",
            connection.id, connection.user.id, connection.is_enabled, can_reply(connection),
        )

    async def on_business_message(self, bot: Bot, message: Message) -> Outcome:
        outcome = await self._handle(bot, message)
        self.outcomes.append(outcome)
        log.debug("Business message %s in chat %s: %s", message.message_id, message.chat.id, outcome.value)
        return outcome

    async def _handle(self, bot: Bot, message: Message) -> Outcome:
        connection_id = message.business_connection_id
        if not connection_id:
            return Outcome.NO_CONNECTION
        chat_id = message.chat.id
        # Mark as seen before any await so parallel duplicates are caught.
        if not self.seen.first_time(f"{connection_id}:{chat_id}:{message.message_id}"):
            return Outcome.DUPLICATE
        sender = message.from_user
        if message.sender_business_bot is not None or (sender is not None and sender.is_bot):
            return Outcome.IGNORED_BOT

        connection = self.connections.get(connection_id)
        if connection is None:
            try:
                connection = await bot.get_business_connection(connection_id)
            except TelegramAPIError as error:
                log.warning("Could not look up business connection %s: %s", connection_id, error)
                return Outcome.NO_CONNECTION
            self.connections[connection_id] = connection
        if connection.user.id != self.settings.owner_id:
            return Outcome.FOREIGN_CONNECTION
        if not connection.is_enabled:
            return Outcome.CONNECTION_DISABLED

        text = describe(message)
        if text is None:
            return Outcome.UNSUPPORTED

        now = self.clock()
        chat_key = HistoryStore.key(connection_id, chat_id)
        if sender is not None and sender.id == self.settings.owner_id:
            self.history.add(
                connection_id, chat_id, role="owner", name="You", text=text,
                lang=detect_language(text), ts=now,
            )
            if self.settings.owner_takeover_minutes > 0:
                self._takeover_until[chat_key] = now + self.settings.owner_takeover_minutes * 60
            return Outcome.OWNER_MESSAGE

        language = detect_language(text) or self.history.last_language(connection_id, chat_id)
        chat_name = (
            (sender.full_name if sender else None) or message.chat.full_name or str(chat_id)
        )
        self.history.add(
            connection_id, chat_id, role="other", name=chat_name, text=text, lang=language, ts=now,
        )

        lock = self._locks.setdefault(chat_key, asyncio.Lock())
        async with lock:
            blocked = self._blocked(chat_key)
            if blocked is not None:
                return blocked
            return await self._reply(bot, connection_id, chat_id, chat_name, chat_key, text, language)

    async def _reply(
        self,
        bot: Bot,
        connection_id: str,
        chat_id: int,
        chat_name: str,
        chat_key: str,
        incoming: str,
        language: Optional[str],
    ) -> Outcome:
        system_prompt = build_system_prompt(
            read_profile(self.settings.profile_path), datetime.fromtimestamp(self.clock())
        )
        user_prompt = build_user_prompt(self.history.get(connection_id, chat_id), language)
        try:
            reply = await self.generator.generate(system_prompt, user_prompt)
        except ClaudeLimitError as error:
            now = self.clock()
            if error.resets_at and error.resets_at > now:
                until = error.resets_at
            else:
                until = now + self.settings.limit_cooldown_minutes * 60
            self.state.limit_until = until
            log.warning(
                "%s. No replies until %s (send /resume to retry earlier). CLI said: %s",
                error, _format_time(until), error.detail,
            )
            if self.settings.auto_send:
                await self._alert_owner(
                    bot,
                    f"Claude subscription usage limit reached. I will stay quiet until "
                    f"{_format_time(until)}. Send /resume to retry earlier.",
                )
            return Outcome.LIMITED
        except ClaudeError as error:
            log.error("Could not draft a reply for chat %s: %s", chat_id, error)
            now = self.clock()
            if self.settings.auto_send and (
                self._last_error_alert is None or now - self._last_error_alert >= ERROR_ALERT_INTERVAL
            ):
                self._last_error_alert = now
                await self._alert_owner(bot, f"Could not draft a reply: {error}")
            return Outcome.ERROR

        reply = (reply or "").strip()
        if not reply or reply == SKIP_TOKEN:
            return Outcome.SKIPPED

        preview = Preview(connection_id, chat_id, chat_name, language, incoming, reply)
        if not self.settings.auto_send:
            self._show(preview)
            return Outcome.PREVIEWED

        blocked = self._blocked(chat_key)
        if blocked is not None:
            preview.note = f"not sent: {blocked.value} (state changed while the reply was drafted)"
            self._show(preview)
            return blocked

        # Ask Telegram again right before sending; the cache may be stale.
        try:
            fresh = await bot.get_business_connection(connection_id)
        except TelegramAPIError as error:
            preview.note = f"not sent: could not re-check the business connection ({error})"
            self._show(preview)
            return Outcome.PERMISSION_DENIED
        self.connections[connection_id] = fresh
        problem = None
        if fresh.user.id != self.settings.owner_id:
            problem = "the connection belongs to another account"
        elif not fresh.is_enabled:
            problem = "the connection is disabled"
        elif not can_reply(fresh):
            problem = "the bot has no permission to reply in Telegram Business settings"
        if problem:
            preview.note = f"not sent: {problem}"
            self._show(preview)
            return Outcome.PERMISSION_DENIED

        try:
            for start in range(0, len(reply), TELEGRAM_CHUNK):
                await bot.send_message(
                    chat_id, reply[start:start + TELEGRAM_CHUNK], business_connection_id=connection_id
                )
        except TelegramAPIError as error:
            log.error("Telegram refused to send the reply to chat %s: %s", chat_id, error)
            return Outcome.ERROR
        self.history.add(
            connection_id, chat_id, role="owner", name="You", text=reply, lang=language, ts=self.clock(),
        )
        log.info("Sent a reply to %s (%s)", chat_name, chat_id)
        return Outcome.SENT

    # ----------------------------------------------------------------- helpers

    def _blocked(self, chat_key: str) -> Optional[Outcome]:
        now = self.clock()
        if self.state.paused:
            return Outcome.PAUSED
        if self.state.limit_until is not None and self.state.limit_until > now:
            return Outcome.LIMITED
        until = self._takeover_until.get(chat_key)
        if until is not None and until > now:
            return Outcome.OWNER_ACTIVE
        return None

    def _show(self, preview: Preview) -> None:
        self.preview_sink(preview)
        record = dict(asdict(preview), ts=self.clock())
        append_private_line(self.previews_path, json.dumps(record, ensure_ascii=False))

    async def _alert_owner(self, bot: Bot, text: str) -> None:
        if self.settings.owner_id is None:
            return
        try:
            await bot.send_message(self.settings.owner_id, text)
        except TelegramAPIError as error:
            log.warning("Could not notify the owner (send /start to the bot first): %s", error)

    # ---------------------------------------------------------- owner controls

    def pause(self) -> None:
        self.state.paused = True

    def resume(self) -> None:
        self.state.paused = False
        self.state.limit_until = None
        self._takeover_until.clear()

    def _limit_active(self) -> bool:
        return self.state.limit_until is not None and self.state.limit_until > self.clock()

    def status_text(self) -> str:
        if self.settings.auto_send:
            mode = "AUTO-SEND: replies are sent from your account"
        else:
            mode = "PREVIEW: replies are only shown in the logs"
        lines = [
            f"Mode: {mode}",
            f"State: {'paused' if self.state.paused else 'running'}",
        ]
        if self._limit_active():
            lines.append(f"Claude usage limit: waiting until {_format_time(self.state.limit_until)}")
        else:
            lines.append("Claude usage limit: none")
        lines.append(f"Chats in history: {self.history.chat_count()}")
        return "\n".join(lines)

    def start_text(self) -> str:
        if self.settings.auto_send:
            mode = "AUTO-SEND — javoblar sizning nomingizdan yuboriladi"
        else:
            mode = "PREVIEW — javoblar yuborilmaydi, faqat logda ko'rinadi"
        state = "pauzada" if self.state.paused else "ishlayapti"
        minutes = self.settings.owner_takeover_minutes
        lines = [
            "Salom! Men sizning shaxsiy yordamchingizman.",
            "Telegram Business orqali sizga yozganlarga sizning uslubingizda, "
            "profile.md dagi ma'lumotlar asosida javob tayyorlayman.",
            "",
            "Hozirgi holat:",
            f"• Rejim: {mode}",
            f"• Holat: {state}",
        ]
        if self._limit_active():
            lines.append(f"• Claude limiti: {_format_time(self.state.limit_until)} gacha kutyapman")
        lines += [
            "",
            "Qanday ulanadi:",
            "1. Telegram → Settings → Telegram Business → Chatbots",
            "2. Shu botning username'ini kiriting",
            "3. Bot qaysi chatlarni ko'rishini tanlang",
            "4. Javob berish ruxsatini yoqing",
            "Buning uchun Telegram Premium kerak.",
            "",
            "Buyruqlar:",
            "/pause — javob berishni to'xtatish",
            "/resume — davom ettirish",
            "/status — holatni ko'rish",
            "",
            "Bilib qo'ying:",
        ]
        if minutes > 0:
            lines.append(f"• Chatda o'zingiz yozsangiz, bot o'sha chatda {minutes} daqiqa jim turadi.")
        lines += [
            "• Pul, qarz, uchrashuv va rejalarga rozilik bermaydi, va'da qilmaydi.",
            "• profile.md ni to'ldiring: bot faqat undagi ma'lumotlarni biladi.",
        ]
        if not profile_is_filled(read_profile(self.settings.profile_path)):
            lines.append("• profile.md hali to'ldirilmagan!")
        if not self.settings.auto_send:
            lines.append(
                "• Hozir PREVIEW rejimi. Javoblar yaxshi bo'lsa, AUTO_SEND=true qiling."
            )
        return "\n".join(lines)


def make_claude_cli(settings: Settings) -> ClaudeCLI:
    return ClaudeCLI(
        settings.claude_bin,
        workdir=settings.data_dir / "claude-run",
        model=settings.claude_model or None,
        effort=settings.claude_effort,
        timeout=settings.claude_timeout,
        oauth_token=settings.claude_oauth_token or None,
        auth_label=settings.claude_auth_mode,
    )


def build_assistant(settings: Settings, **kwargs: Any) -> Assistant:
    return Assistant(settings, make_claude_cli(settings), **kwargs)
