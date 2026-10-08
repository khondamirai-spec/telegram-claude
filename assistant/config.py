"""Settings loading.

Settings come from exactly one source and are never mixed:

* local runs read only the ``.env`` file (via ``dotenv_values``; the shell
  environment is ignored on purpose);
* container runs (Railway) pass the process environment in as ``source``
  (see ``bot.py --settings-from-env``) and the ``.env`` file is not read.

Nothing is ever written to ``os.environ``, so tokens cannot leak into other
processes through it.
"""

from __future__ import annotations

import re
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Mapping, Optional, Union

from dotenv import dotenv_values

PROJECT_DIR = Path(__file__).resolve().parent.parent

SETTING_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "OWNER_ID",
    "AUTO_SEND",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_BIN",
    "CLAUDE_MODEL",
    "CLAUDE_EFFORT",
    "CLAUDE_TIMEOUT_SECONDS",
    "LIMIT_COOLDOWN_MINUTES",
    "OWNER_TAKEOVER_MINUTES",
    "HISTORY_LIMIT",
    "DATA_DIR",
    "PROFILE_PATH",
)

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

AUTH_MACHINE_LOGIN = "this machine's claude login"
AUTH_TOKEN_DOTENV = "subscription token from .env"
AUTH_TOKEN_ENVIRONMENT = "subscription token from environment variables"

_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}
_BOT_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]+$")


class ConfigError(Exception):
    """Invalid or missing settings. Messages never contain secret values."""


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(repr=False)
    owner_id: Optional[int]
    auto_send: bool
    claude_oauth_token: str = field(repr=False)
    claude_bin: str
    claude_model: str
    claude_effort: str
    claude_timeout: float
    limit_cooldown_minutes: int
    owner_takeover_minutes: int
    history_limit: int
    data_dir: Path
    profile_path: Path
    from_environment: bool = False

    @property
    def claude_auth_mode(self) -> str:
        if not self.claude_oauth_token:
            return AUTH_MACHINE_LOGIN
        return AUTH_TOKEN_ENVIRONMENT if self.from_environment else AUTH_TOKEN_DOTENV


def _default_claude_bin() -> str:
    found = shutil.which("claude")
    if found:
        return found
    return str(Path.home() / ".local" / "bin" / "claude")


def load_settings(
    env_file: Union[Path, str],
    *,
    require_telegram: bool = True,
    overrides: Optional[Mapping[str, str]] = None,
    source: Optional[Mapping[str, str]] = None,
) -> Settings:
    """Load settings from ``env_file`` or, when given, from ``source``.

    ``env_file`` is also the base for relative paths. ``overrides`` is for tests.
    """
    env_file = Path(env_file)
    base_dir = env_file.resolve().parent

    if source is None:
        where = str(env_file)
        if not env_file.is_file():
            raise ConfigError(
                f"{env_file} not found. Create it with: "
                "cp .env.example .env && chmod 600 .env"
            )
        raw: Dict[str, Optional[str]] = dict(dotenv_values(env_file))
    else:
        where = "the environment variables"
        raw = dict(source)
    if overrides:
        raw.update(overrides)
    values = {key: (value or "").strip() for key, value in raw.items()}

    def get(key: str) -> str:
        return values.get(key, "")

    def integer(key: str, default: int, minimum: int) -> int:
        text = get(key)
        if not text:
            return default
        try:
            number = int(text)
        except ValueError:
            raise ConfigError(f"{key} in {where} must be a whole number") from None
        if number < minimum:
            raise ConfigError(f"{key} in {where} must be at least {minimum}")
        return number

    def path(key: str, default: str) -> Path:
        result = Path(get(key) or default).expanduser()
        return result if result.is_absolute() else base_dir / result

    bot_token = get("TELEGRAM_BOT_TOKEN")
    owner_text = get("OWNER_ID")
    owner_id: Optional[int] = None
    if require_telegram:
        if not bot_token:
            raise ConfigError(f"TELEGRAM_BOT_TOKEN is empty in {where} (get it from @BotFather)")
        if not owner_text:
            raise ConfigError(f"OWNER_ID is empty in {where} (your numeric Telegram ID, see @userinfobot)")
    if bot_token and not _BOT_TOKEN_RE.match(bot_token):
        raise ConfigError(
            f"TELEGRAM_BOT_TOKEN in {where} does not look like a bot token "
            "(expected <digits>:<text> from @BotFather)"
        )
    if owner_text:
        if not owner_text.isdigit():
            raise ConfigError(f"OWNER_ID in {where} must contain digits only")
        owner_id = int(owner_text)

    auto_text = get("AUTO_SEND").lower() or "false"
    if auto_text in _TRUE:
        auto_send = True
    elif auto_text in _FALSE:
        auto_send = False
    else:
        raise ConfigError(f"AUTO_SEND in {where} must be true or false")

    oauth_token = get("CLAUDE_CODE_OAUTH_TOKEN")
    if oauth_token:
        if oauth_token.startswith("sk-ant-api"):
            raise ConfigError(
                f"CLAUDE_CODE_OAUTH_TOKEN in {where} is an Anthropic API key, not a "
                "subscription token. This bot only uses your Claude subscription: "
                "run `claude setup-token` and use the sk-ant-oat... token it prints"
            )
        if re.search(r"\s", oauth_token):
            raise ConfigError(
                f"CLAUDE_CODE_OAUTH_TOKEN in {where} contains whitespace; "
                "the token must be on a single line"
            )
        if not oauth_token.startswith("sk-ant-oat"):
            raise ConfigError(
                f"CLAUDE_CODE_OAUTH_TOKEN in {where} is not a `claude setup-token` "
                "token (it should start with sk-ant-oat)"
            )

    effort = get("CLAUDE_EFFORT").lower() or "low"
    if effort not in EFFORT_LEVELS:
        raise ConfigError(f"CLAUDE_EFFORT in {where} must be one of: {', '.join(EFFORT_LEVELS)}")

    claude_bin = get("CLAUDE_BIN")
    claude_bin = str(Path(claude_bin).expanduser()) if claude_bin else _default_claude_bin()

    return Settings(
        bot_token=bot_token,
        owner_id=owner_id,
        auto_send=auto_send,
        claude_oauth_token=oauth_token,
        claude_bin=claude_bin,
        claude_model=get("CLAUDE_MODEL"),
        claude_effort=effort,
        claude_timeout=float(integer("CLAUDE_TIMEOUT_SECONDS", 120, 1)),
        limit_cooldown_minutes=integer("LIMIT_COOLDOWN_MINUTES", 30, 1),
        owner_takeover_minutes=integer("OWNER_TAKEOVER_MINUTES", 15, 0),
        history_limit=integer("HISTORY_LIMIT", 30, 1),
        data_dir=path("DATA_DIR", "data"),
        profile_path=path("PROFILE_PATH", "profile.md"),
        from_environment=source is not None,
    )


def env_file_warning(path: Union[Path, str]) -> Optional[str]:
    """Return a warning when the .env file is readable by group or others."""
    try:
        mode = Path(path).stat().st_mode
    except OSError:
        return None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return (
            f"{path} is readable by other users (mode {oct(mode & 0o777)}). "
            f"It holds secret tokens: run chmod 600 {path}"
        )
    return None
