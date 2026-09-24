"""Materialize pre-event P0 basket selection without execution authority."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any, Mapping

from .cash_phase import BasketCashPhaseInput, CashPhase
from .basket_kernel.state_machine import basket_lifecycle_projection
from .cash_phase_authority import (
    CashPhaseAuthorityManifest,
    load_cash_phase_authority_manifest,
    resolve_active_cash_phase_policy,
)
from .contracts import ContractError, content_sha256
from .market_ingress import (
    derive_official_ws_price_tick_size,
    fold_depth_tail,
    load_official_ws_quotes,
)
from .market_registry import load_market_registry_snapshot
from .observed_high_floor import (
    MARKET_SNAPSHOT_KEY as OBSERVED_HIGH_MARKET_SNAPSHOT_KEY,
)
from .convergence_evidence import load_convergence_evidence
from .production import compile_production_plan
from .regime_materialization import load_regime_artifact
from .production_io import (
    SsotChainState,
    chain_compile_kwargs,
    assemble_market_snapshot,
    derive_policy_maturity_bin,
    assert_current_probability_bridge,
    assert_formal_probability_model_authority,
    load_materialized_probability_envelopes,
    load_production_artifacts,
    resolve_delta_v2_runtime_authority,
    target_registry_source_lane,
)
from .performance_budget import load_performance_budget
from .resource_bounds import assert_connection_target_capacity
from .settlement_time import (
    assert_probability_timezone_matches_market,
    registry_settlement_timezone,
)
from .stages import select_current_stage


SCHEMA = "bayes_basket_target_p0_preselection_v1"
STATE = "PRELIMINARY_NO_LIVE"
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_CANDIDATE"
# Compile-time companion to a preselection, never part of its stored identity.
UNPERSISTED_MARKET_SNAPSHOT_KEY = "_production_market_snapshot"


def _decision_semantic_projection(
    compiled: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the bounded economic decision identity for append deduplication.

    Resident WS stream-confirmation clocks and their derived transport hashes
    prove freshness, but they are not a new basket decision.  Persist another
    append-only preselection only when an input that can change the selected
    support, target inventory, candidate action, cash phase, policy, or
    scenario economics actually changes.
    """

    plan = compiled.get("plan")
    if not isinstance(plan, Mapping):
        # A blocked compile carries no plan by construction: the runtime's
        # ``_blocked`` answer names its reasons and leaves ``plan`` None.
        # Its identity is the status and those reasons -- every plan field
        # below projects to None -- and refusing to project it turned a
        # HOLD the runtime had already named (support_empty, no solvable
        # window, certainty gate) into ``p0_preselection_compiled_plan_missing``
        # one layer up: fail-closed, no preselection row, no decision.
        # Measured 2026-09-02 07:40Z, 15 forward station-days a cycle.  A
        # compile that claims to be ready or to hold without a plan is still
        # the contract breach it always was.
        if str(compiled.get("status") or "") == "blocked":
            plan = {}
        else:
            raise ContractError("p0_preselection_compiled_plan_missing")
    cash_phase = compiled.get("cash_phase_decision")
    if not isinstance(cash_phase, Mapping):
        cash_phase = {}
    policy = compiled.get("resolved_station_policy")
    if not isinstance(policy, Mapping):
        policy = {}
    action_selection = plan.get("delta_v2_action_selection")
    if not isinstance(action_selection, Mapping):
        action_selection = {}
    target_solution = plan.get("target_solution")
    if not isinstance(target_solution, Mapping):
        target_solution = {}
    # Charter 171: the compile stores this object twice, verbatim, at both
    # levels -- 4,353 bytes a row across 119,596 rows.  The compact rebuild
    # of 09-21 stripped the inner copy from the rows stored before it; the
    # writer still stores both (changing it waits on the runtime readers of
    # ``delta_plan["delta_v2_audit"]``), so read the inner one when it is
    # there and the outer one when it is not.  Measured over
    # 1,204 rows spanning the whole table: 1,184 carried both and none of
    # them differed, 20 carried only the outer, and none carried only the
    # inner -- which is what makes dropping it lossless.
    delta_audit = plan.get("delta_v2_audit")
    if not isinstance(delta_audit, Mapping) and plan:
        # ``and plan``, not a bare fallback.  This projection is what
        # ``plan_sha256`` hashes, and a re-commit of the same preselection_id
        # compares that hash and raises ``p0_preselection_content_conflict``
        # when it moved -- so the projection of every already-stored row has
        # to come out byte for byte the same.  A BLOCKED compile carries
        # ``plan = {}`` and has always projected an empty audit here; falling
        # back for it would change those rows' hash.  A ready plan with the
        # inner copy stripped has its other keys, so ``plan`` is truthy and
        # the outer copy is read instead.
        delta_audit = compiled.get("delta_v2_audit")
    if not isinstance(delta_audit, Mapping):
        delta_audit = {}
    return {
        "schema": "bayes_basket_target_p0_decision_semantic_v1",
        "status": compiled.get("status"),
        "reason_codes": compiled.get("reason_codes"),
        "cadence_decision": compiled.get("cadence_decision"),
        "cash_phase": {
            "clock_phase": cash_phase.get("clock_phase"),
            "current_phase": cash_phase.get("current_phase"),
            "next_phase": cash_phase.get("next_phase"),
            "policy_version": cash_phase.get("policy_version"),
            "reason_code": cash_phase.get("reason_code"),
        },
        "policy": {
            "activation_id": policy.get("activation_id"),
            "authority_scope": policy.get("authority_scope"),
            "policy_id": policy.get("policy_id"),
            "policy_sha256": policy.get("policy_sha256"),
            "global_prior_id": policy.get("global_prior_id"),
            "target_artifact_id": policy.get("target_artifact_id"),
            "reserve_artifact_id": policy.get("reserve_artifact_id"),
            "cadence_artifact_id": policy.get("cadence_artifact_id"),
        },
        "plan": {
            "state": plan.get("state"),
            "basket_lifecycle": basket_lifecycle_projection(
                plan,
                required=False,
            ),
            "reason_codes": plan.get("reason_codes"),
            "center_bucket": plan.get("center_bucket"),
            "runner_up_bucket": plan.get("runner_up_bucket"),
            "center_stable": plan.get("center_stable"),
            "direction": plan.get("direction"),
            "candidate_support": plan.get("candidate_support"),
            "final_support": plan.get("final_support"),
            "selected_buckets": plan.get("selected_buckets"),
            "excluded_candidate_buckets": plan.get(
                "excluded_candidate_buckets"
            ),
            "underreach_risk_buckets": plan.get(
                "underreach_risk_buckets"
            ),
            "upside_breakthrough_risk_buckets": plan.get(
                "upside_breakthrough_risk_buckets"
            ),
            "maker_candidate_diagnostics": plan.get(
                "maker_candidate_diagnostics"
            ),
            "target_shares_by_bucket": target_solution.get(
                "target_shares_by_bucket"
            ),
            "target_cost_by_bucket": target_solution.get(
                "target_cost_by_bucket"
            ),
            "target_value": target_solution.get("target_value"),
            "selected_actions": action_selection.get("selected_actions"),
            "blockers": action_selection.get("blockers"),
            "hold_selected": action_selection.get("hold_selected"),
            "basket_complete": action_selection.get("basket_complete"),
            "delta_basket_score_v2": delta_audit.get(
                "delta_basket_score_v2"
            ),
            "policy_artifact_id": delta_audit.get("policy_artifact_id"),
            "policy_artifact_sha256": delta_audit.get(
                "policy_artifact_sha256"
            ),
            "fill_distribution_artifact_sha256": delta_audit.get(
                "fill_distribution_artifact_sha256"
            ),
            "scenario_pnl_usdc": plan.get("scenario_pnl_usdc"),
            "expected_pnl_usdc": plan.get("expected_pnl_usdc"),
            "profit_floor_usdc": plan.get("profit_floor_usdc"),
            "cvar_usdc": plan.get("cvar_usdc"),
            "total_target_shares": plan.get("total_target_shares"),
            "total_delta_notional_usdc": plan.get(
                "total_delta_notional_usdc"
            ),
        },
    }


