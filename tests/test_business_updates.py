from __future__ import annotations

import asyncio
import json
import os

from aiogram.methods import GetBusinessConnection, SendMessage

from assistant.claude_cli import ClaudeAuthError, ClaudeLimitError, ClaudeTimeout
from assistant.handlers import STRANGER_TEXT
from assistant.service import Outcome
from tests.conftest import CONNECTION_ID, OWNER_ID, STRANGER_ID, FakeGenerator


# ------------------------------------------------------------------ preview


async def test_preview_mode_shows_reply_and_sends_nothing(make_harness):
    h = make_harness(generator=FakeGenerator("Yaxshi, o'zing-chi?"))
    await h.connect()
    outcome = await h.business_message("Salom, qalesan?")

    assert outcome is Outcome.PREVIEWED
    assert h.sent() == []
    assert h.previews[0].reply == "Yaxshi, o'zing-chi?"
    assert h.previews[0].incoming == "Salom, qalesan?"
    assert h.previews[0].language == "uz"

    path = h.data_dir / "previews.jsonl"
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    record = json.loads(path.read_text().splitlines()[0])
    assert record["reply"] == "Yaxshi, o'zing-chi?"

    # The unsent reply is not part of the history.
    history = h.assistant.history.get(CONNECTION_ID, STRANGER_ID)
    assert [item["role"] for item in history] == ["other"]


