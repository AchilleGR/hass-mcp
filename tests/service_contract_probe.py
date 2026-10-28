"""Offline protocol regression; run inside the pinned image with networking disabled."""
import asyncio
import json
import importlib.util
import os
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, patch

import httpx
from mcp.shared.memory import create_connected_server_and_client_session
from app import hass, server


def shared_roundtrip(result):
    """Replay the real native MCP result through the production gateway HTTP path."""
    path = os.environ.get("GATEWAY_SOURCE")
    if not path:
        return
    spec = importlib.util.spec_from_file_location("gateway", path)
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    calls = []

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body)
            data = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()

    class Backend(g.Backend):
        def start(self):
            self.running = True

    gateway = g.Gateway({"image": g.IMAGE, "methods": {}}, Backend)
    try:
        for site in g.SITES:
            g.BACKENDS[site] = upstream.server_port
            frontend = g.ThreadingHTTPServer(("127.0.0.1", 0), g.handler(gateway, site))
            worker = threading.Thread(target=frontend.serve_forever, daemon=True)
            worker.start()
            try:
                body = {"jsonrpc": "2.0", "id": 17, "method": "tools/call",
                        "params": {"name": "call_service_tool", "arguments": {
                            "domain": "light", "service": "turn_on"}}}
                request = urllib.request.Request(f"http://127.0.0.1:{frontend.server_port}/mcp",
                    data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request) as response:
                    assert json.load(response) == {"jsonrpc": "2.0", "id": 17, "result": result}
                assert gateway.backends[site].users == 0
            finally:
                frontend.shutdown()
                frontend.server_close()
                worker.join()
        assert len(calls) == len(g.SITES), "Gateway retried or lost a request"
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join()


async def main():
    cases = [([], 200), ([{"entity_id": "light.fixture", "state": "on"}], 200),
             ({"service_response": {"fixture": True}}, 200), ({}, 401), ({}, 500),
             ({"error": "fixture failure"}, 200), ([{"error": "fixture failure"}], 200),
             ({}, "timeout"), ({}, "connection")]
    failures = []
    for payload, status in cases:
        request = httpx.Request("POST", "http://invalid.local/api/services/light/turn_on")
        client = AsyncMock()
        if status == "timeout":
            client.post.side_effect = httpx.ReadTimeout("fixture", request=request)
        elif status == "connection":
            client.post.side_effect = httpx.ConnectError("fixture", request=request)
        else:
            client.post.return_value = httpx.Response(status, json=payload, request=request)
        with patch.object(hass, "get_client", AsyncMock(return_value=client)):
            async with create_connected_server_and_client_session(server.mcp._mcp_server) as session:
                result = await session.call_tool("call_service_tool", {
                    "domain": "light", "service": "turn_on", "data": {"entity_id": "light.fixture"}})
                text = " ".join(c.text for c in result.content if c.type == "text")
                shared_roundtrip(result.model_dump(mode="json", exclude_none=True))
                error_payload = isinstance(payload, dict) and "error" in payload or (
                    isinstance(payload, list) and any("error" in item for item in payload))
                if status == 200 and not error_payload:
                    ok = not result.isError and json.loads(text).get("success") is True
                    if ok:
                        ok = json.loads(text)["affected_entities"] == payload
                else:
                    expected = str(status) if isinstance(status, int) and status != 200 else (
                        "Timeout" if status == "timeout" else "Connection" if status == "connection" else "error")
                    ok = bool(result.isError) and expected.lower() in text.lower()
                assert client.post.await_count == 1, "Service request retried"
                print(json.dumps({"status": status, "payload": payload, "isError": result.isError,
                                  "text": text, "pass": ok}))
                if not ok:
                    failures.append((status, payload))
    assert not failures, failures


if __name__ == "__main__":
    asyncio.run(main())
