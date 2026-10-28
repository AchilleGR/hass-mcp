"""Native MCP and loopback HTTP regression coverage for bounded service calls."""
import asyncio
from contextlib import asynccontextmanager
import json
import time

import httpx
import pytest
import respx
from mcp.shared.memory import create_connected_server_and_client_session

from app import hass, server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def mock_get_client():
    # Override conftest's mocked client: exercise the real HTTPX request boundary.
    yield


@pytest.fixture(autouse=True)
async def reset_client():
    await hass.cleanup_client()
    yield
    await hass.cleanup_client()


async def invoke(arguments):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        return await client.call_tool("call_service_tool", arguments)


@asynccontextmanager
async def fake_ha(monkeypatch, delay, drip=False):
    requests = []
    finished = asyncio.Event()
    tasks = set()
    times = {}

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1]) for line in header.split(b"\r\n")
                          if line.lower().startswith(b"content-length:"))
            requests.append(json.loads(await reader.readexactly(length)))
            await asyncio.sleep(delay)
            times["completed"] = time.monotonic()
            finished.set()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n")
            if drip:
                # Each read progresses inside a 1s idle budget; total time exceeds it.
                for chunk in (b"[", b"]"):
                    await asyncio.sleep(0.75)
                    writer.write(chunk)
                    await writer.drain()
            else:
                writer.write(b"[]")
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            tasks.discard(task)

    endpoint = await asyncio.start_server(handle, "127.0.0.1", 0)
    monkeypatch.setattr(hass, "HA_URL", "http://127.0.0.1:" + str(endpoint.sockets[0].getsockname()[1]))
    try:
        yield requests, finished, times
    finally:
        endpoint.close()
        await endpoint.wait_closed()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 15)


async def test_reload_survives_previous_deadline(monkeypatch):
    async with fake_ha(monkeypatch, delay=10.3) as (requests, finished, _):
        result = await invoke({"domain": "automation", "service": "reload", "data": {"id": "fixture"}})
        assert not result.isError, result.content
        assert json.loads(result.content[0].text)["affected_entities"] == []
        assert requests == [{"id": "fixture"}]
        assert finished.is_set()


async def test_ordinary_service_retains_ten_second_limit(monkeypatch):
    async with fake_ha(monkeypatch, delay=10.3) as (requests, finished, _):
        result = await invoke({"domain": "light", "service": "turn_on", "data": {"entity_id": "light.fixture"}})
        assert result.isError and "Timeout" in result.content[0].text
        await asyncio.wait_for(finished.wait(), 2)
        assert requests == [{"entity_id": "light.fixture"}]


@pytest.mark.parametrize("domain,service,override,expected", [
    ("light", "turn_on", None, 10), ("automation", "reload", None, 60),
    ("automation", "reload", 30, 30), ("light", "turn_on", 60, 60),
    ("light", "turn_on", 1, 1), ("light", "turn_on", 1.5, 1.5),
])
@respx.mock
async def test_per_call_budget_is_not_service_data(domain, service, override, expected):
    route = respx.post(f"http://localhost:8123/api/services/{domain}/{service}").mock(
        return_value=httpx.Response(200, json=[]))
    args = {"domain": domain, "service": service, "data": {"fixture": True}}
    if override is not None:
        args["timeout_seconds"] = override
    result = await invoke(args)
    assert not result.isError
    assert route.call_count == 1
    request = route.calls[0].request
    assert json.loads(request.content) == {"fixture": True}
    assert request.extensions["timeout"] == {"connect": 10, "read": expected, "write": 10, "pool": 10}
    assert (await hass.get_client()).timeout.read == 10


@respx.mock
async def test_budget_does_not_consume_a_same_named_service_data_field():
    route = respx.post("http://localhost:8123/api/services/automation/reload").mock(
        return_value=httpx.Response(200, json=[]))
    data = {"id": "fixture", "timeout_seconds": "HA-owned field"}
    result = await invoke({"domain": "automation", "service": "reload", "data": data,
                           "timeout_seconds": 1.5})
    assert not result.isError
    assert json.loads(route.calls[0].request.content) == data
    assert route.call_count == 1


@pytest.mark.parametrize("invalid", [True, False, "60", 0, -1, 60.1])
@respx.mock
async def test_invalid_native_budget_never_dispatches(invalid):
    route = respx.post("http://localhost:8123/api/services/automation/reload").mock(
        return_value=httpx.Response(200, json=[]))
    result = await invoke({"domain": "automation", "service": "reload", "timeout_seconds": invalid})
    assert result.isError
    assert route.call_count == 0


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf")])
@respx.mock
async def test_nonfinite_values_rejected_at_mcp_validation_before_dispatch(invalid):
    from mcp.server.fastmcp.exceptions import ToolError
    route = respx.post("http://localhost:8123/api/services/automation/reload").mock(
        return_value=httpx.Response(200, json=[]))
    with pytest.raises((ToolError, ValueError), match="finite"):
        await server.mcp.call_tool("call_service_tool", {
            "domain": "automation", "service": "reload", "timeout_seconds": invalid})
    assert route.call_count == 0


@pytest.mark.parametrize("invalid", [True, "60", float("nan"), float("inf"), -float("inf"), 0, 61])
@respx.mock
async def test_direct_helper_validates_nonfinite_and_bounds(invalid):
    route = respx.post("http://localhost:8123/api/services/automation/reload").mock(
        return_value=httpx.Response(200, json=[]))
    result = await hass.call_service("automation", "reload", {}, timeout_seconds=invalid)
    assert "finite number between 1 and 60" in result["error"]
    assert route.call_count == 0


async def test_deadline_covers_continuously_progressing_response(monkeypatch):
    async with fake_ha(monkeypatch, delay=0, drip=True) as (requests, _, _):
        start = time.monotonic()
        result = await invoke({"domain": "automation", "service": "reload", "timeout_seconds": 1})
        elapsed = time.monotonic() - start
        assert result.isError and "Timeout" in result.content[0].text
        assert elapsed < 1.5, elapsed
        assert requests == [{}]


async def test_timeout_does_not_retry_or_undo_accepted_ha_work(monkeypatch):
    async with fake_ha(monkeypatch, delay=1.3) as (requests, finished, times):
        result = await invoke({"domain": "automation", "service": "reload", "timeout_seconds": 1})
        returned = time.monotonic()
        assert result.isError and "Timeout" in result.content[0].text
        assert not finished.is_set()
        assert "uncertain" in result.content[0].text
        assert "before retrying" in result.content[0].text
        await asyncio.wait_for(finished.wait(), 2)
        assert times["completed"] > returned
        assert requests == [{}]
