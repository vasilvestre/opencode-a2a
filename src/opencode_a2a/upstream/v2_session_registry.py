"""Durable record of gateway-created sessions, used only to re-adopt after a restart.

A record never grants ownership by itself: V2ExecutionEngine.adopt() compares the
stored tail with the upstream projection before it reclaims a session. The file is
a sibling of the task-store database so both survive the same disk lifetime.

shortcut: records are never pruned, so the file grows with every session this
gateway ever created; prune it if the gateway serves hundreds of thousands.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEME = "sqlite+aiosqlite://"
_SUFFIX = ".sessions.json"


@dataclass(frozen=True)
class SessionRecord:
    directory: str
    last_message_id: str | None


class V2SessionRegistry:
    """JSON sibling of the task-store database; a null path disables persistence."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.records: dict[str, SessionRecord] = {}
        if path is not None:
            self._load()

    @classmethod
    def from_database_url(cls, url: str | None) -> V2SessionRegistry:
        """Derive the sibling path, or disable persistence for a non-file database."""
        if not url or not url.startswith(_SCHEME):
            return cls(None)
        # sqlite:///relative and sqlite:////absolute both keep one leading slash.
        raw = url[len(_SCHEME) :]
        if not raw.startswith("/"):
            return cls(None)
        database = Path(raw[1:])
        if not database.name or database.name.startswith(":"):
            return cls(None)
        return cls(database.with_suffix(_SUFFIX))

    def get(self, session: str) -> SessionRecord | None:
        return self.records.get(session)

    def save(self, session: str, directory: str, last_message_id: str | None) -> None:
        """Record a gateway-created session; a failed write only disables adoption."""
        self.records[session] = SessionRecord(directory, last_message_id)
        if self.path is None:
            return
        payload = {
            identity: {
                "directory": record.directory,
                "last_message_id": record.last_message_id,
            }
            for identity, record in self.records.items()
        }
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(payload, sort_keys=True))
            os.replace(temporary, self.path)
        except OSError as exc:
            logger.warning("Cannot persist the V2 session registry: %s", exc)

    def _load(self) -> None:
        """Drop anything malformed: an unreadable record must never be adopted."""
        if self.path is None:
            return
        try:
            payload = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        for session, entry in payload.items():
            if not isinstance(session, str) or not session or not isinstance(entry, dict):
                continue
            directory = entry.get("directory")
            tail = entry.get("last_message_id")
            if not isinstance(directory, str) or not directory:
                continue
            if tail is not None and not isinstance(tail, str):
                continue
            self.records[session] = SessionRecord(directory, tail)