def _utc(value: Any, *, reason: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(reason) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(reason)
    return parsed.astimezone(timezone.utc)


def _registry_rows(
    registry: Mapping[str, Any],
    *,
    station_id: str,
    event_date: str,
) -> list[dict[str, Any]]:
    rows = [
        dict(row)
        for row in registry.get("rows", [])
        if isinstance(row, Mapping)
        and str(row.get("station_id") or "") == station_id
        and str(row.get("event_date") or "") == event_date
    ]
    if not rows:
        raise ContractError("p0_preselection_registry_scope_missing")
    market_timezone = registry_settlement_timezone(
        rows,
        station_id=station_id,
        event_date=event_date,
    )
    assert_probability_timezone_matches_market(
        probability_timezone=str(
            next(
                (
                    row.get("settlement_timezone")
                    for row in rows
                    if row.get("settlement_timezone")
                ),
                "",
            )
        ),
        market_timezone=market_timezone,
    )
    return rows


def latest_visible_p0_station_ids(
    conn: sqlite3.Connection,
    *,
    event_date: str,
    evaluation_at_utc: str,
) -> list[str]:
    """Return each station whose own latest visible checkpoint is fresh.

    The correlated maximum is deliberate: a newer checkpoint at one station
    must never hide another station's independently valid checkpoint.
    """

    evaluation = _utc(
        evaluation_at_utc,
        reason="p0_preselection_evaluation_at_invalid",
    )
    latest_by_station: dict[str, tuple[datetime, datetime]] = {}
    for station_id, decision_at, fresh_until in conn.execute(
        "SELECT station_id,decision_at_utc,fresh_until_utc "
        "FROM basket_probability_envelope "
        "WHERE event_date=? AND state='COMMITTED' ORDER BY station_id,rowid",
        (event_date,),
    ).fetchall():
        station = str(station_id or "").strip()
        if not station:
            continue
        decision = _utc(
            decision_at,
            reason="p0_preselection_decision_at_invalid",
        )
        freshness = _utc(
            fresh_until,
            reason="p0_preselection_fresh_until_invalid",
        )
        if decision > evaluation:
            continue
        current = latest_by_station.get(station)
        if current is None or decision > current[0]:
            latest_by_station[station] = (decision, freshness)
    return sorted(
        station
        for station, (decision, fresh_until) in latest_by_station.items()
        if decision <= evaluation < fresh_until
    )


def _forward_min_order_size(
    registry_row: Mapping[str, Any], raw_registry: Any
) -> float:
    """The market's stated minimum lot, or 0.0 when the row states none."""

    raw = registry_row.get("order_min_size")
    if raw is None and isinstance(raw_registry, Mapping):
        raw = raw_registry.get("order_min_size")
    if raw is None or isinstance(raw, bool):
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value) or value < 0.0:
        return 0.0
    return value


def _forward_tick_size(registry_row: Mapping[str, Any]) -> float | None:
    """The market's stated price increment for this token, or None.

    Read from the projected column, falling back to the raw registry blob
    the same way the minimum lot beside it does, because a snapshot taken
    before the field was projected still carries it in ``registry_json``.
    """

    raw = registry_row.get("order_price_min_tick_size")
    if raw is None:
        blob = registry_row.get("registry_json")
        if isinstance(blob, str):
            try:
                blob = json.loads(blob)
            except (TypeError, ValueError):
                blob = None
        if isinstance(blob, Mapping):
            raw = blob.get("order_price_min_tick_size")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or not 0.0 < value < 1.0:
        return None
    return value


FORWARD_BOOK_SCHEMA = "bayes_basket_target_p0_forward_book_v1"
# One rung of a forward book: what the venue offered on that bucket, and
# nothing the station-day holds.  Holdings are layered on by the assembler.
FORWARD_BOOK_RUNG_KEYS = (
    "bucket", "ordinal", "unmaterialized_book", "bid_price", "ask_levels",
    "ask_available", "bid_levels", "price_tick_size", "tick_size_source",
    "min_order_size", "maker_queue_ahead_shares", "maker_queue_known",
    "token_id",
)


def _planning_inputs(
    planning_budget_usdc: float, book_equity_usdc: float | None,
) -> tuple[float, float | None]:
    try:
        planning_budget = float(planning_budget_usdc)
    except (TypeError, ValueError) as exc:
        raise ContractError(
            "p0_preselection_planning_budget_invalid"
        ) from exc
    if not math.isfinite(planning_budget) or planning_budget < 0.0:
        raise ContractError("p0_preselection_planning_budget_invalid")
    # The book's equity is a different number from this scope's planning
    # budget, and the sizing kernel needs both.  ``remaining_budget_usdc`` is
    # what this station-day may plan against -- the portfolio quota, a
    # quarter of the pool at most and often far less -- while the per-scope
    # stake cap (2% of the book, charter of 2026-08-26) is a share of the
    # whole book.  ``runtime._compile_target_plan`` falls back to the budget
    # when no equity is on the snapshot, so on this lane the 2% has been
    # riding on the quota instead of the pool: 514.29 x 0.25 x 0.02 x 0.6 =
    # 1.54 USDC for a P0 open, below one exchange lot on any leg above 0.30,
    # and every positive-edge forward basket of 2026-09-02 (6 of 6) was
    # refused for a missing leg it could have afforded at 6.17.  The forward
    # loop passes the pool balance here; nothing is derived from it.
    book_equity: float | None = None
    if book_equity_usdc is not None:
        try:
            book_equity = float(book_equity_usdc)
        except (TypeError, ValueError) as exc:
            raise ContractError(
                "p0_preselection_book_equity_invalid"
            ) from exc
        if not math.isfinite(book_equity) or book_equity < 0.0:
            raise ContractError("p0_preselection_book_equity_invalid")
    return planning_budget, book_equity


