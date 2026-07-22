# Known issues and hardening work

Status: active  
Last reviewed: 2026-07-22

This page tracks reliability gaps that remain open after an immediate incident
fix. An item is not closed merely because production traffic recovered; it is
closed only when its prevention, detection, and recovery criteria are verified.

## Streaming lifecycle and concurrency settlement

Status: mitigated in production; hardening remains open

### Impact

Dayboard requests failed first with `Connection error` and later with a friendly
"AI service is busy" message. The latter was a Northgate-generated
`429 CONCURRENCY_LIMIT_EXCEEDED`, not evidence that the upstream model provider
was busy. Successful streamed provider calls could leave active concurrency
leases behind long enough for a later call in the same agent run to be rejected.

### Incident sequence

1. Northgate continued reading after the provider's SSE `[DONE]` event while
   waiting for upstream EOF. A provider connection that stayed open therefore
   kept request finalization and its concurrency lease alive. Commit `703e8b8`
   made the terminal SSE event end the upstream stream.
2. Rebuilding only the base Compose file omitted the platform override and
   detached Northgate from the shared `platform-infra` network. Dayboard could
   no longer resolve the gateway. The host now selects both Compose files, and
   commit `91723b2` documents and preserves the platform topology during upgrades.
3. Ending the stream at `[DONE]` exposed a second cancellation path. Starlette's
   response task cancelled its disconnect listener, and AnyIO cancellation
   propagated into Northgate's finalization work. `asyncio.shield` did not protect
   a child task that inherited the cancelled AnyIO scope. PostgreSQL request
   records remained `started` and Redis leases were released only after later
   task cleanup. Commit `bdb19e2` uses an AnyIO shielded cancellation scope and
   adds a regression test with suspended settlement operations.

These were coupled lifecycle and deployment defects, not three independent
provider outages.

### Why existing verification missed it

- Streaming tests covered incremental delivery, terminal events, and disconnects,
  but mocked settlement methods completed without yielding. They did not reproduce
  cancellation during real PostgreSQL and Redis I/O.
- Policy tests proved atomic admission under parallel requests, but did not run a
  sequence of model/tool/model calls under a low concurrency limit while injecting
  terminal-event cancellation.
- The platform network was an optional Compose override selected by an operator
  command, so a valid base Compose rebuild could produce an invalid application
  topology.
- Health checks proved each process was alive. They did not prove that an
  application container could resolve and call its configured gateway.

### Required hardening

- Add an integration test with real PostgreSQL and Redis that sends several
  sequential streamed requests through one agent-style run under the production
  concurrency limit. Every request must settle and the active lease count must
  return to zero before the next request.
- Add cancellation injection at each async finalization boundary: upstream close,
  attempt settlement, request settlement, cache write, route health update, and
  policy settlement.
- Add a reconciliation job or explicit recovery command for expired Redis leases
  and PostgreSQL request records left in `started`. Define terminal outcomes for
  records whose exact usage can no longer be recovered; do not fabricate usage.
- Export and alert on oldest active lease age, count and age of `started` request
  records, settlement failures, cancelled database connections, and policy
  rejection counts grouped by stable error code.
- Make the production deployment entry point encode the platform topology rather
  than rely on shell state. CI or the upgrade script must validate that Northgate
  joins both its private network and every configured application network.
- Extend readiness or deployment acceptance with a dependency probe from the
  application container to Northgate. Keep the ordinary Northgate readiness
  endpoint provider-neutral.
- Verify that gateway saturation and upstream provider throttling remain distinct
  stable error codes, metrics, and operator messages.
- Run a soak test containing streaming, tool calls, client disconnects, retries,
  and container recreation before declaring this issue closed.

### Closure criteria

This issue can be closed only when all of the following are demonstrated:

- No active concurrency lease or `started` request record remains after the
  sequential streaming integration suite, including injected cancellation cases.
- A failed settlement is observable and recoverable without restarting Northgate
  or manually editing Redis.
- The supported upgrade command preserves application connectivity by construction
  and fails before replacement when the required network topology is absent.
- A production-like soak completes without false
  `CONCURRENCY_LIMIT_EXCEEDED`, leaked database connections, or unexplained
  request records.

