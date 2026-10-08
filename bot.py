"""Entry point.

    python bot.py                        run the bot with settings from .env
    python bot.py --settings-from-env    run in a container (Railway): settings
                                         come from environment variables
    python bot.py --check-claude         check the Claude CLI isolation and login
    python bot.py --try "message"        draft a reply to a made-up message

Exit codes: 0 ok, 1 Claude check or draft failed, 2 configuration error or the
bot token was rejected by Telegram.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional

from assistant.claude_cli import ClaudeError
from assistant.config import (
    PROJECT_DIR,
    SETTING_KEYS,
    ConfigError,
    Settings,
    env_file_warning,
    load_settings,
)
from assistant.language import detect_language
from assistant.prompts import build_system_prompt, build_user_prompt
from assistant.service import build_assistant, make_claude_cli, profile_is_filled, read_profile
from assistant.store import ensure_private_dir

log = logging.getLogger("bot")

FORBIDDEN_KEY_SOURCES = {"ANTHROPIC_API_KEY", "apiKeyHelper", "/login managed key"}
ALLOWED_PROVIDERS = {"firstParty", None}


def settings_from_process_env() -> Dict[str, str]:
    """Read the bot settings from os.environ and remove them from it.

    Removing them (even on error) keeps the bot token out of the environment
    that the ``claude`` child process inherits.
    """
    try:
        return {key: os.environ[key] for key in SETTING_KEYS if key in os.environ}
    finally:
        for key in SETTING_KEYS:
            os.environ.pop(key, None)


async def check_claude(settings: Settings) -> int:
    cli = make_claude_cli(settings)
    try:
        info = await cli.inspect()
    except ClaudeError as error:
        print(json.dumps({"auth": settings.claude_auth_mode, "error": str(error)}, indent=2, ensure_ascii=False))
        return 1
    print(json.dumps(info, indent=2, ensure_ascii=False))

    problems: List[str] = []
    init = info.get("init")
    if not init:
        problems.append("the CLI sent no init event")
    else:
        for key in ("tools", "mcp_servers", "plugins", "skills"):
            if init.get(key):
                problems.append(f"{key} should be empty but is {init.get(key)!r}")
        if init.get("apiKeySource") in FORBIDDEN_KEY_SOURCES:
            problems.append(f"apiKeySource is {init.get('apiKeySource')!r}: an API key is in use, not the subscription")
        if init.get("apiProvider") not in ALLOWED_PROVIDERS:
            problems.append(f"apiProvider is {init.get('apiProvider')!r}, expected firstParty")
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        return 1
    print(f"OK: Claude CLI is isolated and answers using {info['auth']}.")
    return 0


async def try_reply(settings: Settings, text: str) -> int:
    cli = make_claude_cli(settings)
    language = detect_language(text)
    system_prompt = build_system_prompt(read_profile(settings.profile_path), datetime.now())
    user_prompt = build_user_prompt([{"role": "other", "name": "Friend", "text": text}], language)
    try:
        reply = await cli.generate(system_prompt, user_prompt)
    except ClaudeError as error:
        print(f"Claude error: {error}", file=sys.stderr)
        return 1
    print(f"Language: {language or 'unknown'}")
    print(f"Message:  {text}")
    print(f"Reply:    {reply}")
    return 0


async def run_bot(settings: Settings) -> int:
    from aiogram import Bot, Dispatcher
    from aiogram.exceptions import TelegramUnauthorizedError

    from assistant.handlers import ALLOWED_UPDATES, build_router

    bot = Bot(settings.bot_token)
    try:
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            log.error("Telegram rejected TELEGRAM_BOT_TOKEN. Check it with @BotFather.")
            return 2
        assistant = build_assistant(settings)
        dispatcher = Dispatcher()
        dispatcher.include_router(build_router(assistant))

        log.info("Bot @%s is running", me.username)
        if settings.auto_send:
            log.warning("Mode: AUTO-SEND - replies are sent from your account")
        else:
            log.info("Mode: PREVIEW - replies are only shown in the logs")
        log.info("Claude auth: %s", settings.claude_auth_mode)
        log.info("Claude binary: %s", settings.claude_bin)
        if assistant.state.paused:
            log.warning("The bot starts PAUSED. Send /resume to the bot to continue.")
        if not profile_is_filled(read_profile(settings.profile_path)):
            log.warning("%s is not filled in yet; replies will be vague", settings.profile_path)

        await dispatcher.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
    finally:
        await bot.session.close()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Telegram Business assistant powered by the Claude Code CLI")
    parser.add_argument("--settings-from-env", action="store_true",
                        help="read settings from environment variables (container / Railway)")
    parser.add_argument("--check-claude", action="store_true",
                        help="check Claude CLI isolation and login without Telegram")
    parser.add_argument("--try", dest="try_text", metavar="TEXT",
                        help="draft a reply to a made-up message without Telegram")
    parser.add_argument("--env-file", default=str(PROJECT_DIR / ".env"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    offline = args.check_claude or args.try_text is not None
    source = settings_from_process_env() if args.settings_from_env else None
    try:
        settings = load_settings(args.env_file, require_telegram=not offline, source=source)
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2

    if source is None:
        warning = env_file_warning(args.env_file)
        if warning:
            log.warning(warning)
    ensure_private_dir(settings.data_dir)
    ensure_private_dir(settings.data_dir / "claude-run")

    if args.check_claude:
        return asyncio.run(check_claude(settings))
    if args.try_text is not None:
        return asyncio.run(try_reply(settings, args.try_text))
    try:
        return asyncio.run(run_bot(settings))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