def _market_snapshot(
    conn: sqlite3.Connection,
    *,
    envelope: Any,
    registry_rows: list[Mapping[str, Any]],
    quotes: Mapping[str, Mapping[str, Any]],
    manifest: Mapping[str, Any],
    evaluation_at_utc: str,
    planning_budget_usdc: float = 0.0,
    book_equity_usdc: float | None = None,
    ledger_holdings: Mapping[str, Any] | None = None,
    account_capital: Mapping[str, Any] | None = None,
    chain_state: Any = None,
) -> dict[str, Any]:
    """The forward snapshot from the live stream: read the book, then assemble.

    Two halves since 2026-09-24 (user ruling 「账本对齐」): the book is what
    the stream offered and is validated here once; the snapshot is that book
    plus the station-day's holdings and history.  A replay of a recorded
    decision and the paper ledger hand in the recorded book instead
    (``forward_book_from_snapshot_row``) and share the second half.
    """

    _planning_inputs(planning_budget_usdc, book_equity_usdc)
    book = forward_book_from_ws(
        envelope=envelope,
        registry_rows=registry_rows,
        quotes=quotes,
        manifest=manifest,
        evaluation_at_utc=evaluation_at_utc,
    )
    return _forward_market_snapshot(
        conn,
        envelope=envelope,
        book=book,
        evaluation_at_utc=evaluation_at_utc,
        planning_budget_usdc=planning_budget_usdc,
        book_equity_usdc=book_equity_usdc,
        ledger_holdings=ledger_holdings,
        account_capital=account_capital,
        chain_state=chain_state,
    )


def forward_book_from_ws(
    *,
    envelope: Any,
    registry_rows: list[Mapping[str, Any]],
    quotes: Mapping[str, Mapping[str, Any]],
    manifest: Mapping[str, Any],
    evaluation_at_utc: str,
) -> dict[str, Any]:
    """Validate one scope's forward book off the official WS stream.

    Everything here is a property of the venue at ``evaluation_at_utc``:
    manifest boundary and binding, registry support, strict-as-of clocks,
    the tick and the folded ladders.  Nothing here reads the station-day's
    holdings or history.
    """

    evaluation = _utc(
        evaluation_at_utc,
        reason="p0_preselection_evaluation_at_invalid",
    )
    if (
        manifest.get("schema") != "clob_ws_latest_quote_snapshot_v1"
        or manifest.get("state") != "connected"
        or manifest.get("source") != "official_polymarket_clob_websocket"
        or manifest.get("evidence_only") is not True
        or manifest.get("authorizes_order") is not False
    ):
        raise ContractError("p0_preselection_ws_manifest_boundary_invalid")
    registry_timezone = registry_settlement_timezone(
        registry_rows,
        station_id=str(envelope.station_id),
        event_date=str(envelope.event_date),
    )
    assert_probability_timezone_matches_market(
        probability_timezone=str(envelope.settlement_timezone),
        market_timezone=registry_timezone,
    )
    assert_probability_timezone_matches_market(
        probability_timezone=str(manifest.get("target_timezone") or ""),
        market_timezone=registry_timezone,
    )
    manifest_as_of = _utc(
        manifest.get("as_of_utc"),
        reason="p0_preselection_manifest_as_of_invalid",
    )
    connection_observed = _utc(
        manifest.get("connection_observed_at"),
        reason="p0_preselection_manifest_observed_at_invalid",
    )
    if manifest_as_of > evaluation or connection_observed > evaluation:
        raise ContractError("p0_preselection_manifest_not_strict_asof")
    maximum_age = float(
        load_performance_budget()["limits"]["max_ws_snapshot_age_sec"]
    )
    if (evaluation - connection_observed).total_seconds() > maximum_age:
        raise ContractError("p0_preselection_manifest_stale")
    manifest_sha = str(manifest.get("snapshot_sha256") or "")
    manifest_core = {
        key: value for key, value in manifest.items() if key != "snapshot_sha256"
    }
    if manifest_sha != content_sha256(manifest_core):
        raise ContractError("p0_preselection_manifest_content_sha_mismatch")
    if manifest.get("quote_storage") != "inline_v1":
        raise ContractError("p0_preselection_inline_ws_manifest_required")
    manifest_quotes = manifest.get("quotes_by_token")
    if not isinstance(manifest_quotes, Mapping):
        raise ContractError("p0_preselection_manifest_quotes_missing")
    by_bucket = {str(row.get("outcome") or ""): row for row in registry_rows}
    if set(by_bucket) != set(envelope.probabilities):
        raise ContractError("p0_preselection_registry_probability_support_mismatch")
    buckets = []
    quote_times = []
    market_labels = set()
    # No scope-wide price pooling here any more.  It resolved one token's
    # tick off a sibling's book on the premise that a market has one grid;
    # the registry states a grid per token and this venue really does mix
    # them, so each bucket now asks the registry about itself and infers
    # only from its own levels.  See derive_official_ws_price_tick_size.
    declared_unmaterialized = {
        str(value)
        for value in manifest.get("unmaterialized_tokens") or []
        if str(value)
    }
    for bucket in sorted(envelope.probabilities, key=envelope.bucket_ordinals.__getitem__):
        registry_row = by_bucket[bucket]
        token_id = str(registry_row.get("yes_token") or "")
        quote = quotes.get(token_id)
        if quote is None:
            # A listing-day book nobody has quoted yet.  The publisher
            # subscribes at listing (charter 48) and names such tokens
            # explicitly; a named bookless bucket is a legal market state --
            # unbuyable this cycle, never a scope blocker.  An *unnamed*
            # missing token is still a defect: the scope claimed a bucket
            # the stream is not carrying.
            if (
                token_id not in declared_unmaterialized
                and token_id not in manifest_quotes
            ):
                raise ContractError(
                    "p0_preselection_official_ws_token_missing"
                )
            buckets.append({
                "bucket": bucket,
                "ordinal": int(envelope.bucket_ordinals[bucket]),
                "unmaterialized_book": True,
                "bid_price": 0.0,
                "ask_levels": [],
                "ask_available": False,
                "bid_levels": [],
                "price_tick_size": 0.0,
                "tick_size_source": "UNMATERIALIZED_BOOK",
                "min_order_size": 0.0,
                "maker_queue_ahead_shares": 0.0,
                "maker_queue_known": False,
                "token_id": token_id,
            })
            raw_registry = registry_row.get("registry_json")
            if isinstance(raw_registry, str):
                raw_registry = json.loads(raw_registry)
            if isinstance(raw_registry, Mapping):
                market_labels.add(str(
                    raw_registry.get("event_id")
                    or raw_registry.get("slug")
                    or ""
                ))
            continue
        manifest_quote = manifest_quotes.get(token_id)
        if (
            not isinstance(manifest_quote, Mapping)
            or content_sha256(dict(manifest_quote)) != content_sha256(dict(quote))
        ):
            raise ContractError("p0_preselection_quote_manifest_binding_invalid")
        asks = quote.get("asks")
        bids = quote.get("bids")
        if not isinstance(asks, list) or not isinstance(bids, list):
            raise ContractError("p0_preselection_official_ws_book_invalid")
        ask_levels = sorted(
            (
                {"price": float(level["price"]), "shares": float(level["size"])}
                for level in asks
                if isinstance(level, Mapping)
                and float(level.get("price", 0.0)) > 0.0
                and float(level.get("size", 0.0)) > 0.0
            ),
            key=lambda item: item["price"],
        )
        bid_levels = sorted(
            (
                {"price": float(level["price"]), "shares": float(level["size"])}
                for level in (bids or [])
                if isinstance(level, Mapping)
                and float(level.get("price", 0.0)) > 0.0
                and float(level.get("size", 0.0)) > 0.0
            ),
            key=lambda item: item["price"],
            reverse=True,
        )
        if not ask_levels and not bid_levels:
            raise ContractError("p0_preselection_official_ws_book_empty")
        price_tick_size, tick_size_source = (
            derive_official_ws_price_tick_size(
                quote,
                asks=ask_levels,
                bids=bid_levels,
                registry_tick=_forward_tick_size(registry_row),
            )
        )
        # Charter 168, the second writer (2026-09-21).  This lane assembles
        # its own ladders from the stream and ``production_worker`` writes
        # them to ``basket_official_ws_snapshot`` -- the ingress fold never
        # reached them: 18% of new snapshot rows, 34% of the column's bytes.
        # Same fold, same place as the ingress: after the tick has been
        # derived from the real ladder, with that tick, outward.
        ask_levels = fold_depth_tail(ask_levels, ascending=True, tick=price_tick_size)
        bid_levels = fold_depth_tail(bid_levels, ascending=False, tick=price_tick_size)
        observed = str(quote.get("book_observed_at") or quote.get("created_at") or "")
        quote_observed_at = _utc(
            observed,
            reason="p0_preselection_quote_observed_at_missing",
        )
        stream_confirmed_raw = str(
            quote.get("_stream_confirmed_at") or observed
        )
        stream_confirmed_at = _utc(
            stream_confirmed_raw,
            reason="p0_preselection_stream_confirmation_missing",
        )
        if stream_confirmed_at < quote_observed_at:
            raise ContractError(
                "p0_preselection_stream_confirmation_predates_book"
            )
        probability_decision_raw = str(
            getattr(envelope, "decision_at_utc", "") or ""
        )
        if (
            probability_decision_raw
            and stream_confirmed_at
            < _utc(
                probability_decision_raw,
                reason="p0_preselection_probability_decision_at_invalid",
            )
        ):
            raise ContractError(
                "p0_preselection_quote_predates_probability"
            )
        if (
            quote_observed_at > evaluation
            or stream_confirmed_at > evaluation
        ):
            raise ContractError("p0_preselection_quote_not_strict_asof")
        if (evaluation - stream_confirmed_at).total_seconds() > maximum_age:
            raise ContractError("p0_preselection_quote_stale")
        quote_times.append(observed)
        raw_registry = registry_row.get("registry_json")
        if isinstance(raw_registry, str):
            raw_registry = json.loads(raw_registry)
        if isinstance(raw_registry, Mapping):
            market_labels.add(str(raw_registry.get("event_id") or raw_registry.get("slug") or ""))
        buckets.append({
            "bucket": bucket,
            "ordinal": int(envelope.bucket_ordinals[bucket]),
            "bid_price": float(bid_levels[0]["price"]) if bid_levels else 0.0,
            "ask_levels": ask_levels,
            "ask_available": bool(ask_levels),
            "bid_levels": bid_levels,
            "price_tick_size": price_tick_size,
            "tick_size_source": tick_size_source,
            # The exchange's minimum lot, off the same registry row the main
            # lane reads it from.  This lane never carried it, so every
            # forward bucket reached the sizing kernel with a lot of 0 -- no
            # rung could be raised to a lot (charter 70) and no sub-lot rung
            # was named as such.  The mirror rows this lane reads keep the
            # value inside ``registry_json`` (the main lane's loader lifts it
            # to the top level; this one does not), so both places are read.
            # Tolerantly: a snapshot that predates the field states no lot,
            # which the kernel's contract already defines as 0, rather than
            # refusing the whole forward compile.
            "min_order_size": _forward_min_order_size(registry_row, raw_registry),
            "maker_queue_ahead_shares": 0.0,
            "maker_queue_known": False,
            "token_id": token_id,
        })
    if len(market_labels) != 1 or not next(iter(market_labels)):
        raise ContractError("p0_preselection_market_identity_ambiguous")
    if not quote_times:
        # Every bucket of the scope is bookless.  There is nothing to price
        # against and no quote clock to anchor strict-as-of, so the scope is
        # not compilable yet -- distinct from a missing token, which is a
        # stream defect rather than an empty market.
        raise ContractError("p0_preselection_official_ws_scope_bookless")
    quote_observed = max(
        quote_times,
        key=lambda value: _utc(
            value,
            reason="p0_preselection_quote_observed_at_missing",
        ),
    )
    return {
        "schema": FORWARD_BOOK_SCHEMA,
        "station_id": str(envelope.station_id),
        "event_date": str(envelope.event_date),
        "settlement_timezone": registry_timezone,
        "market_id": next(iter(market_labels)),
        "quote_observed_at": quote_observed,
        "manifest_sha256": manifest_sha,
        "rungs": buckets,
    }


