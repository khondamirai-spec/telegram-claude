"""Runs the Claude Code CLI (``claude -p``) to generate one reply.

Every reply is one isolated ``claude -p`` run that uses the owner's Claude
subscription (a ``claude setup-token`` token or the machine's login). No
Anthropic API key is ever used:

* the conversation goes in through stdin only, never through argv;
* the system prompt (with the profile) is written to a 0600 temp file, so it
  does not show up in ``ps``;
* no shell is involved, the child runs in its own empty 0700 directory and in
  its own process group so a timeout can kill everything it started;
* the child environment drops API keys, third-party provider switches and the
  bot token, and the subscription token is redacted from any output.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

from .store import ensure_private_dir

log = logging.getLogger(__name__)

# Built-in plugins shipped inside the CLI (from the stream-json init event).
BUILTIN_PLUGINS = (
    "cc-plugin-agents-md@builtin",
    "cc-plugin-telemetry@builtin",
    "cc-plugin-plugin-authoring@builtin",
)

ISOLATION_SETTINGS: Dict[str, Any] = {
    "disableAllHooks": True,
    "enabledPlugins": {name: False for name in BUILTIN_PLUGINS},
}

STRIPPED_ENV_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    # Set by --bare; it switches OAuth login off.
    "CLAUDE_CODE_SIMPLE",
)

ISOLATION_ENV = {
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
}

REDACTED = "[REDACTED]"

_LIMIT_RE = re.compile(
    r"usage limit|hit your (usage )?limit|limit reached|rate[ _-]?limit|out of (extra )?usage|quota",
    re.IGNORECASE,
)
_AUTH_RE = re.compile(
    r"not logged in|/login|invalid api key|authentication|unauthori[sz]ed|oauth token",
    re.IGNORECASE,
)
_EPOCH_RE = re.compile(r"\|\s*(\d{9,12})\b")
_RESET_RE = re.compile(r"\bresets?\s+((?:at\s+)?[^\n|·]+)", re.IGNORECASE)


class ClaudeError(Exception):
    """A Claude failure whose message is safe to show to the owner."""


class ClaudeTimeout(ClaudeError):
    pass


class ClaudeAuthError(ClaudeError):
    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail


class ClaudeLimitError(ClaudeError):
    def __init__(
        self,
        detail: str = "",
        resets_at: Optional[float] = None,
        reset_text: Optional[str] = None,
    ):
        if reset_text:
            when = reset_text
        elif resets_at:
            when = datetime.fromtimestamp(resets_at).strftime("%Y-%m-%d %H:%M")
        else:
            when = "time unknown"
        super().__init__(f"Claude subscription usage limit reached (resets {when})")
        self.detail = detail
        self.resets_at = resets_at
        self.reset_text = reset_text


def build_env(
    oauth_token: Optional[str] = None,
    base: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Environment for the ``claude`` child process."""
    env = {
        key: value
        for key, value in (os.environ if base is None else base).items()
        if not key.startswith("ANTHROPIC_") and key not in STRIPPED_ENV_KEYS
    }
    env.update(ISOLATION_ENV)
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    return env


def classify_failure(message: str, status: Optional[int] = None) -> ClaudeError:
    """Turn CLI error text into the matching exception (returned, not raised)."""
    text = (message or "").strip()
    if status == 429 or _LIMIT_RE.search(text):
        epoch = _EPOCH_RE.search(text)
        resets_at = float(epoch.group(1)) if epoch else None
        detail = _EPOCH_RE.sub("", text).strip()
        reset = _RESET_RE.search(detail)
        reset_text = reset.group(1).strip().rstrip(".") if reset else None
        return ClaudeLimitError(detail, resets_at, reset_text)
    if status in (401, 403) or _AUTH_RE.search(text):
        return ClaudeAuthError(
            "Claude CLI is not logged in or its login was rejected. "
            "Run `claude` on this machine and sign in with /login.",
            detail=text[:300],
        )
    return ClaudeError(f"Claude CLI failed: {text[:500] or 'no output'}")


def parse_result(returncode: int, stdout: str, stderr: str) -> str:
    """Extract the reply text from ``--output-format json/stream-json`` output."""
    lines = [line for line in (stdout or "").splitlines() if line.strip()]
    data: Any = None
    if lines:
        try:
            data = json.loads(lines[-1])
        except ValueError:
            data = None
    if not isinstance(data, dict):
        raise classify_failure(
            (stderr or "").strip() or (stdout or "").strip() or f"exit code {returncode}"
        )
    if data.get("is_error") or data.get("subtype") != "success":
        text = f"{data.get('result') or ''}\n{stderr or ''}".strip()
        status = data.get("api_error_status")
        raise classify_failure(
            text or f"subtype {data.get('subtype')}",
            status if isinstance(status, int) else None,
        )
    result = data.get("result")
    if not isinstance(result, str) or not result.strip():
        raise ClaudeError("Claude returned an empty reply")
    return result.strip()


