from __future__ import annotations

import json
from pathlib import Path

from opencode_a2a.upstream.v2_session_registry import SessionRecord, V2SessionRegistry


def test_registry_round_trip_through_the_database_sibling(tmp_path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path}/task.db"
    registry = V2SessionRegistry.from_database_url(url)
    assert registry.path == tmp_path / "task.sessions.json"
    registry.save("ses_one", "/workspace", None)
    registry.save("ses_two", "/workspace", "msg_tail")
    assert json.loads((tmp_path / "task.sessions.json").read_text()) == {
        "ses_one": {"directory": "/workspace", "last_message_id": None},
        "ses_two": {"directory": "/workspace", "last_message_id": "msg_tail"},
    }

    reopened = V2SessionRegistry.from_database_url(url)
    assert reopened.get("ses_one") == SessionRecord("/workspace", None)
    assert reopened.get("ses_two") == SessionRecord("/workspace", "msg_tail")
    assert reopened.get("ses_unknown") is None


def test_relative_database_url_keeps_the_registry_next_to_it() -> None:
    registry = V2SessionRegistry.from_database_url("sqlite+aiosqlite:///./opencode-a2a.db")
    assert registry.path == Path("opencode-a2a.sessions.json")


def test_registry_disables_persistence_without_a_file_database() -> None:
    url = "sqlite+aiosqlite:///:memory:"
    for value in (None, "", url, "postgresql://db"):
        registry = V2SessionRegistry.from_database_url(value)
        assert registry.path is None
        registry.save("ses_one", "/workspace", "msg_tail")
        assert registry.get("ses_one") == SessionRecord("/workspace", "msg_tail")


def test_unreadable_registry_keeps_no_adoptable_records(tmp_path) -> None:
    path = tmp_path / "task.sessions.json"
    path.write_text("{not json")
    assert V2SessionRegistry(path).records == {}

    path.write_text('{"ses_one": {"directory": 5}, "ses_two": "bad", "ses_three": {}}')
    assert V2SessionRegistry(path).records == {}
