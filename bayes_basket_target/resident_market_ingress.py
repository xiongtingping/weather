"""Resident-owned market evidence materialization for ``production_no_live``.

The market WS service publishes only the official, content-addressed manifest.
This adapter runs inside the existing ``reported_trajectory_no`` service and
joins that manifest with target-owned q=0 inventory, target-owned quiescent
order state, and a promoted cash-phase artifact.  It never contacts an
exchange, reads legacy ticks/positions, or creates an order.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .activation import load_target_activation
from .cash_phase import BasketCashPhasePolicyArtifact, CashPhase, evaluate_cash_phase
from .cash_phase_authority import (
    CashPhaseAuthorityManifest,
    load_cash_phase_authority_manifest,
    resolve_active_cash_phase_policy,
)
from .contracts import ContractError, content_sha256
from .inventory_producer import (
    build_no_live_inventory_ingress,
    has_execution_transition_versions,
)
from .market_ingress import (
    load_official_ws_quotes,
    materialize_market_evidence,
    materialize_official_ws_evidence_cache,
)
from .market_registry import load_started_registry_rows
from .order_state_producer import (
    build_execution_transition_order_state_ingress,
    build_no_live_order_state_ingress,
)
from .quote_freshness import QuoteFreshnessPolicy


SCHEMA = "bayes_basket_resident_market_ingress_v1"


def _timestamp(value: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("resident_market_observed_at_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("resident_market_observed_at_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _registry_scopes(
    conn: sqlite3.Connection,
    *,
    station_id: str = "",
    event_date: str = "",
) -> dict[tuple[str, str], list[dict[str, str]]]:
    rows = load_started_registry_rows(conn, station_id=station_id, event_date=event_date)
    scopes: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in rows:
        station = str(row["station_id"] or "").strip()
        day = str(row["event_date"] or "").strip()
        bucket = str(row["outcome"] or "").strip()
        token = str(row["yes_token"] or "").strip()
        if not station or not day or not bucket or not token:
            raise ContractError("resident_market_registry_identity_missing")
        scopes.setdefault((station, day), []).append({"bucket": bucket, "token_id": token})
    if not scopes:
        raise ContractError("resident_market_registry_scope_empty")
    return scopes


def _complete_quote_scopes(
    scopes: Mapping[tuple[str, str], list[Mapping[str, str]]],
    quotes: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[tuple[str, str], list[Mapping[str, str]]], dict[str, list[str]]]:
    """Select only scopes whose every target YES bucket has WS evidence.

    The market ingress remains strict for an explicitly selected scope.  This
    boundary prevents an unrelated stale or reconnecting registry scope from
    aborting complete scopes in the same resident pass, while preserving a
    diagnostic of every omitted scope and token.
    """

    complete: dict[tuple[str, str], list[Mapping[str, str]]] = {}
    skipped: dict[str, list[str]] = {}
    for scope, rows in sorted(scopes.items()):
        missing = sorted(
            str(item["token_id"])
            for item in rows
            if str(item["token_id"]) not in quotes
        )
        if missing:
            skipped[f"{scope[0]}|{scope[1]}"] = missing
        else:
            complete[scope] = list(rows)
    return complete, skipped


def _policy_for_scope(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    observed_at_utc: str,
    policy_authority_scope: str,
    cash_phase_authority_manifest: CashPhaseAuthorityManifest | Mapping[str, Any] | None = None,
) -> BasketCashPhasePolicyArtifact:
    authority_scope = str(policy_authority_scope or "").strip().upper()
    rows = conn.execute(
        "SELECT policy_version,station_id,station_timezone,"
        "heavy_to_standard_local_time,standard_to_tail_local_time,"
        "cash_by_phase_json,valid_from_utc,valid_until_utc,evidence_status,"
        "promoted,content_sha256 FROM basket_cash_phase_policy_artifact "
        "WHERE station_id=?",
        (station_id,),
    ).fetchall()
    policies = []
    for row in rows:
        try:
            cash_by_phase = json.loads(str(row[5] or "{}"))
        except (TypeError, ValueError) as exc:
            raise ContractError("resident_cash_phase_policy_json_invalid") from exc
        policies.append(BasketCashPhasePolicyArtifact.from_mapping({
            "policy_version": row[0],
            "station_id": row[1],
            "station_timezone": row[2],
            "heavy_to_standard_local_time": row[3],
            "standard_to_tail_local_time": row[4],
            "cash_by_phase": cash_by_phase,
            "valid_from_utc": row[6],
            "valid_until_utc": row[7],
            "evidence_status": row[8],
            "promoted": bool(row[9]),
            "content_sha256": row[10],
        }))
    try:
        authority_manifest = cash_phase_authority_manifest
        if authority_manifest is None:
            authority_manifest = load_cash_phase_authority_manifest(
                conn,
                station_id=station_id,
                observed_at_utc=observed_at_utc,
                required_authority_scope=authority_scope,
            )
        return resolve_active_cash_phase_policy(
            policies,
            authority_manifest=authority_manifest,
            station_id=station_id,
            observed_at_utc=observed_at_utc,
            required_authority_scope=authority_scope,
        )
    except ContractError as exc:
        # Preserve the resident boundary's namespace while retaining the
        # exact resolver reason for audit and focused fail-closed diagnosis.
        raise ContractError(f"resident_{exc}") from exc


def _previous_phase(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
) -> CashPhase:
    row = conn.execute(
        "SELECT cash_phase_input_json FROM basket_official_ws_snapshot "
        "WHERE station_id=? AND event_date=? AND state='COMMITTED' "
        "ORDER BY sequence DESC, rowid DESC LIMIT 1",
        (station_id, event_date),
    ).fetchone()
    if row is None:
        return CashPhase.HEAVY
    try:
        value = json.loads(str(row[0] or "{}"))
        return CashPhase(str(value.get("current_phase") or ""))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError("resident_previous_cash_phase_invalid") from exc


def _cash_phase_payload(
    conn: sqlite3.Connection,
    *,
    scopes: Mapping[tuple[str, str], list[Mapping[str, str]]],
    quotes: Mapping[str, Mapping[str, Any]],
    policy_authority_scope: str,
    cash_phase_authority_manifest: CashPhaseAuthorityManifest | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    markets: list[dict[str, Any]] = []
    for (station_id, event_date), registry_rows in sorted(scopes.items()):
        selected_times = []
        for item in registry_rows:
            quote = quotes.get(str(item["token_id"]))
            if quote is None:
                continue
            selected_times.append(str(quote.get("book_observed_at") or quote.get("created_at") or ""))
        if not selected_times:
            raise ContractError("resident_selected_quote_missing")
        observed_at = _timestamp(max(selected_times))
        policy = _policy_for_scope(
            conn,
            station_id=station_id,
            observed_at_utc=observed_at,
            policy_authority_scope=policy_authority_scope,
            cash_phase_authority_manifest=cash_phase_authority_manifest,
        )
        previous = _previous_phase(conn, station_id=station_id, event_date=event_date)
        phase_input = {
            "policy_version": policy.policy_version,
            "station_id": station_id,
            "current_phase": previous.value,
            "event_date_local": event_date,
            "observed_at_utc": observed_at,
            "station_timezone": policy.station_timezone,
            "checkpoint_id": "cash_" + content_sha256({
                "policy_sha256": policy.content_sha256,
                "station_id": station_id,
                "event_date": event_date,
                "observed_at_utc": observed_at,
            })[:32],
        }
        decision = evaluate_cash_phase(policy, phase_input)
        phase_input["current_phase"] = decision.next_phase.value
        markets.append({
            "station_id": station_id,
            "event_date": event_date,
            "cash_phase_input": phase_input,
            "cash_phase_decision": decision.to_dict(),
        })
    return {
        "schema": "bayes_basket_cash_phase_ingress_v1",
        "markets": markets,
        "cash_phase_authority_manifest": (
            cash_phase_authority_manifest.to_dict()
            if isinstance(cash_phase_authority_manifest, CashPhaseAuthorityManifest)
            else dict(cash_phase_authority_manifest or {})
        ),
        "places_orders": False,
        "calls_order_gateway": False,
    }


def materialize_resident_no_live_market_evidence(
    conn: sqlite3.Connection,
    *,
    manifest_path: Path,
    activation_path: Path,
    remaining_budget_usdc: float,
    policy_authority_scope: str,
    cash_phase_authority_manifest: CashPhaseAuthorityManifest | Mapping[str, Any] | None = None,
    station_id: str = "",
    event_date: str = "",
    materialize_ws_cache: bool = True,
) -> dict[str, Any]:
    """Materialize current WS evidence, then the stricter executable bundle.

    The evidence cache is deliberately committed first and independently.  It
    is target-owned, bounded, evidence-only data and cannot satisfy readiness
    or be consumed by the compiler.  If q=0, order-state, cash policy, or any
    other executable prerequisite is missing, the cache remains useful while
    the full market bundle still fails closed without writing a snapshot.
    """

    evidence_cache = None
    activation = load_target_activation(Path(activation_path))
    activation_scope = str(
        activation.get("policy_authority_scope") or ""
    ).strip().upper()
    requested_scope = str(policy_authority_scope or "").strip().upper()
    if activation_scope != requested_scope:
        raise ContractError(
            "resident_quote_freshness_authority_scope_mismatch"
        )
    quote_freshness_policy = QuoteFreshnessPolicy.from_mapping(
        activation.get("quote_freshness_policy")
    )
    if materialize_ws_cache:
        evidence_cache = materialize_official_ws_evidence_cache(
            conn,
            manifest_path=Path(manifest_path),
            station_id=station_id,
            event_date=event_date,
            commit=True,
        )
    _root, quotes = load_official_ws_quotes(Path(manifest_path))
    all_scopes = _registry_scopes(conn, station_id=station_id, event_date=event_date)
    scopes, skipped_scopes = _complete_quote_scopes(all_scopes, quotes)
    if not scopes:
        raise ContractError("resident_complete_quote_scope_missing")
    selected_times = [
        str(quotes[str(item["token_id"])].get("book_observed_at")
            or quotes[str(item["token_id"])].get("created_at") or "")
        for registry_rows in scopes.values()
        for item in registry_rows
        if str(item["token_id"]) in quotes
    ]
    if not selected_times:
        raise ContractError("resident_selected_quote_missing")
    # Shared inventory/order generations must be no later than every selected
    # quote in the batch.  Using the minimum prevents an unrelated newer token
    # from making a different station's evidence look strict-as-of.
    observed_at = min(selected_times)
    inventory = build_no_live_inventory_ingress(
        conn,
        remaining_budget_usdc=remaining_budget_usdc,
        observed_at_utc=_timestamp(observed_at),
        station_id=station_id,
        event_date=event_date,
    )
    # Once the target transition ledger has any version in the selected
    # scope, q=0/zero-pending is forbidden.  Both projections must consume
    # the same authoritative, content-addressed transition set.
    has_transitions = has_execution_transition_versions(
        conn, station_id=station_id, event_date=event_date
    )
    if has_transitions:
        order_state = build_execution_transition_order_state_ingress(
            conn,
            activation_path=Path(activation_path),
            observed_at_utc=_timestamp(observed_at),
            station_id=station_id,
            event_date=event_date,
        )
    else:
        order_state = build_no_live_order_state_ingress(
            conn,
            activation_path=Path(activation_path),
            observed_at_utc=_timestamp(observed_at),
            station_id=station_id,
            event_date=event_date,
        )
    cash_phase = _cash_phase_payload(
        conn,
        scopes=scopes,
        quotes=quotes,
        policy_authority_scope=policy_authority_scope,
        cash_phase_authority_manifest=cash_phase_authority_manifest,
    )
    selected_keys = set(scopes)
    result = materialize_market_evidence(
        conn,
        manifest_path=Path(manifest_path),
        inventory_payload={
            **inventory,
            "markets": [
                item for item in inventory.get("markets", [])
                if (str(item.get("station_id") or ""), str(item.get("event_date") or "")) in selected_keys
            ],
        },
        order_state_payload={
            **order_state,
            "markets": [
                item for item in order_state.get("markets", [])
                if (str(item.get("station_id") or ""), str(item.get("event_date") or "")) in selected_keys
            ],
        },
        cash_phase_payload=cash_phase,
        station_id=station_id,
        event_date=event_date,
        scope_keys=selected_keys,
        quote_freshness_policy=quote_freshness_policy,
        policy_authority_scope=requested_scope,
        commit=True,
    )
    result["scope_selection"] = {
        "registry_scope_count": len(all_scopes),
        "selected_complete_scope_count": len(scopes),
        "skipped_incomplete_scope_count": len(skipped_scopes),
        "skipped_incomplete_scopes": skipped_scopes,
    }
    if evidence_cache is not None:
        result["evidence_cache"] = evidence_cache
    return result


__all__ = ["SCHEMA", "materialize_resident_no_live_market_evidence"]
