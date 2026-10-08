"""Offline test harness: a real Dispatcher fed with Update payloads."""

from __future__ import annotations

import inspect
import itertools
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetBusinessConnection, SendMessage
from aiogram.types import BusinessConnection, Chat, Message, Update

from assistant.config import load_settings
from assistant.handlers import build_router
from assistant.service import Assistant, Outcome, Preview

OWNER_ID = 1001
STRANGER_ID = 2002
CONNECTION_ID = "conn-owner"
START_TS = 1_700_000_000.0

PROFILE = """<!-- secret comment -->
# Men haqimda
- Ism: Aziz
- Shahar: Toshkent
"""


class RecordingSession(BaseSession):
    """Records every Bot API request instead of going to the network."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: List[Any] = []
        self.connections: Dict[str, Dict[str, Any]] = {}
        self._message_ids = itertools.count(50_000)

    async def close(self) -> None:
        pass

    async def make_request(self, bot: Bot, method: Any, timeout: Optional[int] = None) -> Any:
        self.requests.append(method)
        if isinstance(method, GetBusinessConnection):
            payload = self.connections.get(method.business_connection_id)
            if payload is None:
                raise TelegramBadRequest(method=method, message="Bad Request: business connection not found")
            return BusinessConnection.model_validate(payload)
        if isinstance(method, SendMessage):
            return Message(
                message_id=next(self._message_ids),
                date=int(START_TS),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
                business_connection_id=method.business_connection_id,
            )
        return True

    async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise AssertionError("tests must not download anything")
        yield b""


class FakeGenerator:
    """Returns queued replies (or raises queued exceptions) and records calls."""

    def __init__(self, *items: Any) -> None:
        self.items = list(items)
        self.calls: List[Dict[str, str]] = []
        self.hook: Optional[Callable[[], Any]] = None

    def queue(self, *items: Any) -> None:
        self.items.extend(items)

    async def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append({"system": system_prompt, "user": user_prompt})
        if self.hook is not None:
            result = self.hook()
            if inspect.isawaitable(result):
                await result
        item = self.items.pop(0) if self.items else "Default reply"
        if isinstance(item, BaseException):
            raise item
        return item


class Clock:
    def __init__(self, now: float = START_TS) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def connection_payload(
    connection_id: str = CONNECTION_ID,
    *,
    user_id: int = OWNER_ID,
    enabled: bool = True,
    can_reply: bool = True,
) -> Dict[str, Any]:
    # Both the new `rights.can_reply` and the legacy `can_reply` field.
    return {
        "id": connection_id,
        "user": {"id": user_id, "is_bot": False, "first_name": "Owner"},
        "user_chat_id": user_id,
        "date": int(START_TS),
        "is_enabled": enabled,
        "can_reply": can_reply,
        "rights": {"can_reply": can_reply},
    }


class Harness:
    def __init__(self, tmp_path: Path, *, generator: Optional[FakeGenerator] = None, **overrides: str):
        self.tmp_path = tmp_path
        self.data_dir = tmp_path / "data"
        self.profile_path = tmp_path / "profile.md"
        if not self.profile_path.exists():
            self.profile_path.write_text(PROFILE, encoding="utf-8")
        values = {
            "TELEGRAM_BOT_TOKEN": "123456:TEST-token",
            "OWNER_ID": str(OWNER_ID),
            "AUTO_SEND": "false",
            "CLAUDE_BIN": "/nonexistent/claude",
            "DATA_DIR": str(self.data_dir),
            "PROFILE_PATH": str(self.profile_path),
        }
        values.update(overrides)
        self.settings = load_settings(tmp_path / ".env", overrides=values, source={})
        self.generator = generator or FakeGenerator()
        self.clock = Clock()
        self.session = RecordingSession()
        self.bot = Bot("123456:TEST-token", session=self.session)
        self.previews: List[Preview] = []
        self._update_ids = itertools.count(1)
        self._build()

    def _build(self) -> None:
        self.assistant = Assistant(
            self.settings, self.generator, clock=self.clock, preview_sink=self.previews.append
        )
        self.dp = Dispatcher()
        self.dp.include_router(build_router(self.assistant))

    def restart(self) -> None:
        """A new Assistant over the same data directory (like a process restart)."""
        self._build()

    # ------------------------------------------------------------- feeding

    async def feed(self, payload: Dict[str, Any]) -> None:
        payload = dict(payload, update_id=next(self._update_ids))
        update = Update.model_validate(payload, context={"bot": self.bot})
        await self.dp.feed_update(self.bot, update)

    def set_connection(self, connection_id: str = CONNECTION_ID, **kwargs: Any) -> None:
        """Change what Telegram answers to getBusinessConnection (no update)."""
        self.session.connections[connection_id] = connection_payload(connection_id, **kwargs)

    async def connect(self, connection_id: str = CONNECTION_ID, **kwargs: Any) -> None:
        self.set_connection(connection_id, **kwargs)
        await self.feed({"business_connection": connection_payload(connection_id, **kwargs)})

    async def business_message(
        self,
        text: Optional[str] = "Salom, qalesan?",
        *,
        from_id: int = STRANGER_ID,
        chat_id: int = STRANGER_ID,
        message_id: Optional[int] = None,
        connection_id: str = CONNECTION_ID,
        is_bot: bool = False,
        via_business_bot: bool = False,
        name: str = "Ali",
        **extra: Any,
    ) -> Outcome:
        message: Dict[str, Any] = {
            "message_id": message_id if message_id is not None else next(self._update_ids) + 10_000,
            "date": int(self.clock()),
            "chat": {"id": chat_id, "type": "private", "first_name": name},
            "from": {"id": from_id, "is_bot": is_bot, "first_name": name},
            "business_connection_id": connection_id,
        }
        if text is not None:
            message["text"] = text
        if via_business_bot:
            message["sender_business_bot"] = {"id": 777, "is_bot": True, "first_name": "Helper", "username": "helper_bot"}
        message.update(extra)
        await self.feed({"business_message": message})
        return self.assistant.outcomes[-1]

    async def direct_message(self, text: str, *, from_id: int = OWNER_ID) -> None:
        message: Dict[str, Any] = {
            "message_id": next(self._update_ids) + 20_000,
            "date": int(self.clock()),
            "chat": {"id": from_id, "type": "private", "first_name": "User"},
            "from": {"id": from_id, "is_bot": False, "first_name": "User"},
            "text": text,
        }
        if text.startswith("/"):
            command = text.split()[0]
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(command)}]
        await self.feed({"message": message})

    # ------------------------------------------------------------- inspecting

    def sent(self) -> List[SendMessage]:
        return [r for r in self.session.requests if isinstance(r, SendMessage)]

    def lookups(self) -> List[GetBusinessConnection]:
        return [r for r in self.session.requests if isinstance(r, GetBusinessConnection)]


@pytest.fixture
def make_harness(tmp_path: Path) -> Callable[..., Harness]:
    def factory(**kwargs: Any) -> Harness:
        return Harness(tmp_path, **kwargs)

    return factory