class ClaudeCLI:
    """Generates replies by running ``claude -p`` once per reply."""

    def __init__(
        self,
        claude_bin: str,
        *,
        workdir: Union[Path, str],
        model: Optional[str] = None,
        effort: str = "low",
        timeout: float = 120.0,
        oauth_token: Optional[str] = None,
        auth_label: Optional[str] = None,
    ):
        self.claude_bin = claude_bin
        self.workdir = Path(workdir)
        self.model = model or None
        self.effort = effort
        self.timeout = timeout
        self._oauth_token = oauth_token or None
        self._auth_label = auth_label

    def __repr__(self) -> str:
        return (
            f"ClaudeCLI(claude_bin={self.claude_bin!r}, workdir={str(self.workdir)!r}, "
            f"model={self.model!r}, effort={self.effort!r}, timeout={self.timeout!r}, "
            f"auth={self.auth_mode!r})"
        )

    @property
    def auth_mode(self) -> str:
        if self._auth_label:
            return self._auth_label
        return "subscription token" if self._oauth_token else "this machine's claude login"

    def build_command(self, system_prompt_file: str, output_format: str = "json") -> List[str]:
        cmd = [
            self.claude_bin, "-p",
            "--safe-mode",
            "--settings", json.dumps(ISOLATION_SETTINGS),
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--system-prompt-file", system_prompt_file,
            "--output-format", output_format,
            "--effort", self.effort,
        ]
        if output_format == "stream-json":
            cmd.append("--verbose")
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    def _redact(self, text: str) -> str:
        if self._oauth_token:
            text = text.replace(self._oauth_token, REDACTED)
        return text

    @staticmethod
    def _kill(proc: "asyncio.subprocess.Process") -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    async def _run(self, system_prompt: str, stdin_text: str, output_format: str) -> Tuple[int, str, str]:
        ensure_private_dir(self.workdir)
        fd, prompt_path = tempfile.mkstemp(prefix="system-", suffix=".txt", dir=self.workdir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(system_prompt)
            cmd = self.build_command(prompt_path, output_format)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(self.workdir),
                    env=build_env(self._oauth_token),
                    start_new_session=True,
                )
            except (FileNotFoundError, NotADirectoryError):
                raise ClaudeError(
                    f"Claude CLI not found at {self.claude_bin}. Install Claude Code "
                    "or set CLAUDE_BIN"
                ) from None
            except PermissionError:
                raise ClaudeError(f"Claude CLI at {self.claude_bin} is not executable") from None
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(stdin_text.encode("utf-8")), self.timeout
                )
            except asyncio.TimeoutError:
                self._kill(proc)
                await proc.wait()
                raise ClaudeTimeout(f"Claude CLI did not answer within {self.timeout:g} seconds") from None
            except asyncio.CancelledError:
                self._kill(proc)
                raise
        finally:
            try:
                os.unlink(prompt_path)
            except OSError:
                pass
        return (
            proc.returncode if proc.returncode is not None else -1,
            self._redact(stdout.decode("utf-8", errors="replace")),
            self._redact(stderr.decode("utf-8", errors="replace")),
        )

    def _explain_auth(self, error: ClaudeAuthError) -> ClaudeAuthError:
        if not self._oauth_token:
            return error
        detail = self._redact(error.detail)
        return ClaudeAuthError(
            "Claude rejected the subscription token or it has expired: run "
            "`claude setup-token` on a computer with a browser and update "
            f"CLAUDE_CODE_OAUTH_TOKEN. CLI said: {detail or 'nothing'}",
            detail,
        )

    async def generate(self, system_prompt: str, user_prompt: str) -> str:
        returncode, stdout, stderr = await self._run(system_prompt, user_prompt, "json")
        try:
            return parse_result(returncode, stdout, stderr)
        except ClaudeAuthError as error:
            raise self._explain_auth(error) from None

    async def inspect(self) -> Dict[str, Any]:
        """Send a tiny request and report what the CLI loaded and how it logged in."""
        returncode, stdout, stderr = await self._run("Reply with exactly: OK", "ping", "stream-json")
        init: Optional[Dict[str, Any]] = None
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "system" and event.get("subtype") == "init":
                init = {
                    key: event.get(key)
                    for key in ("tools", "mcp_servers", "plugins", "skills", "apiKeySource", "apiProvider", "model")
                }
                break
        try:
            reply = parse_result(returncode, stdout, stderr)
        except ClaudeAuthError as error:
            raise self._explain_auth(error) from None
        return {"auth": self.auth_mode, "init": init, "reply": reply}
