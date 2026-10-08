"""Small JSON-file stores kept under ``data/``."""

from __future__ import annotations

import json
import os
import tempfile
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union


def ensure_private_dir(path: Union[Path, str]) -> Path:
    """Create ``path`` if needed and make it accessible to the owner only."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def append_private_line(path: Path, line: str) -> None:
    """Append one line to a file that is created with mode 0600."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(line.rstrip("\n") + "\n")


class JsonFile:
    """A JSON document written atomically (temp file 0600 + os.replace)."""

    def __init__(self, path: Union[Path, str], default: Callable[[], Any]):
        self.path = Path(path)
        self._default = default

    def load(self) -> Any:
        try:
            with open(self.path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return self._default()

    def save(self, data: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


class HistoryStore:
    """Recent messages per business connection *and* chat."""

    def __init__(self, path: Union[Path, str], limit: int):
        self._file = JsonFile(path, dict)
        self.limit = limit
        data = self._file.load()
        self._chats: Dict[str, List[Dict[str, Any]]] = data if isinstance(data, dict) else {}

    @staticmethod
    def key(connection_id: str, chat_id: int) -> str:
        return f"{connection_id}:{chat_id}"

    def get(self, connection_id: str, chat_id: int) -> List[Dict[str, Any]]:
        return [dict(item) for item in self._chats.get(self.key(connection_id, chat_id), [])]

    def add(
        self,
        connection_id: str,
        chat_id: int,
        *,
        role: str,
        name: str,
        text: str,
        lang: Optional[str],
        ts: float,
    ) -> None:
        items = self._chats.setdefault(self.key(connection_id, chat_id), [])
        items.append({"role": role, "name": name, "text": text, "lang": lang, "ts": ts})
        del items[: max(0, len(items) - self.limit)]
        self._file.save(self._chats)

    def last_language(self, connection_id: str, chat_id: int) -> Optional[str]:
        for item in reversed(self._chats.get(self.key(connection_id, chat_id), [])):
            if item.get("role") == "other" and item.get("lang"):
                return item["lang"]
        return None

    def chat_count(self) -> int:
        return len(self._chats)


class SeenMessages:
    """Bounded set of processed message keys that survives restarts."""

    def __init__(self, path: Union[Path, str], capacity: int = 5000):
        self._file = JsonFile(path, list)
        self.capacity = capacity
        data = self._file.load()
        keys = data if isinstance(data, list) else []
        self._keys: "OrderedDict[str, None]" = OrderedDict((str(k), None) for k in keys[-capacity:])

    def first_time(self, key: str) -> bool:
        if key in self._keys:
            return False
        self._keys[key] = None
        while len(self._keys) > self.capacity:
            self._keys.popitem(last=False)
        self._file.save(list(self._keys))
        return True


class RuntimeState:
    """Pause flag and usage-limit deadline that survive restarts."""

    def __init__(self, path: Union[Path, str]):
        self._file = JsonFile(path, dict)
        data = self._file.load()
        data = data if isinstance(data, dict) else {}
        self._paused = bool(data.get("paused", False))
        limit = data.get("limit_until")
        self._limit_until: Optional[float] = float(limit) if isinstance(limit, (int, float)) else None

    def _save(self) -> None:
        self._file.save({"paused": self._paused, "limit_until": self._limit_until})

    @property
    def paused(self) -> bool:
        return self._paused

    @paused.setter
    def paused(self, value: bool) -> None:
        self._paused = bool(value)
        self._save()

    @property
    def limit_until(self) -> Optional[float]:
        return self._limit_until

    @limit_until.setter
    def limit_until(self, value: Optional[float]) -> None:
        self._limit_until = value
        self._save()
