# SSOT Facts Design (SQLite) - Polymarket Weather

## Goal
Make **one canonical "facts" view** the single source of truth (SSOT) for UI/API/auto-trade decisions:

- UI never re-derives "entry candidate / entry reasons / eligibility" from raw fields.
- API always serves a single canonical representation for the same concept.
- Auto-trade uses the same facts fields (or, at minimum, can be audited against them).

## Data Flow (End-to-End)

1) **Ingress (network)**
   - Gamma API: event metadata (slug/title/endDate/outcomes/tokens/volume).
   - CLOB orderbook: best bid/ask + depth.
   - Open-Meteo / NWS: model forecasts.

2) **Snapshots (append-only)**
   - `model_snapshots` (writer: model scan)
   - `market_snapshots` (writer: market refresh)
   - `event_market_ticks` (writer: market refresh / scan) - per outcome+side quotes for freshness + micro-reads

3) **Composer (facts materialize)**
   - Input: latest model_snapshot + market_snapshot (+ optional tick hydration)
   - Output: `event_fact_history` (append-only history) + `event_fact_latest` (pointer)
   - Facts payload (`fact_json`) is the **only** object UI reads.

4) **Serving (UI/API)**
   - `/api/runs` returns `fact_json` (not event_runs) + server-side hydration:
     - `market_updated_at` can be advanced by latest tick.
     - `auto_trade_ssot` is attached server-side for backward compatibility.

5) **Trading**
   - Auto-trade reads facts rows and should align its gating to facts-derived fields.

## Tables & Responsibility

- `model_snapshots`:
  - Keys: (run_date, slug, event_date, created_at)
  - Payload: per-bucket q distribution + model metadata.

- `market_snapshots`:
  - Keys: (run_date, slug, event_date, created_at)
  - Payload: per-outcome orderbook-derived fields (best bid/ask, spread, depth).

- `event_market_ticks`:
  - Keys: (run_date, slug, event_date, outcome, side, created_at)
  - Purpose:
    - quote freshness timestamp (`market_updated_at`)
    - lightweight fallback hydration when composer is skipped/lagging

- `event_fact_history` / `event_fact_latest`:
  - Canonical SSOT facts store.
  - Facts payload contains:
    - `distribution` (merged model+market rows)
    - `auto_trades` (state from `edge_trades`)
    - `auto_trade_ssot` (single canonical "entry candidate + reasons" object)

## Facts Contract (UI should only read these)

- **Freshness**
  - `market_updated_at` (preferred) + `market_updated_source`
  - `model_cache_age_sec`

- **Entry SSOT**
  - `auto_trade_ssot.main_candidate`: the chosen candidate (side/outcome/ask/q/edge_score/edge_min...)
  - `auto_trade_ssot.main_entry_ok`: boolean
  - `auto_trade_ssot.main_entry_reasons`: list[str]
  - UI may render this directly; no recomputation from distribution.

## Concurrency & Locks

- Snapshot writers (`model_scan`, `market_refresh`) should avoid long transactions.
- Facts materialize uses a filesystem lock (`data/facts_materialize.lock/`) to prevent concurrent writers.
- UI reads should not trigger background backfills by default (`PM_FACTS_ALLOW_READ_BACKFILL=0`).

## Migration Strategy

1) Attach `auto_trade_ssot` server-side (on `/api/runs`) for immediate UI SSOT consumption.
2) Gradually persist `auto_trade_ssot` into `event_fact_history.fact_json` during materialize runs.
3) Modify auto-trade gating to optionally trust `auto_trade_ssot` (auditable SSOT).

