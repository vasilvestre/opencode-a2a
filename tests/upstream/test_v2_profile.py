from __future__ import annotations

import json

import httpx
import pytest

from opencode_a2a.opencode_upstream_client import UpstreamContractError
from opencode_a2a.upstream.v2 import UnsupportedUpstreamOperationError, V2UpstreamAdapter
from opencode_a2a.upstream.v2_profile import (
    exclusive_permissions,
    validate_exclusive_configuration,
    validate_exclusive_settings,
)
from tests.support.settings import make_settings


def test_exclusive_profile_requires_private_separate_authenticated_upstream() -> None:
    settings = make_settings()
    with pytest.raises(ValueError, match="credentials"):
        validate_exclusive_settings(settings)
    settings.opencode_auth_password = "test-only-upstream"  # pragma: allowlist secret
    settings.opencode_workspace_root = "/workspace"
    validate_exclusive_settings(settings)
    settings.opencode_base_url = "http://example.test:4096"
    with pytest.raises(ValueError, match="loopback"):
        validate_exclusive_settings(settings)


def test_background_and_custom_producers_are_rejected() -> None:
    plugins = {"data": [{"source": {"type": "builtin"}}]}
    validate_exclusive_configuration([{"type": "document", "info": {"warming": False}}], plugins)
    for info in (
        {"warming": True},
        {"plugins": ["untrusted"]},
        {"mcp": {"servers": {"remote": {}}}},
    ):
        with pytest.raises(UpstreamContractError):
            validate_exclusive_configuration([{"type": "document", "info": info}], plugins)
    with pytest.raises(UpstreamContractError, match="builtin"):
        validate_exclusive_configuration([], {"data": [{"source": {"type": "local"}}]})
    permissions = exclusive_permissions()
    assert permissions == [
        {"action": "*", "resource": "*", "effect": "deny"},
        {"action": "question", "resource": "*", "effect": "allow"},
        {"action": "read", "resource": "*", "effect": "ask"},
        {"action": "external_directory", "resource": "*", "effect": "deny"},
    ]
    assert permissions[0] == {"action": "*", "resource": "*", "effect": "deny"}
    assert permissions[-1] == {"action": "external_directory", "resource": "*", "effect": "deny"}


@pytest.mark.asyncio
async def test_exclusive_workspace_is_fixed_without_changing_default_v2_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exclusive = V2UpstreamAdapter(
        make_settings(
            opencode_api_version="v2",
            opencode_v2_execution="exclusive",
            opencode_auth_password="test-only-upstream",  # pragma: allowlist secret
            opencode_workspace_root="/workspace",
        )
    )
    transport = V2UpstreamAdapter(
        make_settings(opencode_api_version="v2", opencode_workspace_root="/workspace")
    )
    requests: list[httpx.Request] = []

    def unexpected(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("A location override must reject before upstream HTTP")

    monkeypatch.setattr(exclusive._client, "_transport", httpx.MockTransport(unexpected))
    try:
        assert exclusive._resolve_location(None, None) == "/workspace"
        with pytest.raises(ValueError, match="fixed configured workspace"):
            await exclusive.create_session(directory="/workspace/other")
        assert transport._resolve_location("/workspace/other", None) == "/workspace/other"
        assert exclusive._v2_execution.sessions == {}
        assert requests == []
    finally:
        await exclusive.close()
        await transport.close()


def test_tool_policy_full_lifts_the_deny_all_rule_only() -> None:
    assert exclusive_permissions() == exclusive_permissions("readonly")
    assert {"action": "*", "resource": "*", "effect": "deny"} not in exclusive_permissions("full")
    assert {"action": "*", "resource": "*", "effect": "allow"} in exclusive_permissions("full")
    assert exclusive_permissions("full")[-1] == {
        "action": "external_directory",
        "resource": "*",
        "effect": "deny",
    }


def _exclusive_settings(**overrides: object):
    return make_settings(
        opencode_api_version="v2",
        opencode_v2_execution="exclusive",
        opencode_auth_password="test-only-upstream",  # pragma: allowlist secret
        opencode_workspace_root="/workspace",
        **overrides,
    )


@pytest.mark.asyncio
async def test_version_pin_accepts_the_2x_line_and_the_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def verify(version: str, **overrides: object) -> list[str]:
        requests: list[httpx.Request] = []

        async def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/api/info":
                return httpx.Response(200, json={"version": version})
            if request.url.path == "/api/config":
                return httpx.Response(200, json=[])
            return httpx.Response(200, json={"data": []})

        client = V2UpstreamAdapter(_exclusive_settings(**overrides))
        monkeypatch.setattr(client._client, "_transport", httpx.MockTransport(respond))
        try:
            await client._verify_exclusive_profile("/workspace")
        finally:
            await client.close()
        return [request.url.path for request in requests]

    assert await verify("2.2.5") == ["/api/info", "/api/config", "/api/plugin"]
    with pytest.raises(UpstreamContractError, match="3.0.0"):
        await verify("3.0.0")
    assert await verify("3.0.0", a2a_upstream_pin_override=True) == [
        "/api/info",
        "/api/config",
        "/api/plugin",
    ]


@pytest.mark.asyncio
async def test_shared_workspace_skips_only_the_inventory_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/info":
            return httpx.Response(200, json={"version": "2.2.5"})
        if request.url.path == "/api/session":
            return httpx.Response(200, json={"data": {"id": "ses-shared"}})
        raise AssertionError(f"unexpected upstream call {request.url.path}")

    client = V2UpstreamAdapter(
        _exclusive_settings(a2a_shared_workspace=True, a2a_tool_policy="full")
    )
    monkeypatch.setattr(client._client, "_transport", httpx.MockTransport(respond))
    try:
        session_id = await client.create_session()
        payload = json.loads(requests[-1].content)
        with pytest.raises(UnsupportedUpstreamOperationError, match="gateway-created session"):
            await client.submit_prompt("ses-imported", "run hostname")
    finally:
        await client.close()

    assert [request.url.path for request in requests] == ["/api/info", "/api/session"]
    assert session_id == "ses-shared"
    assert payload["permissions"] == exclusive_permissions("full")
