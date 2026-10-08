# Telegram Business assistant (Claude Code CLI)

A personal assistant for your own Telegram account. It connects through
**Telegram Business → Chatbots**; when someone writes to you, it drafts a reply
in your name and in your style, using what `profile.md` says about you.

Replies are written by the **Claude Code CLI** (`claude -p`) installed where the
bot runs, so your **Claude subscription** is used. An Anthropic API key is never
used.

## What it does

- **Preview mode is the default** (`AUTO_SEND=false`): drafted replies are only
  printed in the log (`PREVIEW, not sent`) and saved to `data/previews.jsonl`.
  Nothing is sent until you set `AUTO_SEND=true`.
- Which chats the bot sees is decided **only by Telegram**: *Settings → Telegram
  Business → Chatbots*, where you pick the chats. There is no allowlist in the code.
- History is kept per business connection and chat, so chats never mix.
- Replies follow the language of the other person's latest message (Uzbek,
  Russian, English; Uzbek in the same script, Latin or Cyrillic).

## Behaviour

| Situation | What happens |
|---|---|
| Business connection of another account (not `OWNER_ID`) | Ignored, Claude is not called |
| Which chats | Whatever you selected in *Telegram Business → Chatbots* |
| You write in a chat yourself | Saved to history, never answered; the bot stays quiet in that chat for `OWNER_TAKEOVER_MINUTES` |
| Messages from bots (incl. this bot's own replies) | Ignored |
| The same message delivered twice (also after a restart) | Processed once |
| Service messages (title changes, pins, ...) | Ignored; media is described as `[photo]`, `[voice message]`, `[sticker] 😀`, ... |
| `/pause` | No replies at all until `/resume` (survives restarts) |
| Subscription usage limit | No Claude calls until the reset time (or `LIMIT_COOLDOWN_MINUTES`), survives restarts; in auto-send mode you get one message |
| Timeout or login error | Nothing is sent to the chat; in auto-send mode you get at most one alert per 10 minutes |
| Claude answers `[SKIP]` | Nothing is shown or sent |
| Before an auto-send | Blocks are checked again (you may have written meanwhile) and the connection is re-fetched from Telegram: owner, enabled and the reply right |

Owner commands (only you, in a private chat with the bot):

- `/start` — a guide in Uzbek: mode, state, how to connect, commands. Anyone
  else gets a short Uzbek note that this is a private assistant; Claude is not
  called and nothing about you is revealed.
- `/pause`, `/resume`, `/status`.

Other direct messages to the bot are ignored silently.

## How Claude is called

Each reply is one run of:

```
claude -p --safe-mode --settings '{"disableAllHooks":true,"enabledPlugins":{...:false}}' \
  --tools "" --strict-mcp-config --setting-sources "" --disable-slash-commands \
  --no-session-persistence --system-prompt-file <0600 temp file> \
  --output-format json --effort low [--model ...]
```

- No tools, MCP servers, plugins, skills, hooks, CLAUDE.md or memory: Claude can
  only write text.
- The conversation goes in through **stdin**, the system prompt (with your
  profile) through a 0600 temp file that is deleted afterwards, so neither shows
  up in `ps`. No shell is used. The process runs in `data/claude-run` (0700) in
  its own process group; on timeout the whole group is killed.
- **`--bare` is not used**: it ignores OAuth logins, so the subscription would
  not work.
- The child environment drops every `ANTHROPIC_*` variable, the bot token,
  `CLAUDE_CODE_USE_BEDROCK/VERTEX/FOUNDRY`, `CLAUDE_CODE_SIMPLE` and the
  variables of a parent Claude Code session.
- Login: `CLAUDE_CODE_OAUTH_TOKEN` (a `claude setup-token` token, `sk-ant-oat...`)
  is given only to the `claude` child process. API keys (`sk-ant-api...`) are
  rejected. The token is redacted from any CLI output. If the token is empty, the
  machine's own `claude` login is used (local testing only).
- `python bot.py --check-claude` proves all of this with a tiny request: it
  fails unless tools, MCP servers, plugins and skills are empty, no API key is
  the key source and the provider is first-party.

## Deploy to Railway

1. On a computer with a browser run `claude setup-token`, approve with your
   subscription account, copy the `sk-ant-oat...` token (valid for 1 year).
2. In @BotFather: `/newbot`, then *Bot Settings → Business Mode → Turn on*.
   Get your numeric user id from @userinfobot.
3. Create a **private** GitHub repository and push this code (`profile.md` is
   built into the image, so the repository must stay private).
4. Railway → *New Project* → *GitHub Repository* → pick the repository.
5. Service → *Variables* → *Raw Editor*:
   ```
   TELEGRAM_BOT_TOKEN=...
   OWNER_ID=...
   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat...
   AUTO_SEND=false
   ```
   Optional: `CLAUDE_MODEL`, `CLAUDE_EFFORT`, `CLAUDE_TIMEOUT_SECONDS`,
   `LIMIT_COOLDOWN_MINUTES`, `OWNER_TAKEOVER_MINUTES`, `HISTORY_LIMIT`.
   `CLAUDE_BIN`, `DATA_DIR` and `PROFILE_PATH` are not needed.
6. Volume: right-click the canvas (or Cmd+K) → *Volume* → select the service →
   mount path exactly **`/app/data`** → *Deploy*. Without a volume, history,
   pause and limit state are lost on every deploy.
7. Logs should show `Bot @... is running`, `Mode: PREVIEW` and
   `Claude auth: subscription token from environment variables`. Red lines in
   Railway only mean stderr, not errors.
8. Send `/start` to the bot. Then in Telegram: *Settings → Telegram Business →
   Chatbots* (Telegram Premium required) → the bot's username → choose chats →
   allow replying. The log shows `Business connection ...: user=<id> enabled=True`.
9. When someone writes to you, the log shows a `PREVIEW, not sent` block. Once
   the drafts look right, set `AUTO_SEND=true`.

Notes:

- Run the bot in **one place only**: two pollers with the same bot token fight
  each other. Do not run `python bot.py` locally while Railway is running.
- Every push redeploys. Changing `profile.md` also means commit + push.
- Token expired or leaked: run `claude setup-token` again and update the Railway
  variable. Bot token leaked: `/revoke` in @BotFather.

## Local development

```
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env && chmod 600 .env    # then edit .env
.venv/bin/python bot.py --check-claude
.venv/bin/python bot.py --try "Salom, qalesan?"
```

Locally only `.env` is read (shell variables are ignored). If
`CLAUDE_CODE_OAUTH_TOKEN` is empty, this machine's `claude` login is used.

## Commands

| Command | Purpose | Exit codes |
|---|---|---|
| `python bot.py` | Run the bot with `.env` | 0, 2 = bad config or bot token rejected |
| `python bot.py --settings-from-env` | Container mode (Railway) | same |
| `python bot.py --check-claude` | Check Claude isolation and login, no Telegram | 0 ok, 1 failed, 2 bad config |
| `python bot.py --try "text"` | Draft a reply to a made-up message, no Telegram | 0 ok, 1 Claude error, 2 bad config |

`--check-claude` and `--try` also work with `--settings-from-env`.

## Tests

```
.venv/bin/python -m pytest
```

All tests run offline: Telegram is replaced by a recording session and `claude`
by a fake script.

## Files

```
bot.py                     entry point
assistant/config.py        settings (.env or environment variables)
assistant/claude_cli.py    isolated `claude -p` runs, error classification
assistant/language.py      uz / ru / en hint
assistant/prompts.py       system and user prompts
assistant/store.py         history, seen messages, pause/limit state
assistant/service.py       the decision logic
assistant/handlers.py      aiogram router
profile.md                 about you (template)
Dockerfile, railway.json   Railway deployment
tests/                     offline tests
```