def forward_book_from_snapshot_row(
    row: Mapping[str, Any], *, manifest_sha256: str = "",
) -> dict[str, Any]:
    """The forward book a decision priced against, from its committed quote row.

    The quote row is the book as the stream offered it plus the holdings the
    decision layered on (and the materializer's ``bid_available``); the
    rungs keep only the venue's fields, so the station-day's holdings come
    from whoever replays (the SSOT's chain state, or the paper ledger's).
    """

    raw = row.get("buckets_json")
    rungs_raw = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(rungs_raw, list) or not rungs_raw:
        raise ContractError("p0_forward_book_row_buckets_invalid")
    rungs = [
        {key: item[key] for key in FORWARD_BOOK_RUNG_KEYS if key in item}
        for item in rungs_raw
    ]
    return {
        "schema": FORWARD_BOOK_SCHEMA,
        "station_id": str(row["station_id"]),
        "event_date": str(row["event_date"]),
        "settlement_timezone": str(row["settlement_timezone"]),
        "market_id": str(row["market_id"]),
        "quote_observed_at": str(row["quote_observed_at"]),
        # The stream manifest is not on the quote row; a replay that has the
        # recorded preselection names it, the paper ledger leaves it empty.
        "manifest_sha256": str(manifest_sha256 or ""),
        "rungs": rungs,
    }


