from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencode_a2a.upstream.contracts import ExecutionState
from opencode_a2a.upstream.v2_events import V2EventHub, V2ExecutionUncertainError
from opencode_a2a.upstream.v2_execution import V2ExecutionEngine
from tests.upstream.test_v2_execution import NativeServer


def _database_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path}/state.db"


def _registry_path(tmp_path: Path) -> Path:
    return tmp_path / "state.sessions.json"


async def _restart(server: NativeServer) -> None:
    """Drop the in-memory gateway state the way a process restart does."""
    await server.hub.close()
    server.hub = V2EventHub(server.client, ready_timeout=0.1)
    server.engine = V2ExecutionEngine(server.client, server.hub, timeout=0.5)
    server.feed.send({"type": "server.connected"})


@pytest.mark.asyncio
async def test_restart_adopts_a_session_whose_projected_tail_still_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = NativeServer(monkeypatch, database_url=_database_url(tmp_path))
    try:
        first = await server.engine.submit("ses_one", "first")
        assert first.outcome is not None and first.outcome.state == ExecutionState.COMPLETED
        assert _registry_path(tmp_path).exists()

        await _restart(server)
        assert server.engine.sessions == {}

        await server.engine.adopt("ses_one", "/workspace")
        assert set(server.engine.sessions) == set(server.hub.sessions) == {"ses_one"}

        second = await server.engine.submit("ses_one", "second")
        assert second.outcome is not None and second.outcome.state == ExecutionState.COMPLETED
        assert len(server.inputs) == 2 and len(set(server.inputs)) == 2
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_restart_refuses_a_session_whose_tail_moved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = NativeServer(monkeypatch, database_url=_database_url(tmp_path))
    try:
        await server.engine.submit("ses_one", "first")
        await _restart(server)
        server.messages["ses_one"].insert(
            0, {"id": "msg_foreign_tail", "type": "assistant", "content": []}
        )

        before = len(server.requests)
        with pytest.raises(V2ExecutionUncertainError, match="gateway-created session"):
            await server.engine.adopt("ses_one", "/workspace")

        assert [request.url.path for request in server.requests[before:]] == [
            "/api/session/ses_one/message"
        ]
        assert server.engine.sessions == {}
        assert server.hub.sessions == {}
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_unknown_session_is_refused_without_upstream_http(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = NativeServer(monkeypatch, database_url=_database_url(tmp_path))
    try:
        with pytest.raises(V2ExecutionUncertainError, match="gateway-created session"):
            await server.engine.adopt("ses_foreign", "/workspace")
        assert server.requests == []
        assert set(server.hub.sessions) == {"ses_one"}
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_record_is_ignored_when_the_upstream_no_longer_has_the_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _registry_path(tmp_path).write_text(
        json.dumps({"ses_gone": {"directory": "/workspace", "last_message_id": "msg_cached"}})
    )
    server = NativeServer(monkeypatch, database_url=_database_url(tmp_path))
    try:
        server.messages["ses_gone"] = []
        with pytest.raises(V2ExecutionUncertainError, match="gateway-created session"):
            await server.engine.adopt("ses_gone", "/workspace")
        assert "ses_gone" not in server.engine.sessions
        assert "ses_gone" not in server.hub.sessions
    finally:
        await server.close()
