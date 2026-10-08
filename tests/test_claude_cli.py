from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from assistant.claude_cli import (
    REDACTED,
    ClaudeAuthError,
    ClaudeCLI,
    ClaudeError,
    ClaudeLimitError,
    ClaudeTimeout,
    classify_failure,
    parse_result,
)

TOKEN = "sk-ant-oat01-" + "A1b2C3d4" * 8

FAKE_CLAUDE = """#!{python}
import json, os, subprocess, sys, time

mode = os.environ.get("FAKE_MODE", "ok")
argv = sys.argv[1:]
stdin = sys.stdin.read()
prompt = prompt_mode = None
if "--system-prompt-file" in argv:
    path = argv[argv.index("--system-prompt-file") + 1]
    with open(path) as handle:
        prompt = handle.read()
    prompt_mode = oct(os.stat(path).st_mode & 0o777)
record = {{
    "argv": argv, "stdin": stdin, "env": dict(os.environ), "cwd": os.getcwd(),
    "cwd_mode": oct(os.stat(".").st_mode & 0o777), "listing": sorted(os.listdir(".")),
    "pid": os.getpid(), "prompt": prompt, "prompt_mode": prompt_mode,
}}

def result(text, is_error=False, **extra):
    print(json.dumps(dict({{"type": "result", "subtype": "success", "is_error": is_error, "result": text}}, **extra)))

if mode == "sleep":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    record["child_pid"] = child.pid

with open(os.environ["FAKE_RECORD"], "w") as handle:
    json.dump(record, handle)

if mode == "ok":
    result("  Hello there  \\n")
elif mode == "stream":
    print(json.dumps({{"type": "system", "subtype": "init", "tools": [], "mcp_servers": [], "plugins": [],
                      "skills": [], "apiKeySource": "none", "apiProvider": "firstParty", "model": "test-model"}}))
    result("OK")
elif mode == "sleep":
    time.sleep(60)
elif mode == "limit_epoch":
    result("Claude AI usage limit reached|1760000000", is_error=True)
elif mode == "limit_text":
    result("You've hit your limit · resets 3pm (Asia/Tashkent)", is_error=True)
elif mode == "auth":
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "none")
    sys.stderr.write("auth failed for " + token + "\\n")
    result("Invalid API key · token " + token + " · Please run /login", is_error=True, api_error_status=401)
elif mode == "crash":
    sys.stderr.write("Something exploded\\n")
    sys.exit(3)
elif mode == "empty":
    result("")
elif mode == "echo_token":
    result("token=" + os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""))
"""


@pytest.fixture
def fake(tmp_path, monkeypatch):
    script = tmp_path / "fake-claude"
    script.write_text(FAKE_CLAUDE.format(python=sys.executable))
    script.chmod(0o755)
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_RECORD", str(record))
    monkeypatch.setenv("FAKE_MODE", "ok")

    class Fake:
        workdir = tmp_path / "data" / "claude-run"

        def cli(self, **kwargs):
            kwargs.setdefault("workdir", self.workdir)
            return ClaudeCLI(str(script), **kwargs)

        def mode(self, name):
            monkeypatch.setenv("FAKE_MODE", name)

        def record(self):
            return json.loads(record.read_text())

    return Fake()


async def test_reply_is_stripped(fake):
    assert await fake.cli().generate("system", "hi") == "Hello there"


async def test_message_goes_through_stdin_and_profile_through_a_private_file(fake):
    await fake.cli().generate("PROFILE: secret profile", "MESSAGE: hello")
    rec = fake.record()
    assert rec["stdin"] == "MESSAGE: hello"
    assert not any("hello" in arg or "secret profile" in arg for arg in rec["argv"])
    assert rec["prompt"] == "PROFILE: secret profile"
    assert rec["prompt_mode"] == "0o600"
    assert os.listdir(fake.workdir) == []


async def test_temp_file_is_removed_on_error(fake):
    fake.mode("crash")
    with pytest.raises(ClaudeError):
        await fake.cli().generate("system", "hi")
    assert os.listdir(fake.workdir) == []


async def test_no_shell_is_involved(fake, tmp_path):
    marker = tmp_path / "pwned"
    await fake.cli().generate(f"$(touch {marker})", f"`touch {marker}`; $(touch {marker})")
    assert not marker.exists()


async def test_isolation_flags(fake):
    await fake.cli(model="sonnet", effort="medium").generate("system", "hi")
    argv = fake.record()["argv"]
    prompt_file = argv[argv.index("--system-prompt-file") + 1]
    assert argv[:argv.index("--settings") + 1] == ["-p", "--safe-mode", "--settings"]
    settings = json.loads(argv[3])
    assert argv[4:] == [
        "--tools", "",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--system-prompt-file", prompt_file,
        "--output-format", "json",
        "--effort", "medium",
        "--model", "sonnet",
    ]
    assert "--mcp-config" not in argv and "--bare" not in argv
    assert settings["disableAllHooks"] is True
    assert settings["enabledPlugins"] and not any(settings["enabledPlugins"].values())