async def test_known_connection_makes_no_telegram_requests_in_preview(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message("Hi")
    await h.business_message("Are you there?")
    assert h.session.requests == []


async def test_unknown_connection_is_looked_up_once_then_cached(make_harness):
    h = make_harness()
    h.set_connection()
    assert await h.business_message("Hi") is Outcome.PREVIEWED
    assert await h.business_message("Hello?") is Outcome.PREVIEWED
    assert len(h.lookups()) == 1
    assert all(isinstance(r, GetBusinessConnection) for r in h.session.requests)


async def test_failed_lookup_is_no_connection(make_harness):
    h = make_harness()
    assert await h.business_message("Hi", connection_id="missing") is Outcome.NO_CONNECTION
    assert h.generator.calls == []


async def test_foreign_and_disabled_connections_never_call_claude(make_harness):
    h = make_harness()
    await h.connect("foreign", user_id=999)
    assert await h.business_message("Hi", connection_id="foreign") is Outcome.FOREIGN_CONNECTION
    await h.connect("off", enabled=False)
    assert await h.business_message("Hi", connection_id="off") is Outcome.CONNECTION_DISABLED
    assert h.generator.calls == []
    assert h.sent() == []


async def test_any_chat_gets_a_reply_without_an_allowlist(make_harness):
    h = make_harness()
    await h.connect()
    for chat_id in (11, 22, 33333333):
        assert await h.business_message("Hi", from_id=chat_id, chat_id=chat_id) is Outcome.PREVIEWED


# ------------------------------------------------------------------ owner


async def test_owner_message_is_recorded_and_never_answered(make_harness):
    h = make_harness()
    await h.connect()
    outcome = await h.business_message("Ertaga gaplashamiz", from_id=OWNER_ID, chat_id=STRANGER_ID)
    assert outcome is Outcome.OWNER_MESSAGE
    assert h.generator.calls == []
    history = h.assistant.history.get(CONNECTION_ID, STRANGER_ID)
    assert history[-1]["role"] == "owner"
    assert history[-1]["text"] == "Ertaga gaplashamiz"


async def test_takeover_silences_the_chat_until_it_expires(make_harness):
    h = make_harness(OWNER_TAKEOVER_MINUTES="15")
    await h.connect()
    await h.business_message("Men o'zim yozaman", from_id=OWNER_ID)
    assert await h.business_message("Ok, kutaman") is Outcome.OWNER_ACTIVE
    assert h.generator.calls == []

    # Other chats are not affected.
    assert await h.business_message("Hi", from_id=5, chat_id=5) is Outcome.PREVIEWED

    h.clock.advance(15 * 60 + 1)
    assert await h.business_message("Hali ham bormisan?") is Outcome.PREVIEWED


async def test_takeover_zero_disables_it(make_harness):
    h = make_harness(OWNER_TAKEOVER_MINUTES="0")
    await h.connect()
    await h.business_message("Salom", from_id=OWNER_ID)
    assert await h.business_message("Salom!") is Outcome.PREVIEWED


# ------------------------------------------------------------------ bots, duplicates


async def test_bot_messages_are_ignored(make_harness):
    h = make_harness()
    await h.connect()
    assert await h.business_message("Auto reply", via_business_bot=True, from_id=OWNER_ID) is Outcome.IGNORED_BOT
    assert await h.business_message("I am a bot", is_bot=True) is Outcome.IGNORED_BOT
    assert h.generator.calls == []


async def test_duplicates_are_processed_once_even_in_parallel_and_after_restart(make_harness):
    h = make_harness()
    await h.connect()
    await asyncio.gather(*(h.business_message("Hi", message_id=7) for _ in range(3)))
    assert sorted(o.value for o in h.assistant.outcomes) == ["duplicate", "duplicate", "previewed"]

    assert await h.business_message("Hi", message_id=7) is Outcome.DUPLICATE
    h.restart()
    await h.connect()
    assert await h.business_message("Hi", message_id=7) is Outcome.DUPLICATE
    assert len(h.generator.calls) == 1


# ------------------------------------------------------------------ content


async def test_service_message_is_unsupported(make_harness):
    h = make_harness()
    await h.connect()
    outcome = await h.business_message(None, new_chat_title="New title")
    assert outcome is Outcome.UNSUPPORTED
    assert h.generator.calls == []


async def test_sticker_and_voice_are_described(make_harness):
    h = make_harness()
    await h.connect()
    sticker = {
        "file_id": "s1", "file_unique_id": "u1", "type": "regular", "width": 512, "height": 512,
        "is_animated": False, "is_video": False, "emoji": "😀",
    }
    await h.business_message(None, sticker=sticker)
    voice = {"file_id": "v1", "file_unique_id": "u2", "duration": 3}
    await h.business_message(None, voice=voice)
    texts = [item["text"] for item in h.assistant.history.get(CONNECTION_ID, STRANGER_ID)]
    assert texts == ["[sticker] 😀", "[voice message]"]


async def test_history_is_separate_per_connection_and_chat(make_harness):
    h = make_harness()
    await h.connect()
    await h.connect("second")
    await h.business_message("secret for chat A", chat_id=1, from_id=1)
    await h.business_message("message in chat B", chat_id=2, from_id=2)
    await h.business_message("same chat, other connection", chat_id=1, from_id=1, connection_id="second")

    prompts = [call["user"] for call in h.generator.calls]
    assert "secret for chat A" in prompts[0]
    assert "secret for chat A" not in prompts[1]
    assert "secret for chat A" not in prompts[2]
    assert len(h.assistant.history.get(CONNECTION_ID, 1)) == 1
    assert len(h.assistant.history.get("second", 1)) == 1


async def test_language_hint_and_fallback_for_emoji(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message("Привет, как дела?", chat_id=1, from_id=1)
    await h.business_message("Hi, are you free tonight?", chat_id=2, from_id=2)
    await h.business_message("Bugun bo'shmisan?", chat_id=3, from_id=3)
    await h.business_message("👍", chat_id=1, from_id=1)
    hints = [call["user"].rsplit("\n", 1)[-1] for call in h.generator.calls]
    assert hints == [
        "Their latest message is in Russian.",
        "Their latest message is in English.",
        "Their latest message is in Uzbek.",
        "Their latest message is in Russian.",
    ]


async def test_skip_is_not_shown(make_harness):
    h = make_harness(generator=FakeGenerator("[SKIP]", "  "))
    await h.connect()
    assert await h.business_message("ok") is Outcome.SKIPPED
    assert await h.business_message("👍") is Outcome.SKIPPED
    assert h.previews == []


async def test_profile_is_sent_without_comments(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message("Hi")
    system = h.generator.calls[0]["system"]
    assert "Ism: Aziz" in system
    assert "secret comment" not in system


# ------------------------------------------------------------------ auto-send


async def test_auto_send_rechecks_connection_then_sends(make_harness):
    h = make_harness(AUTO_SEND="true", generator=FakeGenerator("Hey!"))
    await h.connect()
    assert await h.business_message("Hi") is Outcome.SENT

    kinds = [type(r) for r in h.session.requests]
    assert kinds == [GetBusinessConnection, SendMessage]
    sent = h.sent()[0]
    assert sent.chat_id == STRANGER_ID
    assert sent.text == "Hey!"
    assert sent.business_connection_id == CONNECTION_ID
    history = h.assistant.history.get(CONNECTION_ID, STRANGER_ID)
    assert history[-1] == dict(history[-1], role="owner", text="Hey!")


async def test_auto_send_splits_long_replies(make_harness):
    h = make_harness(AUTO_SEND="true", generator=FakeGenerator("x" * 9000))
    await h.connect()
    assert await h.business_message("Tell me everything") is Outcome.SENT
    assert [len(m.text) for m in h.sent()] == [4000, 4000, 1000]


async def test_auto_send_refuses_without_reply_right_or_when_disabled(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    h.set_connection(can_reply=False)
    assert await h.business_message("Hi") is Outcome.PERMISSION_DENIED
    assert "permission" in h.previews[-1].note

    h.set_connection(enabled=False)
    assert await h.business_message("Hi again") is Outcome.PERMISSION_DENIED
    assert "disabled" in h.previews[-1].note
    assert h.sent() == []


async def test_auto_send_stops_when_owner_writes_during_generation(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()

    async def owner_writes():
        h.generator.hook = None
        await h.business_message("Men o'zim javob beraman", from_id=OWNER_ID)

    h.generator.hook = owner_writes
    assert await h.business_message("Salom") is Outcome.OWNER_ACTIVE
    assert h.sent() == []
    assert "owner_active" in h.previews[-1].note


# ------------------------------------------------------------------ owner commands


async def test_pause_and_resume_only_work_for_the_owner_and_survive_restart(make_harness):
    h = make_harness()
    await h.connect()

    await h.direct_message("/pause", from_id=STRANGER_ID)
    assert h.assistant.state.paused is False
    assert h.sent() == []

    await h.direct_message("/pause")
    assert h.assistant.state.paused is True
    assert await h.business_message("Hi") is Outcome.PAUSED

    h.restart()
    await h.connect()
    assert await h.business_message("Hello?") is Outcome.PAUSED

    await h.direct_message("/resume", from_id=STRANGER_ID)
    assert h.assistant.state.paused is True
    await h.direct_message("/resume")
    assert h.assistant.state.paused is False
    assert await h.business_message("Now?") is Outcome.PREVIEWED


async def test_strangers_direct_messages_get_nothing(make_harness):
    h = make_harness()
    await h.direct_message("/status", from_id=STRANGER_ID)
    await h.direct_message("hello bot", from_id=STRANGER_ID)
    assert h.sent() == []


async def test_status_for_owner(make_harness):
    h = make_harness()
    await h.direct_message("/status")
    assert "PREVIEW" in h.sent()[-1].text


async def test_start_for_owner_is_an_uzbek_guide(make_harness):
    h = make_harness()
    await h.direct_message("/start")
    text = h.sent()[-1].text
    assert "PREVIEW" in text
    assert "ishlayapti" in text
    assert "Chatbots" in text
    assert "/pause" in text and "/resume" in text and "/status" in text
    assert "AUTO_SEND=true" in text


async def test_start_shows_auto_send_and_pause(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.direct_message("/pause")
    await h.direct_message("/start")
    text = h.sent()[-1].text
    assert "AUTO-SEND" in text
    assert "pauzada" in text
    assert "AUTO_SEND=true qiling" not in text


async def test_start_for_stranger_is_a_short_note(make_harness):
    h = make_harness()
    await h.direct_message("/start", from_id=STRANGER_ID)
    sent = h.sent()
    assert len(sent) == 1
    assert sent[0].chat_id == STRANGER_ID
    assert sent[0].text == STRANGER_TEXT
    assert "Aziz" not in sent[0].text
    assert h.generator.calls == []


# ------------------------------------------------------------------ limits and errors


async def test_limit_waits_until_reset_then_continues(make_harness):
    h = make_harness()
    reset = h.clock() + 3600
    h.generator.queue(ClaudeLimitError("limit", resets_at=reset), "Back!")
    await h.connect()
    assert await h.business_message("Hi") is Outcome.LIMITED
    assert await h.business_message("Hi?") is Outcome.LIMITED
    assert len(h.generator.calls) == 1

    h.restart()
    await h.connect()
    assert await h.business_message("Still there?") is Outcome.LIMITED

    h.clock.advance(3601)
    assert await h.business_message("Now?") is Outcome.PREVIEWED
    assert len(h.generator.calls) == 2


async def test_limit_without_reset_uses_cooldown(make_harness):
    h = make_harness(LIMIT_COOLDOWN_MINUTES="30")
    h.generator.queue(ClaudeLimitError("limit"))
    await h.connect()
    assert await h.business_message("Hi") is Outcome.LIMITED
    assert h.assistant.state.limit_until == h.clock() + 30 * 60
    h.clock.advance(29 * 60)
    assert await h.business_message("Hi?") is Outcome.LIMITED
    h.clock.advance(61)
    assert await h.business_message("Hi!?") is Outcome.PREVIEWED


async def test_limit_notifies_owner_in_auto_send_and_resume_clears_it(make_harness):
    h = make_harness(AUTO_SEND="true")
    h.generator.queue(ClaudeLimitError("limit"))
    await h.connect()
    assert await h.business_message("Hi") is Outcome.LIMITED
    alerts = [m for m in h.sent() if m.chat_id == OWNER_ID]
    assert len(alerts) == 1 and "limit" in alerts[0].text
    assert all(m.chat_id == OWNER_ID for m in h.sent())

    await h.direct_message("/resume")
    assert h.assistant.state.limit_until is None
    assert await h.business_message("Hi again") is Outcome.SENT


async def test_limit_in_preview_mode_does_not_message_owner(make_harness):
    h = make_harness()
    h.generator.queue(ClaudeLimitError("limit"))
    await h.connect()
    assert await h.business_message("Hi") is Outcome.LIMITED
    assert h.sent() == []


async def test_timeout_and_auth_errors_send_nothing_to_chat_and_alert_once(make_harness):
    h = make_harness(AUTO_SEND="true")
    h.generator.queue(ClaudeTimeout("slow"), ClaudeAuthError("login", "detail"), ClaudeTimeout("slow"))
    await h.connect()
    for text in ("one", "two", "three"):
        assert await h.business_message(text) is Outcome.ERROR
    assert all(m.chat_id == OWNER_ID for m in h.sent())
    assert len(h.sent()) == 1


async def test_errors_in_preview_mode_send_nothing(make_harness):
    h = make_harness()
    h.generator.queue(ClaudeTimeout("slow"))
    await h.connect()
    assert await h.business_message("one") is Outcome.ERROR
    assert h.sent() == []