def _forward_market_snapshot(
    conn: sqlite3.Connection,
    *,
    envelope: Any,
    book: Mapping[str, Any],
    evaluation_at_utc: str,
    planning_budget_usdc: float = 0.0,
    book_equity_usdc: float | None = None,
    ledger_holdings: Mapping[str, Any] | None = None,
    account_capital: Mapping[str, Any] | None = None,
    chain_state: Any = None,
) -> dict[str, Any]:
    """One forward book plus the station-day's holdings, assembled."""

    evaluation = _utc(
        evaluation_at_utc,
        reason="p0_preselection_evaluation_at_invalid",
    )
    planning_budget, book_equity = _planning_inputs(
        planning_budget_usdc, book_equity_usdc,
    )
    if (
        book.get("schema") != FORWARD_BOOK_SCHEMA
        or str(book.get("station_id") or "") != str(envelope.station_id)
        or str(book.get("event_date") or "") != str(envelope.event_date)
    ):
        raise ContractError("p0_forward_book_scope_mismatch")
    settlement_timezone = str(book.get("settlement_timezone") or "")
    assert_probability_timezone_matches_market(
        probability_timezone=str(envelope.settlement_timezone),
        market_timezone=settlement_timezone,
    )
    rungs = [dict(rung) for rung in book.get("rungs") or []]
    if {str(rung.get("bucket") or "") for rung in rungs} != set(envelope.probabilities) or any(
        int(rung["ordinal"]) != int(envelope.bucket_ordinals[str(rung["bucket"])])
        for rung in rungs
    ) or len(rungs) != len(envelope.probabilities):
        raise ContractError("p0_preselection_registry_probability_support_mismatch")
    quote_observed = str(book.get("quote_observed_at") or "")
    if _utc(
        quote_observed, reason="p0_preselection_quote_observed_at_missing",
    ) > evaluation:
        raise ContractError("p0_preselection_quote_not_strict_asof")
    maximum_age = float(
        load_performance_budget()["limits"]["max_ws_snapshot_age_sec"]
    )
    market_id = str(book.get("market_id") or "")
    if not market_id:
        raise ContractError("p0_preselection_market_identity_ambiguous")
    # What the reconciliation ledger says this scope already holds (charter
    # 83: a settled live fill is inventory).  A forward day has no resident
    # ingress, so this snapshot is the only place its holdings can enter the
    # kernel; until 2026-09-05 it wrote every bucket held-zero, and the four
    # baskets the venue had half-refused that morning (LTAC, LFPB, SAEZ,
    # ZGSZ) were re-judged as fresh three-leg baskets at the moved prices
    # -- HOLD -- instead of as the legs they were missing.  The holdings
    # enter the evidence identity: the same book held differently is a
    # different quote row, because the bundle deduplicates by snapshot id
    # and refuses a second row whose bytes disagree.
    holdings_by_token: dict[str, dict[str, float]] = {}
    if ledger_holdings is not None:
        raw_holdings = ledger_holdings.get("holdings")
        if not isinstance(raw_holdings, Mapping):
            raise ContractError("p0_preselection_ledger_holdings_invalid")
        registry_tokens = {str(rung["token_id"]) for rung in rungs}
        for token_id, entry in raw_holdings.items():
            if not isinstance(entry, Mapping):
                raise ContractError("p0_preselection_ledger_holdings_invalid")
            try:
                held_shares = float(entry.get("shares", 0.0))
                held_cost = float(entry.get("cost_usdc", 0.0))
            except (TypeError, ValueError) as exc:
                raise ContractError(
                    "p0_preselection_ledger_holdings_invalid"
                ) from exc
            if (
                not math.isfinite(held_shares)
                or not math.isfinite(held_cost)
                or held_shares < 0.0
                or held_cost < 0.0
            ):
                raise ContractError("p0_preselection_ledger_holdings_invalid")
            if held_shares <= 0.0:
                continue
            if str(token_id) not in registry_tokens:
                # A settled fill on a token this scope's registry no longer
                # names is a scope conflict, the same one the resident
                # inventory producer refuses; pricing the day without it
                # would silently drop a position.
                raise ContractError(
                    "p0_preselection_ledger_holdings_token_unknown"
                )
            holdings_by_token[str(token_id)] = {
                "shares": held_shares,
                "cost_usdc": held_cost,
            }
    holdings_sha256 = (
        content_sha256({
            "schema": "bayes_basket_target_p0_ledger_holdings_v1",
            "holdings": holdings_by_token,
        })
        if holdings_by_token
        else ""
    )
    _no_holding = {"shares": 0.0, "cost_usdc": 0.0}
    rows = []
    for rung in rungs:
        token_id = str(rung["token_id"])
        held = holdings_by_token.get(token_id, _no_holding)
        row = {key: value for key, value in rung.items() if key != "token_id"}
        row.update({
            # Held is held, book or no book.
            "filled_shares": held["shares"],
            "filled_cost_usdc": held["cost_usdc"],
            "pending_shares": 0.0,
            "pending_notional_usdc": 0.0,
            "active_before": False,
            "token_id": token_id,
        })
        rows.append(row)
    economic_quote_sha256 = content_sha256({
        "schema": "bayes_basket_target_p0_economic_quote_v1",
        "station_id": envelope.station_id,
        "event_date": envelope.event_date,
        "settlement_timezone": settlement_timezone,
        "market_id": market_id,
        "buckets": [
            {
                key: value
                for key, value in bucket.items()
                if key in {
                    "bucket",
                    "token_id",
                    "ask_levels",
                    "bid_levels",
                    "price_tick_size",
                    "tick_size_source",
                }
            }
            for bucket in rows
        ],
    })
    # The quote identity is the book's economic bytes (transport clocks and
    # ids stay out of it); the holdings ride beside it, so a fill makes a
    # new evidence row for an unchanged book rather than a conflicting one.
    quote_version = "p0_ws_" + economic_quote_sha256[:32]
    if holdings_by_token:
        quote_version += "_h" + holdings_sha256[:12]
    # Charter 141 / 136 (五) / 143: everything that is a property of the
    # station-day rather than of this source -- the real account, the chain's
    # send history, the last decision's core -- is attached by the one
    # assembler both builders share.  This builder carried none of them
    # until 2026-09-17, and it is the builder of every P0-M and P1 decision.
    return assemble_market_snapshot(
        conn,
        station_id=str(envelope.station_id),
        event_date=str(envelope.event_date),
        evaluation_at_utc=evaluation_at_utc,
        identity={
            "snapshot_id": quote_version,
            "sequence": 0,
            "observed_at": quote_observed,
            "market_id": market_id,
            "settlement_timezone": settlement_timezone,
            "quote_version": quote_version,
            "official_ws_quote_sha256": economic_quote_sha256,
            "economic_quote_sha256": economic_quote_sha256,
            "quote_observed_at": quote_observed,
            # A streaming book has no exchange-stated expiry; what bounds its
            # actionability is the same strict-as-of freshness budget that
            # gates staleness above.  The forward evidence binding requires
            # an explicit expiry clock (decision must precede it), and
            # leaving the field absent failed every listing-day stage-open
            # as an invalid timestamp -- the whole fleet fell back to
            # preliminary records.
            "quote_expires_at": (
                _utc(
                    quote_observed,
                    reason="p0_preselection_quote_observed_at_missing",
                )
                + timedelta(seconds=maximum_age)
            ).isoformat().replace("+00:00", "Z"),
            "quote_source": "OFFICIAL_CLOB_WS",
            # q0 is the empty book; a scope the ledger says is held names the
            # holdings it was priced on, and the forward evidence trio writes
            # that inventory row under this version.
            "inventory_version": (
                "p0_preselection_ledger_" + holdings_sha256[:16]
                if holdings_by_token
                else "p0_preselection_q0"
            ),
            "order_state_version": "p0_preselection_known_zero",
        },
        buckets=rows,
        # This is a no-live planning ceiling, not reserved or executable
        # capital.  Keeping it distinct from capital_authorized allows P0
        # and P0-M to compute a real DeltaBasketScore target before P1
        # without creating an intent, reservation, or order.
        remaining_budget_usdc=planning_budget,
        cash_phase_input=None,
        account_capital=account_capital,
        extra={
            **(
                {
                    "ledger_holdings": holdings_by_token,
                    "ledger_holdings_sha256": holdings_sha256,
                }
                if holdings_by_token
                else {}
            ),
            # Absent when the caller has no pool to report; the compiler
            # then sizes off the budget as before.  Never persisted: the
            # materialized quote row picks its columns by name, so this
            # rides in memory only.  The real account, when the store has
            # one, overrides it in the assembler.
            **(
                {"book_equity_usdc": book_equity}
                if book_equity is not None
                else {}
            ),
        },
        chain_state=chain_state,
    )