async def test_child_environment_is_cleaned(fake, monkeypatch):
    for key, value in {
        "ANTHROPIC_API_KEY": "sk-ant-api-xxx",
        "ANTHROPIC_BASE_URL": "https://example.invalid",
        "TELEGRAM_BOT_TOKEN": "1:abc",
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-parent",
        "CLAUDE_CODE_SIMPLE": "1",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "CLAUDE_CODE_USE_FOUNDRY": "1",
        "CLAUDECODE": "1",
        "CLAUDE_CODE_ENTRYPOINT": "cli",
    }.items():
        monkeypatch.setenv(key, value)
    await fake.cli().generate("system", "hi")
    env = fake.record()["env"]
    assert not [key for key in env if key.startswith("ANTHROPIC_")]
    for key in ("TELEGRAM_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_SIMPLE",
                "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        assert key not in env
    assert env["HOME"] == os.environ["HOME"]
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"


async def test_configured_token_reaches_only_the_child_env(fake):
    cli = fake.cli(oauth_token=TOKEN)
    await cli.generate("system", "hi")
    rec = fake.record()
    assert rec["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert not any(TOKEN in arg for arg in rec["argv"])
    assert TOKEN not in rec["stdin"] and TOKEN not in rec["prompt"]
    assert TOKEN not in repr(cli)


async def test_echoed_token_is_redacted(fake):
    fake.mode("echo_token")
    assert await fake.cli(oauth_token=TOKEN).generate("system", "hi") == f"token={REDACTED}"


async def test_rejected_token_message_explains_and_hides_token(fake):
    fake.mode("auth")
    with pytest.raises(ClaudeAuthError) as caught:
        await fake.cli(oauth_token=TOKEN).generate("system", "hi")
    message = str(caught.value)
    assert TOKEN not in message and TOKEN not in caught.value.detail
    assert REDACTED in message
    assert "claude setup-token" in message
    assert "CLI said:" in message


async def test_login_error_without_token(fake):
    fake.mode("auth")
    with pytest.raises(ClaudeAuthError) as caught:
        await fake.cli().generate("system", "hi")
    assert "/login" in str(caught.value)


async def test_without_configured_token_child_has_none(fake, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-parent")
    fake.mode("echo_token")
    assert await fake.cli().generate("system", "hi") == "token="


async def test_inspect_reports_auth_and_init(fake):
    fake.mode("stream")
    info = await fake.cli(oauth_token=TOKEN, auth_label="subscription token from .env").inspect()
    assert info["auth"] == "subscription token from .env"
    assert info["init"]["tools"] == [] and info["init"]["apiProvider"] == "firstParty"
    assert info["reply"] == "OK"
    argv = fake.record()["argv"]
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv
    assert fake.record()["stdin"] == "ping"


async def test_inspect_default_auth_label(fake):
    fake.mode("stream")
    assert (await fake.cli().inspect())["auth"] == "this machine's claude login"


async def test_runs_in_its_own_private_directory(fake):
    await fake.cli().generate("system", "hi")
    rec = fake.record()
    assert Path(rec["cwd"]).resolve() == fake.workdir.resolve()
    assert rec["cwd_mode"] == "0o700"
    assert len(rec["listing"]) == 1 and rec["listing"][0].startswith("system-")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_timeout_kills_the_whole_process_group(fake):
    fake.mode("sleep")
    started = time.monotonic()
    with pytest.raises(ClaudeTimeout):
        await fake.cli(timeout=1.5).generate("system", "hi")
    rec = fake.record()
    deadline = time.monotonic() + 3
    while (_alive(rec["pid"]) or _alive(rec["child_pid"])) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(rec["pid"])
    assert not _alive(rec["child_pid"])
    assert time.monotonic() - started < 5


async def test_limit_with_epoch(fake):
    fake.mode("limit_epoch")
    with pytest.raises(ClaudeLimitError) as caught:
        await fake.cli().generate("system", "hi")
    assert caught.value.resets_at == 1760000000
    assert "|" not in caught.value.detail
    assert str(caught.value).startswith("Claude subscription usage limit reached (resets ")


async def test_limit_with_text(fake):
    fake.mode("limit_text")
    with pytest.raises(ClaudeLimitError) as caught:
        await fake.cli().generate("system", "hi")
    assert caught.value.reset_text == "3pm (Asia/Tashkent)"
    assert caught.value.resets_at is None


async def test_crash_empty_and_missing_binary(fake, tmp_path):
    fake.mode("crash")
    with pytest.raises(ClaudeError, match="Something exploded"):
        await fake.cli().generate("system", "hi")
    fake.mode("empty")
    with pytest.raises(ClaudeError, match="empty"):
        await fake.cli().generate("system", "hi")
    missing = ClaudeCLI(str(tmp_path / "nope"), workdir=fake.workdir)
    with pytest.raises(ClaudeError, match="not found"):
        await missing.generate("system", "hi")


def test_classify_failure():
    assert isinstance(classify_failure("anything", 429), ClaudeLimitError)
    for text in ("Usage limit reached", "You've hit your limit", "rate_limit_error",
                 "You're out of extra usage", "Quota exceeded"):
        assert isinstance(classify_failure(text), ClaudeLimitError), text
    for text in ("Not logged in", "Please run /login", "Invalid API key", "OAuth token has expired"):
        assert isinstance(classify_failure(text), ClaudeAuthError), text
    assert isinstance(classify_failure("x", 401), ClaudeAuthError)
    other = classify_failure("boom " * 300)
    assert type(other) is ClaudeError
    assert len(str(other)) < 600
    assert classify_failure("Not logged in " + "x" * 1000).detail == ("Not logged in " + "x" * 1000)[:300]


def test_parse_result():
    ok = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": " hi "})
    assert parse_result(0, "noise\n" + ok + "\n\n", "") == "hi"
    error = json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in"})
    with pytest.raises(ClaudeAuthError):
        parse_result(1, error, "")
    max_turns = json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": False})
    with pytest.raises(ClaudeError):
        parse_result(1, max_turns, "")
    with pytest.raises(ClaudeError, match="empty"):
        parse_result(0, json.dumps({"type": "result", "subtype": "success", "result": ""}), "")
    with pytest.raises(ClaudeError, match="exit code 7"):
        parse_result(7, "", "")
    with pytest.raises(ClaudeLimitError):
        parse_result(1, "not json", "usage limit reached")
