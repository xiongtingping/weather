---
status: accepted
authority: L2
owner: clob-order-service
effective_from: 2026-07-20
---

# CLOB Proxy Congestion Guard

## Why This Exists

On 2026-07-20, `clob_order_service` received repeated Polymarket/Cloudflare
`429` / `1015` responses and accumulated about 198 local connections through
the shared Shadowrocket proxy (`127.0.0.1:7891`). That congested the same
proxy path used by Codex and made Codex appear stuck.

## Required Invariants

- Do not remove per-token balance-read deduplication.
- Do not make balance reads unbounded or call `Promise.all` without the shared
  limiter.
- A full balance queue must fail closed; it must not wait indefinitely.
- `429` / `1015` must open the balance-read cooldown. Do not add immediate
  retry loops for these errors.
- Order submission, cancellation, reconciliation, and the Codex proxy route
  must not be placed behind the balance-read queue.

## Defaults

| Setting | Default | Purpose |
| --- | ---: | --- |
| `PM_CLOB_BALANCE_FETCH_MAX_CONCURRENCY` | `2` | Maximum concurrent balance evidence reads. |
| `PM_CLOB_BALANCE_FETCH_MAX_QUEUE` | `24` | Maximum waiting balance reads before fail-closed rejection. |
| `PM_CLOB_BALANCE_RATE_LIMIT_BACKOFF_SEC` | `60` | Initial cooldown after `429` / `1015`. |
| `PM_CLOB_BALANCE_RATE_LIMIT_MAX_BACKOFF_SEC` | `300` | Maximum exponential cooldown. |

## Verification

Read `GET http://127.0.0.1:8765/health`. The fields below must remain within
their bounds:

- `balance_fetch_active <= 2`
- `balance_fetch_queue_depth <= 24`
- `balance_rate_limited_until` is set after `429` / `1015`

Before changing any default, verify that Codex backend requests through the
current Clash Verge mixed port, `http://127.0.0.1:7897`, remain stable while
the CLOB service is active.