def _bind_scope_market_evidence(
    conn: sqlite3.Connection,
    *,
    envelope: Any,
    artifacts: Any,
    station_id: str,
    event_date: str,
    evaluation_at_utc: str,
    registry_snapshot: Mapping[str, Any] | None,
    ws_manifest: Mapping[str, Any] | None,
    ws_quotes: Mapping[str, Mapping[str, Any]] | None,
    planning_budget_usdc: float,
    cash_phase_authority_manifest: (
        CashPhaseAuthorityManifest | Mapping[str, Any] | None
    ) = None,
    book_equity_usdc: float | None = None,
    account_capital: Mapping[str, Any] | None,
    chain_state: Any = None,
    forward_book: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], Any, BasketCashPhaseInput]:
    """Resolve the scope's book and the cash-phase input bound into it.

    ``account_capital`` is charter 141's real-account read
    (``production_io.account_capital_for_snapshot``) for a caller that will
    SIZE on this snapshot, and ``None`` for the evidence-only pass, which
    writes the forward evidence trio and sizes nothing.  Required, not
    defaulted: forgetting it is how the stage lanes sized without the
    whole-book bound until 2026-09-17.

    This is the evidence half of a preselection, and the only builder of it:
    the compile path and the evidence-only path below must produce the same
    bytes for the same quote, because ``insert_market_evidence_bundle``
    deduplicates by ``snapshot_id`` and rejects a second row whose content
    hash disagrees.  Two builders would have made that conflict inevitable.
    """

    authority_manifest = cash_phase_authority_manifest
    if authority_manifest is None:
        authority_manifest = load_cash_phase_authority_manifest(
            conn,
            station_id=station_id,
            observed_at_utc=evaluation_at_utc,
            required_authority_scope=AUTHORITY_SCOPE,
        )
    cash_policy = resolve_active_cash_phase_policy(
        artifacts.cash_phase_policies,
        authority_manifest=authority_manifest,
        station_id=station_id,
        observed_at_utc=evaluation_at_utc,
        required_authority_scope=AUTHORITY_SCOPE,
    )
    # The book comes from the live stream, or -- a replay of a recorded
    # decision, the paper ledger -- is handed in already read
    # (``forward_book_from_snapshot_row``).  Never both.
    live_sources = (registry_snapshot, ws_manifest, ws_quotes)
    if forward_book is None:
        if any(source is None for source in live_sources):
            raise ContractError("p0_preselection_book_source_missing")
        forward_book = forward_book_from_ws(
            envelope=envelope,
            registry_rows=_registry_rows(
                registry_snapshot,
                station_id=station_id,
                event_date=event_date,
            ),
            quotes=ws_quotes,
            manifest=ws_manifest,
            evaluation_at_utc=evaluation_at_utc,
        )
    elif any(source is not None for source in live_sources):
        raise ContractError("p0_preselection_book_source_ambiguous")
    market = _forward_market_snapshot(
        conn,
        envelope=envelope,
        book=forward_book,
        evaluation_at_utc=evaluation_at_utc,
        planning_budget_usdc=planning_budget_usdc,
        book_equity_usdc=book_equity_usdc,
        # Charter 83: what the reconciliation ledger has settled for this
        # scope is inventory, on a forward day as on a started one -- the
        # basket's own fills only, through the one chain-state reader.
        ledger_holdings=(
            chain_state if chain_state is not None else SsotChainState(conn)
        ).token_holdings(station_id=station_id, event_date=event_date),
        account_capital=account_capital,
        chain_state=chain_state,
    )
    phase_input = BasketCashPhaseInput.from_mapping({
        "policy_version": cash_policy.policy_version,
        "station_id": station_id,
        "current_phase": CashPhase.HEAVY.value,
        "event_date_local": event_date,
        # This is evidence lineage, not the daemon poll clock. Duplicate
        # transport frames must not manufacture a new cash decision.
        "observed_at_utc": market["quote_observed_at"],
        "station_timezone": cash_policy.station_timezone,
        "checkpoint_id": "p0_cash_" + envelope.checkpoint_id,
    })
    # The stage-open persists this snapshot as the forward evidence trio
    # (charter 46/48); the materializer requires the cash-phase input bound
    # into the quote row itself.
    market["cash_phase_input"] = phase_input.to_dict()
    return market, cash_policy, phase_input


def compile_p0_market_evidence(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    evaluation_at_utc: str,
    registry_snapshot: Mapping[str, Any],
    ws_manifest: Mapping[str, Any],
    ws_quotes: Mapping[str, Mapping[str, Any]],
    planning_budget_usdc: float,
    cash_phase_authority_manifest: (
        CashPhaseAuthorityManifest | Mapping[str, Any] | None
    ) = None,
    book_equity_usdc: float | None = None,
) -> dict[str, Any]:
    """Resolve one scope's market snapshot without compiling a plan.

    The book and the plan are not the same kind of thing.  A plan is a
    trading decision and may legitimately refuse -- the tape offers a
    sub-tick price, the regime disagrees with the support, delta-v2 has no
    artifact yet -- but the quote that refusal was measured against is an
    observation, and a forward quote missed is not refetchable.  This entry
    point exists so the caller can record the observation on exactly the
    paths where the decision refused.

    It therefore asserts nothing about trading authority: no probability
    bridge, no formal-model authority, no P0/P0-M stage gate.  Those decide
    whether the book may be acted on, which is precisely the question this
    path is not asking.  Everything the *snapshot* depends on -- registry
    support, manifest binding, strict-as-of freshness -- is still enforced
    by the shared builder, so an unrecordable book still fails closed.
    """

    expected_source_lane = target_registry_source_lane(
        conn,
        station_id=station_id,
        event_date=event_date,
    )
    envelopes = load_materialized_probability_envelopes(
        conn,
        station_id=station_id,
        event_date=event_date,
        evaluation_at_utc=evaluation_at_utc,
        expected_source_lane=expected_source_lane,
    )
    envelope = select_current_stage(envelopes, at_utc=evaluation_at_utc)
    artifacts = load_production_artifacts(
        conn,
        station_id=station_id,
        evaluation_at_utc=evaluation_at_utc,
        require_calibration=False,
    )
    market, _cash_policy, _phase_input = _bind_scope_market_evidence(
        conn,
        envelope=envelope,
        artifacts=artifacts,
        station_id=station_id,
        event_date=event_date,
        evaluation_at_utc=evaluation_at_utc,
        registry_snapshot=registry_snapshot,
        ws_manifest=ws_manifest,
        ws_quotes=ws_quotes,
        planning_budget_usdc=planning_budget_usdc,
        cash_phase_authority_manifest=cash_phase_authority_manifest,
        book_equity_usdc=book_equity_usdc,
        # Evidence only: this pass writes the trio and sizes nothing.
        account_capital=None,
    )
    return market


