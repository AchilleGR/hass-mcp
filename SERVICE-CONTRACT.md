# Service result compatibility fix (#152)

Branch `fix/service-contract` starts at deployed upstream revision
`716366b31eb1160f9b18321d840ea9ca8a26d8f9`. Its `app/server.py` matches the
original pinned image before this patch (SHA256
`7b09f874c4bdf4222eba775843e63b97f4b4155b2dc2a795345584e1cec37bb4`).

Upstream [PR46](https://github.com/voska/hass-mcp/pull/46), merged as
`eb25405adcd93104dec6dc4d5cbaa2090c38cce5`, fixes list validation by wrapping
the HA response. Latest upstream release v0.6.1 still wraps helper error
dictionaries in `success: true`. The offline probe reproduces that failure.

This backport adds the success envelope and raises MCP `ToolError` for helper
error dictionaries or error-list responses. Dictionary, entity-list and
empty-list successes retain their original payload in `affected_entities`.
HTTP failures, connection failures and timeouts remain errors. No request is
retried. Successful command return does not establish physical device state.
Other tools and the HA helper's behavior are unchanged.

`Dockerfile.service-contract` copies only the patched server over the original
image, preserving Python, dependencies and all 12 tools. The exported discovery
catalog differs only in this tool's return-description prose. The newer release
adds unrelated tools, so it is not used for this compatibility build.

Run `python tests/service_contract_probe.py` inside the image with networking
disabled and dummy `HA_URL=http://invalid.local`, `HA_TOKEN=offline-fixture`.
It mocks only the HA HTTP client and drives the native MCP protocol. Set
`GATEWAY_SOURCE` to the production gateway source to replay each native result
through all nine gateway HTTP routes, checking exact preservation and one
forward per route. Container loopback works with `--network none`; real HA is
unreachable. This replay does not qualify native backend Streamable HTTP startup
or live Home Assistant behavior. The shared gateway's existing tests cover
discovery, concurrent sharing, idle shutdown and failure guards separately.

The nine fixed cases are three success shapes, HTTP401/500, explicit dict/list
errors, timeout and connection refusal. No YAML or Jinja is owned by this patch;
Python branch coverage is unmeasured. Run the existing pytest suite separately.
All new tests run on Sfera, per the HA testing policy. Deployment requires
separate review and host-specific approval; building an offline candidate is
not deployment.
