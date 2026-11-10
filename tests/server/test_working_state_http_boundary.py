from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from tests.server.test_v2_cancel_http_boundary import HttpBoundary


class MessageSendBoundary(HttpBoundary):
    """Non-streaming SendMessage against the real host and SDK lifecycle."""

    async def start_message_send(
        self, http: httpx.AsyncClient
    ) -> tuple[str, asyncio.Task[httpx.Response]]:
        pending = asyncio.create_task(
            http.post(
                "/message:send",
                json={
                    "message": {
                        "messageId": f"input-{len(self.pending)}",
                        "role": "ROLE_USER",
                        "parts": [{"text": "wait for the working state"}],
                    }
                },
            )
        )
        self.pending.append(pending)
        async with asyncio.timeout(10):
            while True:
                if self.admitted.is_set():
                    listed = await http.get("/tasks")
                    tasks = listed.json()["tasks"]
                    if tasks:
                        return tasks[0]["id"], pending
                if pending.done():
                    raise AssertionError(f"Request ended before execution: {(await pending).text}")
                await asyncio.sleep(0.01)

    async def state(self, http: httpx.AsyncClient, task_id: str) -> str:
        response = await http.get(f"/tasks/{task_id}")
        assert response.status_code == 200
        return str(response.json()["status"]["state"])


@pytest_asyncio.fixture
async def boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[tuple[MessageSendBoundary, httpx.AsyncClient]]:
    server = MessageSendBoundary(tmp_path)
    monkeypatch.setattr(server.client._client, "_transport", httpx.MockTransport(server.handle))
    async with server.app.router.lifespan_context(server.app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app),
            base_url="http://test",
            headers={"Authorization": "Bearer test-token", "A2A-Version": "1.0"},
        ) as http:
            try:
                yield server, http
            finally:
                for stream in server.pending:
                    stream.cancel()
                await asyncio.gather(*server.pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_in_flight_message_send_is_listed_and_fetched_as_working(boundary) -> None:
    server, http = boundary
    task_id, pending = await server.start_message_send(http)

    assert await server.state(http, task_id) == "TASK_STATE_WORKING"
    listed = await http.get("/tasks", params={"status": "TASK_STATE_WORKING"})
    assert [task["id"] for task in listed.json()["tasks"]] == [task_id]

    server.execution("ses_one", server.inputs[0])
    finished = await asyncio.wait_for(pending, 2)
    assert finished.status_code == 200


@pytest.mark.asyncio
async def test_message_send_success_is_terminal_after_working(boundary) -> None:
    server, http = boundary
    task_id, pending = await server.start_message_send(http)
    assert await server.state(http, task_id) == "TASK_STATE_WORKING"

    server.execution("ses_one", server.inputs[0])
    finished = await asyncio.wait_for(pending, 2)

    assert finished.json()["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
    stored = (await http.get(f"/tasks/{task_id}")).json()
    assert stored["status"]["state"] == "TASK_STATE_COMPLETED"
    assert "ses_one" in json.dumps(stored["artifacts"])


@pytest.mark.asyncio
async def test_message_send_upstream_failure_is_terminal_not_stuck_working(boundary) -> None:
    server, http = boundary
    task_id, pending = await server.start_message_send(http)
    assert await server.state(http, task_id) == "TASK_STATE_WORKING"

    server.mode = "failed"
    server.execution("ses_one", server.inputs[0])
    finished = await asyncio.wait_for(pending, 2)

    assert finished.json()["task"]["status"]["state"] == "TASK_STATE_FAILED"
    assert await server.state(http, task_id) == "TASK_STATE_FAILED"


@pytest.mark.asyncio
async def test_cancel_while_working_marks_the_task_canceled(boundary) -> None:
    server, http = boundary
    task_id, pending = await server.start_message_send(http)
    assert await server.state(http, task_id) == "TASK_STATE_WORKING"

    canceled = await asyncio.wait_for(http.post(f"/tasks/{task_id}:cancel", json={}), 2)

    assert canceled.status_code == 200
    assert canceled.json()["status"]["state"] == "TASK_STATE_CANCELED"
    assert await server.state(http, task_id) == "TASK_STATE_CANCELED"
    finished = await asyncio.wait_for(pending, 2)
    assert finished.json()["task"]["status"]["state"] == "TASK_STATE_CANCELED"
