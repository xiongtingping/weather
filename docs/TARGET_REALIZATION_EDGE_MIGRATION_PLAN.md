# Target Realization Field Migration

## Goal

Move backend naming from legacy `edge_*` fields toward the strategy language of
target bucket realization, while keeping existing bot execution, audit tables,
and historical snapshots compatible.

## Non-Negotiables

- No historical backfill in this phase.
- No schema rewrite for `edge_trades` or existing audit tables in this phase.
- New names are merge-filled aliases: never overwrite an existing canonical
  target realization value.
- Legacy `edge_*` fields remain readable until every writer and reader has a
  canonical fallback path and replay tests pass.
- Current runtime semantics are Bayes binary settlement / target realization
  only. Legacy `edge_*` names remain readable for compatibility, but Edge/LCB
  semantics must not trigger orders.

## Canonical Naming

The new canonical prefix is `target_realization_*`.

Primary mapping:

| Canonical | Legacy |
| --- | --- |
| `target_realization_signal_lcb` | `edge_signal_lcb` |
| `target_realization_path_lcb` | `edge_path_p_reach_lcb` |
| `target_realization_tradeability_lcb` | `edge_tradeability_profit_value` |
| `target_realization_execution_lcb` | `edge_tradeability_edge_lcb` |
| `target_realization_tradeability_pass` | `edge_tradeability_pass` |
| `target_realization_tradeability_blocked` | `edge_tradeability_blocked` |
| `target_realization_tradeability_reasons` | `edge_tradeability_reasons` |
| `target_realization_p_realize` | `edge_realizable_p_realize` |
| `target_realization_p_converge` | `edge_realizable_p_converge` |
| `target_execution_margin_entry` | `exec_edge_entry` |
| `target_execution_margin_score` | `exec_edge_score` |
| `target_probability_delta` | `q_edge` |

The full mapping lives in `target_realization_schema.FIELD_ALIASES`.

## Migration Phases

### Phase 1: Read-Path Alias

Status: implemented.

- Add `target_realization_schema.py`.
- Add `apply_target_realization_aliases(row)`.
- `ui/server.py` applies aliases after `_compute_edge_fields_in_place`.
- API rows can now expose both legacy `edge_*` and canonical
  `target_realization_*` fields.
- No database writes or historical mutation.

### Phase 2: New Snapshot Dual-Write

Future guarded change.

- scan/bot writers should call `apply_target_realization_aliases` before
  writing new decision details.
- If a canonical field exists, use it.
- If only legacy exists, merge-fill canonical.
- Keep legacy fields for downstream compatibility.
- Add a new payload meta flag:
  `target_realization_schema_version=target_realization_alias_v1`.

### Phase 3: Canonical-First Readers

Future guarded change.

- Internal readers use `target_realization_*` first, then fallback to `edge_*`.
- UI, logs, and decision summaries stop referencing legacy field names.
- Reject ledgers and audit explainers keep legacy aliases visible only in a
  compatibility/debug section.

### Phase 4: Database Views / Table Alias

Future explicit migration.

- Add views such as `target_realization_trades` over `edge_trades`.
- Do not rename physical tables until live bot, settlement replay, and SSOT
  alignment checks pass under canonical-first readers.

### Phase 5: Legacy Retirement

Future explicit migration.

- Keep legacy reads for archived snapshots.
- Stop emitting legacy fields only after all running daemons, reports, and tests
  no longer require them.

## Verification Gate

Phase 1 is valid only when:

- `python3 -m py_compile target_realization_schema.py ui/server.py` passes.
- `python3 -m pytest tests/test_target_realization_schema.py -q` passes.
- Alias tests prove canonical values are merge-filled, existing canonical values
  are preserved, and `_yes` / `_no` suffixes are supported.

Before Phase 2:

- Run SSOT alignment check.
- Run auto-trade decision audit.
- Confirm no monitor signal path points to the bot signal file.
- Confirm no broad historical write or backfill is being performed.
