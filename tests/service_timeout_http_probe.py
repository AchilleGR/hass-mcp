"""Offline real MCP HTTP caller -> shared gateway -> backend -> delayed fake HA."""
import asyncio
from datetime import timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

BOOT = '''
import hashlib,importlib.util,os
from pathlib import Path
from app import hass,server
assert hashlib.sha256(Path(hass.__file__).read_bytes()).hexdigest()==os.environ['EXPECTED_HASS_SHA']
assert hashlib.sha256(Path(server.__file__).read_bytes()).hexdigest()==os.environ['EXPECTED_SERVER_SHA']
spec=importlib.util.spec_from_file_location('gateway',os.environ['GATEWAY_SOURCE'])
g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
exec(g.HTTP_CODE)
'''


async def main():
    requests = []
    completed = {}
    tasks = set()

    async def fake_ha(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            header = await reader.readuntil(b'\r\n\r\n')
            length = next(int(line.split(b':', 1)[1]) for line in header.split(b'\r\n')
                          if line.lower().startswith(b'content-length:'))
            data = json.loads(await reader.readexactly(length))
            requests.append(data)
            await asyncio.sleep(1.8 if data['case'] == 'uncertain' else 10.3)
            completed[data['case']] = time.monotonic()
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n[]')
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            tasks.discard(task)

    ha = await asyncio.start_server(fake_ha, '127.0.0.1', 0)
    os.environ['HA_URL'] = 'http://127.0.0.1:' + str(ha.sockets[0].getsockname()[1])
    os.environ['HA_TOKEN'] = 'offline-fixture'
    from app import hass, server
    hashes = {name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
              for name, module in [('hass', hass), ('server', server)]}
    spec = importlib.util.spec_from_file_location('gateway', os.environ['GATEWAY_SOURCE'])
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    assert g.FORWARD_TIMEOUT_SECONDS == 75 and g.CALLER_TIMEOUT_SECONDS == 90
    assert hass.MAX_SERVICE_TIMEOUT_SECONDS == 60
    child = subprocess.Popen([sys.executable, '-c', BOOT], stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, env=dict(os.environ, EXPECTED_HASS_SHA=hashes['hass'],
                                        EXPECTED_SERVER_SHA=hashes['server']))
    frontend = worker = None
    observations = []
    try:
        for _ in range(150):
            if child.poll() is not None:
                raise AssertionError(child.stderr.read().decode())
            try:
                urllib.request.urlopen('http://127.0.0.1:8000/mcp', timeout=.1)
                break
            except urllib.error.HTTPError:
                break
            except OSError:
                await asyncio.sleep(.05)
        else:
            raise AssertionError('Native backend did not become ready')

        class NoDockerBackend(g.Backend):
            def start(self):
                self.running = True

        g.BACKENDS['wo'] = 8000
        gateway = g.Gateway(json.loads(Path(os.environ['CATALOG_PATH']).read_text()), NoDockerBackend)
        frontend = g.ThreadingHTTPServer(('127.0.0.1', 0), g.handler(gateway, 'wo'))
        worker = threading.Thread(target=frontend.serve_forever, daemon=True)
        worker.start()
        async with httpx.AsyncClient(timeout=httpx.Timeout(g.CALLER_TIMEOUT_SECONDS)) as http:
            async with streamable_http_client(f'http://127.0.0.1:{frontend.server_port}/mcp',
                                              http_client=http) as (read, write, _):
                async with ClientSession(read, write,
                        read_timeout_seconds=timedelta(seconds=g.CALLER_TIMEOUT_SECONDS)) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert len(tools.tools) == 29
                    schema = next(t.inputSchema for t in tools.tools if t.name == 'call_service_tool')
                    assert schema['properties']['timeout_seconds']['anyOf'][0]['maximum'] == 60
                    cases = [('reload', 'automation', 'reload', None, False),
                             ('ordinary', 'light', 'turn_on', None, True),
                             ('uncertain', 'automation', 'reload', 1, True)]
                    for index, (case, domain, service, budget, error) in enumerate(cases, 1):
                        arguments = {'domain': domain, 'service': service, 'data': {'case': case}}
                        if budget is not None:
                            arguments['timeout_seconds'] = budget
                        start = time.monotonic()
                        result = await session.call_tool('call_service_tool', arguments)
                        returned = time.monotonic()
                        text = ' '.join(c.text for c in result.content if c.type == 'text')
                        assert bool(result.isError) == error, (case, result)
                        if error:
                            assert 'Timeout' in text and 'uncertain' in text
                        else:
                            assert json.loads(text)['affected_entities'] == []
                        if case == 'uncertain':
                            assert case not in completed
                        end = time.monotonic() + 2
                        while case not in completed and time.monotonic() < end:
                            await asyncio.sleep(.02)
                        assert case in completed
                        if case == 'uncertain':
                            assert completed[case] > returned
                        assert len(requests) == index
                        assert requests[-1] == {'case': case}, 'Control timeout leaked into HA data'
                        observations.append({'case': case, 'isError': result.isError,
                            'elapsed_seconds': round(returned-start, 3), 'HA_POST_count': index})
                    invalid = await session.call_tool('call_service_tool', {
                        'domain': 'automation', 'service': 'reload', 'timeout_seconds': 0})
                    assert invalid.isError and len(requests) == 3
                    await session.send_ping()
    finally:
        if frontend:
            frontend.shutdown()
            frontend.server_close()
            worker.join()
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        ha.close()
        await ha.wait_closed()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 15)
    print(json.dumps({'source_hashes': hashes, 'gateway_sha256': g.CODE_SHA256,
        'budgets_seconds': {'HA_maximum': 60, 'gateway': 75, 'caller': 90},
        'observations': observations, 'invalid_budget_rejected': True, 'HA_POSTs': requests}), flush=True)


asyncio.run(main())
