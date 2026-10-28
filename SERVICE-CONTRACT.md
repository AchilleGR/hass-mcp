# Service contract patch for v0.6.1

Branch `fix/service-contract-v0.6.1` is based on upstream release commit
`bff845329fdb4b3a72bbf2e2fba14984515f281a`. Achilleas selected maintained
master/v0.6.1 after clarification that upstream dev is an older unfinished rewrite.

Upstream already wraps successful HA responses. This patch preserves that
wrapper and raises MCP `ToolError` for helper error dictionaries or list entries,
retaining their diagnostic text. Nested `error` keys in successful service data
remain data. The call is never retried. Successful command return does not prove
physical device feedback.

`Dockerfile.service-contract` pins the official v0.6.1 image index
`88b5614295d131b6f4f199db79dc878b685e88252064686eafc9999c37620d27`, whose source
label matches the release commit. It replaces the installed `server.py` and
`hass.py` modules in `/usr/local/lib/python3.13/site-packages/app/`; the Python minor and
installation path are bound to this exact base image. This makes file, module
and `python -c` execution import the same patched implementation. Dependencies
and other upstream files remain those of the release image.

The offline probe `tests/service_contract_probe.py` checks eleven native MCP
cases: dict/list/empty successes, nested error data, HTTP401/500, explicit
dict/list failures, timeout and connection failure. It mocks the HA HTTP client
and asserts one POST per call. Set `GATEWAY_SOURCE` to replay each native result
through all nine production gateway HTTP routes. Use dummy HA credentials and
Docker `--network none`. This replay does not itself test native backend HTTP
startup; the independent reviewer separately exercises production HTTP_CODE
with a real MCP HTTP client. Run the pytest suite separately; the six new
parametrized protocol regressions verify HTTP401/403/500 and three success shapes.

The expanded upstream catalog has 29 tools, including dashboard mutation and
restore tools. The shared gateway must provision a private durable backup bind
per site and validate its container identity before exposing these tools. Keep
these host directories across container replacement and rollback. The owning
codex-config runbook defines deployment, idle guards, rollback and host receipts.
The older compatibility branch and image remain available for rollback.

## Bounded service-call timeouts

`call_service_tool` accepts optional top-level `timeout_seconds`, a finite number
from 1 through 60. Omitted or null selects 60 seconds for `automation.reload`
and 10 seconds for every other service. Booleans and numeric strings are
rejected. The control is never inserted into HA service data; a same-named key
inside `data` remains ordinary service data.

The shared HTTPX client keeps its 10-second defaults. Each service request has
its own read timeout and an `asyncio.timeout` deadline around client acquisition
and the complete HTTP exchange. Connect, write and pool phase limits remain
10 seconds; a progressing response cannot evade the total deadline. Concurrent
calls do not change one another's budgets. Caller cancellation propagates.

A timeout returns an MCP error stating that the service outcome is uncertain
and effects must be verified before retrying. HA may finish after the caller
stops waiting. No automatic retry is made. HTTP error reporting and successful
response envelopes retain their existing behavior.

`tests/test_service_timeouts.py` uses real loopback HTTP, including a reload
that exceeds the former 10-second limit, ordinary-service defaults, invalid
budgets, continuously progressing responses and completion after timeout.
`tests/service_timeout_http_probe.py` exercises a native MCP HTTP caller through
the actual gateway and backend HTTP transport, using a delayed fake HA server
inside an offline container. The coordinated gateway uses 75 seconds and the
Codex HA registrations require at least 90 seconds. A process-only config parser
check does not establish that existing native callers have refreshed.