def compile_p0_preselection(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    evaluation_at_utc: str,
    registry_snapshot: Mapping[str, Any] | None = None,
    ws_manifest: Mapping[str, Any] | None = None,
    ws_quotes: Mapping[str, Mapping[str, Any]] | None = None,
    season_bin: str,
    maturity_bin: str,
    liquidity_bin: str,
    strategy_version: str,
    planning_budget_usdc: float,
    cash_phase_authority_manifest: CashPhaseAuthorityManifest | Mapping[str, Any] | None = None,
    book_equity_usdc: float | None = None,
    observed_running_high_c: float | None = None,
    base_policy: Mapping[str, Any] | None = None,
    chain_state: Any = None,
    forward_book: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile one forward-day (P0 / P0-M) basket decision.

    Production passes the live stream (``registry_snapshot``, ``ws_manifest``,
    ``ws_quotes``) and no ``chain_state``.  A replay of a recorded decision
    and the paper ledger pass ``forward_book`` (the book that decision was
    priced against) and their own ``chain_state`` instead -- the same
    compile, with only the book source and the station-day's history
    swapped (user ruling 2026-09-24: 「账本对齐」).
    """
    chain_state = chain_state if chain_state is not None else SsotChainState(conn)
    expected_source_lane = target_registry_source_lane(
        conn,
        station_id=station_id,
        event_date=event_date,
    )
    envelopes = load_materialized_probability_envelopes(
        conn,
        station_id=station_id,
        event_date=event_date,
        evaluation_at_utc=evaluation_at_utc,
        expected_source_lane=expected_source_lane,
        committed_before_utc=chain_state.store_before_utc,
    )
    envelope = select_current_stage(envelopes, at_utc=evaluation_at_utc)
    probability_bridge = assert_current_probability_bridge(
        conn,
        probability=envelope,
        evaluation_at_utc=evaluation_at_utc,
        committed_before_utc=chain_state.store_before_utc,
    )
    assert_formal_probability_model_authority(
        envelopes,
        probability=envelope,
    )
    if envelope.stage not in {"P0", "P0-M"}:
        raise ContractError("p0_preselection_p1_is_final_authority")
    # Same seam as the open-day chain: the envelope's lead is hour-bucket
    # vocabulary, the policy layer speaks calendar maturity (charter 47).
    probability_maturity_bin = derive_policy_maturity_bin(
        str(envelope.lineage.get("lead_bucket") or ""),
        event_date=str(envelope.event_date),
        settlement_timezone=str(envelope.settlement_timezone),
        evaluation_at_utc=evaluation_at_utc,
    )
    regime_artifact = load_regime_artifact(
        conn,
        probability=envelope,
        evaluation_at_utc=evaluation_at_utc,
    )
    artifacts = load_production_artifacts(
        conn,
        station_id=station_id,
        evaluation_at_utc=evaluation_at_utc,
        # P0/P0-M probability authority is the committed native cross-
        # ensemble envelope above.  Deterministic calibration artifacts are
        # retired from the P0 pathway; this bundle is still required for the
        # station policy/cash compiler but must not resurrect the old model
        # gate for an otherwise valid native envelope.
        require_calibration=False,
    )
    market, cash_policy, phase_input = _bind_scope_market_evidence(
        conn,
        envelope=envelope,
        artifacts=artifacts,
        station_id=station_id,
        event_date=event_date,
        evaluation_at_utc=evaluation_at_utc,
        registry_snapshot=registry_snapshot,
        ws_manifest=ws_manifest,
        ws_quotes=ws_quotes,
        planning_budget_usdc=planning_budget_usdc,
        cash_phase_authority_manifest=cash_phase_authority_manifest,
        book_equity_usdc=book_equity_usdc,
        # Charter 141: the account, read the way the production compile
        # reads it -- stale is a refusal, never a guess -- through the chain
        # state (the real account in production, the simulated one on paper).
        account_capital=chain_state.account_capital(
            evaluation_at_utc=evaluation_at_utc,
        ),
        chain_state=chain_state,
        forward_book=forward_book,
    )
    (
        delta_v2_artifact,
        delta_v2_fill_resolver,
        delta_v2_resolution_blocker,
    ) = resolve_delta_v2_runtime_authority(
        conn,
        station_id=station_id,
        stage=envelope.stage,
        season_bin=season_bin,
        maturity_bin=probability_maturity_bin,
        liquidity_bin=liquidity_bin,
        strategy_version=strategy_version,
        evaluation_at_utc=evaluation_at_utc,
    )
    if delta_v2_resolution_blocker:
        raise ContractError(delta_v2_resolution_blocker)
    compiled = compile_production_plan(
        # A preselection is the opening plan, and a position is opened at P0
        # whichever stage's probability it opens on.  P0-M is a confirmation
        # of an opening, not an opening of its own, and has no execution
        # policy of its own under this strategy version.  (Pinned by
        # ``test_opening_plan_resolves_the_opening_stage_policy_not_the_envelope_stage``;
        # the 2026-09-17 fork sweep confirmed the three stages' policies
        # carry the same values today and left this ruling alone.)
        station_policy_stage="P0",
        # Parity with the compile entry: the opener's recorded tilt and the
        # scope's base policy reach this lane too (charter 139 makes the tilt
        # 1.0 for every chain opened since; no scope carries a base policy
        # today).
        base_policy=base_policy,
        # The chain's history and the claim tables -- the opener's tilt,
        # charter 68(c)'s lambda tables, 71(a)'s last-acted identity and
        # 77's last signature of THIS stage (the envelope's: a P0-M decision
        # is recorded under P0-M) -- from the one chain-state reader both
        # entries share.  This lane forgot two of them in turn when each was
        # read inline here (the tables until the store's first decisions,
        # the history until 2026-09-05).
        **chain_compile_kwargs(
            conn, chain_state, station_id=station_id, event_date=event_date,
            stage=str(envelope.stage),
        ),
        # Arm B (2026-09-06): the belief and book H hours before this
        # compile, for the convergence claim lift (shadow by default).
        convergence_evidence=load_convergence_evidence(
            conn, station_id=station_id, event_date=event_date,
            as_of_utc=evaluation_at_utc,
        ),
        probability_envelopes=envelopes,
        # Charter 125: the day's observed high rides on the book the plan is
        # priced against, so the floor and the quotes are the same snapshot.
        market_snapshot=(
            market
            if observed_running_high_c is None
            else {
                **market,
                OBSERVED_HIGH_MARKET_SNAPSHOT_KEY: float(
                    observed_running_high_c
                ),
            }
        ),
        policy_activations=artifacts.policy_activations,
        global_priors=artifacts.global_priors,
        policy_artifacts=artifacts.policy_artifacts,
        evaluation_at_utc=evaluation_at_utc,
        season_bin=season_bin,
        maturity_bin=probability_maturity_bin,
        liquidity_bin=liquidity_bin,
        strategy_version=strategy_version,
        delta_v2_policy=delta_v2_artifact,
        delta_v2_fill_resolver=delta_v2_fill_resolver,
        cash_phase_policy=cash_policy,
        cash_phase_input=phase_input,
        policy_authority_scope=AUTHORITY_SCOPE,
        regime_artifact=regime_artifact,
        require_regime_ssot=True,
        resource_probe_ok=True,
        delta_v2_owns_cadence=True,
        expected_source_lane=expected_source_lane,
    )
    # The atomic stage-open validates against ``production_inputs`` -- the
    # same identity block the open-day compiler attaches.  The forward
    # preselection resolves every one of these authorities itself, so the
    # stage-open must not fail closed on an absent mapping.
    compiled["production_inputs"] = {
        "probability_envelope_count": len(envelopes),
        "probability_envelope_ids": [
            item.checkpoint_id for item in envelopes
        ],
        "probability_bridge_history_id": probability_bridge["history_id"],
        "probability_bridge_projection_sha256": (
            probability_bridge["projection_sha256"]
        ),
        "probability_bridge_checkpoint_id": (
            probability_bridge["checkpoint_id"]
        ),
        "probability_bridge_envelope_content_sha256": (
            probability_bridge["envelope_content_sha256"]
        ),
        "quote_snapshot_id": market["snapshot_id"],
        "quote_source": market["quote_source"],
        "inventory_version": market["inventory_version"],
        "order_state_version": market["order_state_version"],
        "cash_phase_policy_version": cash_policy.policy_version,
        "cash_phase_policy_sha256": cash_policy.content_sha256,
        "probability_maturity_bin": probability_maturity_bin,
        "regime_artifact_id": regime_artifact.artifact_id,
        "regime_artifact_sha256": regime_artifact.content_sha256,
        "regime_probability_sha256": regime_artifact.probability_sha256,
        "regime_state": regime_artifact.regime,
        "configured_maturity_bin": maturity_bin,
        "season_bin": season_bin,
        "liquidity_bin": liquidity_bin,
        "strategy_version": strategy_version,
        "delta_v2_artifact_id": (
            delta_v2_artifact.artifact_id
            if delta_v2_artifact is not None
            else None
        ),
        "delta_v2_artifact_sha256": (
            delta_v2_artifact.content_sha256
            if delta_v2_artifact is not None
            else None
        ),
        "delta_v2_fill_distribution_artifact_sha256": (
            delta_v2_artifact.fill_distribution_artifact_sha256
            if delta_v2_artifact is not None
            else None
        ),
        "delta_v2_resolution_blocker": None,
        "read_only": True,
    }
    resolved_policy = compiled.get("resolved_station_policy")
    if (
        not isinstance(resolved_policy, Mapping)
        or not str(resolved_policy.get("activation_id") or "").strip()
    ):
        reason_codes = ",".join(
            str(value) for value in compiled.get("reason_codes", [])
        )
        raise ContractError(
            "p0_preselection_policy_activation_unresolved"
            + (":" + reason_codes if reason_codes else "")
        )
    manifest_sha = str(
        (ws_manifest or {}).get("snapshot_sha256")
        or (forward_book or {}).get("manifest_sha256")
        or ""
    )
    identity = {
        "schema": SCHEMA,
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": str(envelope.settlement_timezone),
        "checkpoint_id": envelope.checkpoint_id,
        "probability_stage": envelope.stage,
        "decision_at_utc": envelope.decision_at_utc,
        "quote_observed_at_utc": market["quote_observed_at"],
        "quote_manifest_sha256": manifest_sha,
        "economic_quote_sha256": market["economic_quote_sha256"],
        "policy_activation_id": str(resolved_policy["activation_id"]),
        "policy_authority_scope": AUTHORITY_SCOPE,
        "planning_budget_usdc": float(planning_budget_usdc),
        "cash_phase_authority_manifest": (
            cash_phase_authority_manifest.to_dict()
            if isinstance(cash_phase_authority_manifest, CashPhaseAuthorityManifest)
            else dict(cash_phase_authority_manifest or {})
        ),
        "compiled_plan": compiled,
        "state": STATE,
        "final_authority": "P1",
        "capital_authorized": False,
        "intents_written": 0,
        "reservations_written": 0,
        "live_submit_authorized": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    plan_sha256 = content_sha256(
        _decision_semantic_projection(compiled)
    )
    economic_identity = {
        "schema": SCHEMA,
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": str(envelope.settlement_timezone),
        "checkpoint_id": envelope.checkpoint_id,
        "probability_stage": envelope.stage,
        "policy_activation_id": str(resolved_policy["activation_id"]),
        "policy_authority_scope": AUTHORITY_SCOPE,
        "plan_sha256": plan_sha256,
    }
    return {
        **identity,
        "preselection_id": (
            "p0_preselection_"
            + content_sha256(economic_identity)[:40]
        ),
        "plan_sha256": plan_sha256,
        # Carried out for the atomic commit, which prices the opening intents
        # against the very snapshot this plan was compiled from.  It is
        # deliberately kept out of ``identity`` and dropped again before the
        # preselection row is serialized, so exposing it cannot move the
        # preselection's content address.
        UNPERSISTED_MARKET_SNAPSHOT_KEY: market,
    }


def commit_p0_preselection(
    conn: sqlite3.Connection,
    value: Mapping[str, Any],
    *,
    created_at_utc: str,
) -> dict[str, Any]:
    if value.get("schema") != SCHEMA or value.get("state") != STATE:
        raise ContractError("p0_preselection_contract_invalid")
    if (
        value.get("final_authority") != "P1"
        or value.get("live_submit_authorized") is not False
        or value.get("calls_order_gateway") is not False
        or int(value.get("orders_submitted", -1)) != 0
        or int(value.get("intents_written", -1)) != 0
        or int(value.get("reservations_written", -1)) != 0
    ):
        raise ContractError("p0_preselection_execution_authority_forbidden")
    assert_connection_target_capacity(conn)
    row = (
        str(value["preselection_id"]),
        str(value["station_id"]),
        str(value["event_date"]),
        str(value["checkpoint_id"]),
        str(value["probability_stage"]),
        str(value["decision_at_utc"]),
        str(value["quote_observed_at_utc"]),
        str(value["quote_manifest_sha256"]),
        str(value["policy_activation_id"]),
        str(value["policy_authority_scope"]),
        json.dumps(
            {
                key: item
                for key, item in value.items()
                if key != UNPERSISTED_MARKET_SNAPSHOT_KEY
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        str(value["plan_sha256"]),
        STATE,
        "P1",
        0,
        0,
        0,
        str(created_at_utc),
    )
    existing = conn.execute(
        "SELECT plan_sha256,quote_manifest_sha256 "
        "FROM basket_p0_preselection_plan WHERE preselection_id=?",
        (row[0],),
    ).fetchone()
    if existing is not None:
        if str(existing[0]) != row[11]:
            raise ContractError("p0_preselection_content_conflict")
        return {
            "state": "ALREADY_COMMITTED",
            "wrote": False,
            "preselection_id": row[0],
            "quote_manifest_sha256": str(existing[1]),
        }
    conn.execute(
        "INSERT INTO basket_p0_preselection_plan VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        row,
    )
    return {
        "state": "COMMITTED",
        "wrote": True,
        "preselection_id": row[0],
        "quote_manifest_sha256": row[7],
    }


__all__ = [
    "AUTHORITY_SCOPE",
    "SCHEMA",
    "STATE",
    "commit_p0_preselection",
    "compile_p0_market_evidence",
    "compile_p0_preselection",
    "latest_visible_p0_station_ids",
    "load_market_registry_snapshot",
    "load_official_ws_quotes",
]
