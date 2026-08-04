#!/usr/bin/env python3
"""Read-only production uniqueness, source, capital and latency acceptance."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reported_trajectory_country_sources import load_official_source_config
from bayes_basket_target.held_yes_registry import (
    classify_held_yes_tokens,
    quote_required_token_ids,
    registry_classification_complete,
)

DB = ROOT / "data" / "archive.db"
CONFIG = ROOT / "config" / "trading" / "awc_truth_ingress_v1.json"
STATUS = ROOT / "data" / "weather_basket_yes_status.json"
MARKET_WS_HEARTBEAT = ROOT / "data" / "market_ws_heartbeat.json"
MARKET_WS_MANIFEST = ROOT / "data" / "market_ws_latest_quotes.json"
HELD_YES_INVENTORY = ROOT / "data" / "reported_trajectory_held_yes_tokens.json"
HELD_YES_WS_MANIFEST = ROOT / "data" / "market_ws_held_yes_quotes.json"
HELD_YES_CLOB_RECONCILE = (
    ROOT / "output" / "audits" / "reported_trajectory_held_yes_clob_reconcile_latest.json"
)
MARKET_WS_QUOTE_SHARDS = ROOT / "data" / "market_ws_quote_shards"
HELD_YES_WS_QUOTE_SHARDS = ROOT / "data" / "market_ws_held_yes_quote_shards"
HELD_YES_CLOB_RECONCILE_SCHEMA = "reported_trajectory_held_yes_clob_reconcile_v1"
HELD_YES_CLOB_RECONCILE_SOURCE = "official_local_clob_execution_guard_snapshot"
HELD_YES_WS_MANIFEST_SCHEMA = "clob_ws_held_yes_quote_manifest_v1"
HELD_YES_WS_TOKEN_SCHEMA = "clob_ws_held_yes_token_quote_snapshot_v1"
PRIMARY_WS_MANIFEST_SCHEMA = "clob_ws_latest_quote_snapshot_v1"
PRIMARY_WS_TOKEN_SCHEMA = "clob_ws_token_quote_snapshot_v1"
HELD_YES_QUOTE_MAX_AGE_SEC = 120.0
MAX_PRIMARY_WS_TOKEN_SNAPSHOTS = 4_000
MAX_PRIMARY_WS_TOKEN_SNAPSHOT_BYTES = 64 * 1024
MAX_PRIMARY_WS_SCAN_BYTES = 64 * 1024 * 1024
CORE = {
    "com.weather.weather_basket_yes",
    "com.weather.market_ws",
    "com.weather.clob_order_service",
}
LEGACY = (
    # This is the retired service identity, not merely a legacy strategy
    # implementation.  Keeping it in the runtime exclusion set prevents a
    # stale user/system launchd job from silently coexisting with the sole
    # pure-YES basket owner below.
    "com.weather.reported_trajectory_no",
    "com.weather.dynamic_three_leg_execution", "com.weather.trade_engine",
    "com.weather.auto_trade_entry_1m", "com.weather.auto_trade_worker",
    "com.weather.target_realization_worker", "com.weather.trade_sync_loop",
)


def canonical_sha(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def strict_token_id_set(value: Any) -> set[str] | None:
    """Return a duplicate-free token-id set, or ``None`` for malformed input."""

    if not isinstance(value, list):
        return None
    tokens: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            return None
        token_id = raw.strip()
        if not token_id:
            return None
        tokens.append(token_id)
    token_ids = set(tokens)
    return token_ids if len(token_ids) == len(tokens) else None


def held_yes_reconcile_receipt_valid(
    *, inventory: Mapping[str, Any], receipt: Mapping[str, Any]
) -> bool:
    """Verify that the active held-YES projection is bound to a CLOB receipt.

    A reconciler receipt records the CLOB-proven non-zero token set.  It is
    intentionally not a quote attestation: the old-token quote gate remains
    independently fail-closed.  Its snapshot timestamp is not used as a live
    freshness clock because the CLOB service continuously re-prefetches the
    same active token set; that liveness is checked separately from /health.
    """

    active_token_ids = strict_token_id_set(inventory.get("active_token_ids"))
    known_token_ids = strict_token_id_set(inventory.get("token_ids"))
    nonzero_token_ids = strict_token_id_set(receipt.get("nonzero_token_ids"))
    zero_token_ids = strict_token_id_set(receipt.get("zero_token_ids"))
    evidence_only_token_ids = strict_token_id_set(receipt.get("evidence_only_token_ids"))
    unverified_balance_token_ids = strict_token_id_set(
        receipt.get("unverified_balance_token_ids")
    )
    unavailable_token_ids = strict_token_id_set(receipt.get("unavailable_token_ids"))
    scope_token_ids = _ordered_token_ids(receipt.get("scope_token_ids"))
    if (
        active_token_ids is None
        or known_token_ids is None
        or nonzero_token_ids is None
        or zero_token_ids is None
        or evidence_only_token_ids is None
        or unverified_balance_token_ids is None
        or unavailable_token_ids is None
        or scope_token_ids is None
        or not active_token_ids <= known_token_ids
    ):
        return False
    if (
        inventory.get("complete") is not True
        or inventory.get("active_token_ids_source") != HELD_YES_CLOB_RECONCILE_SOURCE
        or receipt.get("schema") != HELD_YES_CLOB_RECONCILE_SCHEMA
        or receipt.get("source") != HELD_YES_CLOB_RECONCILE_SOURCE
        or receipt.get("complete") is not True
        or receipt.get("evidence_only") is not True
        or receipt.get("authorizes_order") is not False
        or receipt.get("calls_order_gateway") is not False
        or receipt.get("orders_submitted") != 0
        or receipt.get("token_count") != len(known_token_ids)
        or scope_token_ids != sorted(known_token_ids)
        or receipt.get("scope_sha256")
        != hashlib.sha256("\x00".join(scope_token_ids).encode("utf-8")).hexdigest()
        or evidence_only_token_ids
        or unverified_balance_token_ids
        != evidence_only_token_ids
        or unavailable_token_ids
        or (nonzero_token_ids | zero_token_ids) != known_token_ids
        or nonzero_token_ids & zero_token_ids
    ):
        return False

    supplied_receipt_sha = receipt.get("receipt_sha256")
    receipt_identity = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if (
        not isinstance(supplied_receipt_sha, str)
        or supplied_receipt_sha != canonical_sha(receipt_identity)
        or inventory.get("active_token_ids_receipt_sha256") != supplied_receipt_sha
        or active_token_ids != nonzero_token_ids
    ):
        return False

    observed_at = receipt.get("observed_at")
    return (
        isinstance(observed_at, str)
        and bool(observed_at)
        and inventory.get("active_token_ids_observed_at") == observed_at
        and inventory.get("observed_at") == observed_at
    )


def active_held_yes_balances_ready(
    *, health: Mapping[str, Any], active_token_ids: set[str]
) -> bool:
    """Require the local read-only CLOB prefetch to cover every active token."""

    ready_token_ids = strict_token_id_set(
        health.get("conditional_balance_ready_token_ids")
    )
    return bool(
        health.get("active_held_yes_balance_ready") is True
        and ready_token_ids is not None
        and active_token_ids <= ready_token_ids
    )


def forward_awc_taf_capture_isolated(status: Mapping[str, Any]) -> bool:
    """Verify the resident's bounded TAF lane without mixing it into truth.

    The AWC METAR/SPECI truth poll must remain temperature-only.  TAF is a
    separate, bounded forward-evidence lane that can later form a P0-M only
    after its own revision/OOS gates.  Calling the old combined assertion
    ``taf_disabled`` obscured that distinction and made a healthy P0-M input
    pipeline look retired.
    """

    prefetch = status.get("forecast_prefetch")
    if not isinstance(prefetch, Mapping):
        return False
    capture = prefetch.get("forward_taf_capture")
    if not isinstance(capture, Mapping):
        return False
    last_attempt = capture.get("last_attempt")
    if not isinstance(last_attempt, Mapping):
        return False
    return bool(
        status.get("taf_requested") is True
        and status.get("weak_evidence_taf_capture_isolated") is True
        and capture.get("calls_order_gateway") is False
        and capture.get("places_orders") is False
        and capture.get("state") in {
            "captured",
            # One malformed provider clock must remain isolated to its
            # station scope while successful captures stay usable.  The
            # resident reports this aggregate state explicitly.
            "captured_with_scope_failures",
            "not_due",
            # The bounded forecast worker runs outside the truth/decision
            # path.  During its short network window the resident publishes
            # this state before the result is available.  Keep acceptance
            # race-safe by validating the previous attempt below instead of
            # treating an in-flight capture as a broken lane.
            "pending_same_bounded_worker",
            "blocked_fail_closed",
            "no_active_registry_scope",
        }
        and last_attempt.get("state") in {
            "captured",
            "captured_with_scope_failures",
            "blocked_fail_closed",
            "no_active_registry_scope",
        }
        and isinstance(last_attempt.get("capture_count"), int)
        and int(last_attempt["capture_count"]) >= 0
        and last_attempt.get("calls_order_gateway") is False
        and last_attempt.get("places_orders") is False
    )
def held_yes_inventory_fresh(
    *,
    inventory: Mapping[str, Any],
    receipt: Mapping[str, Any],
    health: Mapping[str, Any],
) -> bool:
    """Accept either legacy fast inventory or its CLOB-bound active projection.

    Once an inventory is explicitly projected from the CLOB reconciler, it
    must continue to validate against that receipt.  This prevents a corrupted
    or mismatched receipt from silently falling back to the old 15-second Data
    API timestamp.
    """

    active_token_ids = strict_token_id_set(inventory.get("active_token_ids"))
    known_token_ids = strict_token_id_set(inventory.get("token_ids"))
    if (
        active_token_ids is None
        or known_token_ids is None
        or inventory.get("complete") is not True
        or not active_token_ids <= known_token_ids
    ):
        return False
    projection_bound = any(
        inventory.get(key) is not None
        for key in (
            "active_token_ids_source",
            "active_token_ids_receipt_sha256",
            "active_token_ids_observed_at",
        )
    )
    if projection_bound:
        return held_yes_reconcile_receipt_valid(
            inventory=inventory, receipt=receipt
        ) and active_held_yes_balances_ready(
            health=health, active_token_ids=active_token_ids
        )
    try:
        return bool(
            inventory.get("observed_at")
            and age(str(inventory["observed_at"])) <= 15
        )
    except (TypeError, ValueError):
        return False


def _snapshot_hash_valid(value: Mapping[str, Any]) -> bool:
    supplied = value.get("snapshot_sha256")
    unsigned = {key: item for key, item in value.items() if key != "snapshot_sha256"}
    return isinstance(supplied, str) and len(supplied) == 64 and supplied == canonical_sha(unsigned)


def primary_ws_pure_yes_token_snapshots(
    *, manifest: Mapping[str, Any], shard_dir: Path = MARKET_WS_QUOTE_SHARDS
) -> bool:
    """Prove that every current primary CLOB snapshot is an immutable YES book.

    The manifest intentionally stores token books in independent files rather
    than inline.  Checking only its counters would let a stale NO shard look
    healthy after a service cutover.  This is a bounded, streaming read: every
    active token must be represented exactly once and identify as YES.
    """

    try:
        expected = int(manifest.get("materialized_token_count") or 0)
        subscribed = int(manifest.get("subscribed_token_count") or 0)
    except (TypeError, ValueError):
        return False
    active_shards = manifest.get("active_shards")
    active_generations = manifest.get("active_shard_generations")
    if (
        not 0 < expected <= MAX_PRIMARY_WS_TOKEN_SNAPSHOTS
        or subscribed != expected
        or not isinstance(active_shards, list)
        or not isinstance(active_generations, Mapping)
        or not shard_dir.is_dir()
        or shard_dir.is_symlink()
    ):
        return False
    try:
        active_indexes = {int(value) for value in active_shards}
    except (TypeError, ValueError):
        return False
    if not active_indexes or len(active_indexes) != len(active_shards):
        return False
    expected_generations: dict[int, int] = {}
    for index in active_indexes:
        try:
            expected_generations[index] = int(active_generations[str(index)])
        except (KeyError, TypeError, ValueError):
            return False

    files: list[Path] = []
    try:
        for path in shard_dir.iterdir():
            if not path.is_file() or path.is_symlink() or path.suffix != ".json":
                continue
            files.append(path)
            if len(files) > MAX_PRIMARY_WS_TOKEN_SNAPSHOTS:
                return False
    except OSError:
        return False
    if len(files) != expected:
        return False

    scanned_bytes = 0
    token_ids: set[str] = set()
    for path in files:
        try:
            size = path.stat().st_size
            if not 0 < size <= MAX_PRIMARY_WS_TOKEN_SNAPSHOT_BYTES:
                return False
            scanned_bytes += size
            if scanned_bytes > MAX_PRIMARY_WS_SCAN_BYTES:
                return False
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(payload, Mapping):
            return False
        quote = payload.get("quote")
        token_id = str(payload.get("token_id") or "").strip()
        try:
            shard_index = int(payload.get("shard_index"))
            shard_generation = int(payload.get("shard_generation"))
        except (TypeError, ValueError):
            return False
        if (
            payload.get("schema") != PRIMARY_WS_TOKEN_SCHEMA
            or payload.get("source") != "official_polymarket_clob_websocket"
            or payload.get("evidence_only") is not True
            or payload.get("authorizes_order") is not False
            or payload.get("shard_active") is not True
            or shard_index not in active_indexes
            or shard_generation != expected_generations.get(shard_index)
            or not token_id
            or token_id in token_ids
            or not isinstance(quote, Mapping)
            or str(quote.get("token_id") or "").strip() != token_id
            or str(quote.get("side") or "").upper() != "YES"
            or not _snapshot_hash_valid(payload)
        ):
            return False
        token_ids.add(token_id)
    return len(token_ids) == expected


def _utc_age_seconds(value: Any, *, now: datetime) -> float:
    if not isinstance(value, str) or not value:
        return float("inf")
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return float("inf")
    return (now - observed).total_seconds()


def _ordered_token_ids(value: Any) -> list[str] | None:
    tokens = strict_token_id_set(value)
    if tokens is None:
        return None
    ordered = [str(raw).strip() for raw in value]
    return ordered if ordered == sorted(tokens) else None


def _quote_has_fresh_complete_book(
    quote: Any,
    *,
    token_id: str,
    now: datetime,
    max_age_sec: float,
) -> tuple[bool, float]:
    if not isinstance(quote, Mapping):
        return False, float("inf")
    book_age = _utc_age_seconds(quote.get("book_observed_at"), now=now)
    valid = bool(
        quote.get("token_id") == token_id
        and quote.get("source") == "clob_ws"
        and quote.get("source_detail") == "book"
        and isinstance(quote.get("bids"), list)
        and isinstance(quote.get("asks"), list)
        and 0 <= book_age <= max_age_sec
    )
    return valid, book_age


def held_yes_quote_evidence_records(
    *,
    active_token_ids: set[str],
    primary_manifest: Mapping[str, Any],
    auxiliary_manifest: Mapping[str, Any],
    primary_shard_dir: Path,
    auxiliary_shard_dir: Path,
    now: datetime | None = None,
    max_age_sec: float = HELD_YES_QUOTE_MAX_AGE_SEC,
) -> list[dict[str, Any]]:
    """Validate each held-YES token against primary and auxiliary WS evidence.

    Auxiliary evidence is scope-bound to the CLOB reconciler projection.  A
    valid primary shard remains an independently official alternative for a
    token that is also in the current market catalog.  Neither path accepts a
    missing book as an empty book.
    """

    current = now or datetime.now(timezone.utc)
    expected_scope = sorted(active_token_ids)
    expected_scope_sha = hashlib.sha256("\x00".join(expected_scope).encode("utf-8")).hexdigest()
    primary_root_age = _utc_age_seconds(primary_manifest.get("connection_observed_at"), now=current)
    primary_root_valid = bool(
        primary_manifest.get("schema") == PRIMARY_WS_MANIFEST_SCHEMA
        and primary_manifest.get("state") == "connected"
        and primary_manifest.get("source") == "official_polymarket_clob_websocket"
        and primary_manifest.get("evidence_only") is True
        and primary_manifest.get("authorizes_order") is False
        and primary_manifest.get("quote_storage") == "sha256_token_shards_v1"
        and _snapshot_hash_valid(primary_manifest)
        and 0 <= primary_root_age <= max_age_sec
    )
    auxiliary_scope = _ordered_token_ids(auxiliary_manifest.get("scope_token_ids"))
    auxiliary_root_age = _utc_age_seconds(auxiliary_manifest.get("connection_observed_at"), now=current)
    auxiliary_root_valid = bool(
        auxiliary_manifest.get("schema") == HELD_YES_WS_MANIFEST_SCHEMA
        and auxiliary_manifest.get("state") in {"ready", "partial_evidence"}
        and auxiliary_manifest.get("source") == "official_polymarket_clob_websocket"
        and auxiliary_manifest.get("evidence_only") is True
        and auxiliary_manifest.get("authorizes_order") is False
        and auxiliary_manifest.get("quote_storage") == "sha256_token_shards_v1"
        and auxiliary_scope == expected_scope
        and auxiliary_manifest.get("scope_sha256") == expected_scope_sha
        and _snapshot_hash_valid(auxiliary_manifest)
        and 0 <= auxiliary_root_age <= max_age_sec
    )
    records: list[dict[str, Any]] = []
    for token_id in expected_scope:
        filename = f"{hashlib.sha256(token_id.encode('utf-8')).hexdigest()}.json"
        primary_path = primary_shard_dir / filename
        auxiliary_path = auxiliary_shard_dir / filename
        primary_payload: Mapping[str, Any] = {}
        auxiliary_payload: Mapping[str, Any] = {}
        for path, target in ((primary_path, "primary"), (auxiliary_path, "auxiliary")):
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, Mapping):
                    if target == "primary":
                        primary_payload = loaded
                    else:
                        auxiliary_payload = loaded
            except (OSError, json.JSONDecodeError):
                continue
        primary_quote_ok, primary_book_age = _quote_has_fresh_complete_book(
            primary_payload.get("quote"), token_id=token_id, now=current, max_age_sec=max_age_sec
        )
        primary_valid = bool(
            primary_root_valid
            and primary_payload.get("schema") == PRIMARY_WS_TOKEN_SCHEMA
            and primary_payload.get("token_id") == token_id
            and primary_payload.get("shard_active") is True
            and primary_payload.get("source") == "official_polymarket_clob_websocket"
            and primary_payload.get("evidence_only") is True
            and primary_payload.get("authorizes_order") is False
            and _snapshot_hash_valid(primary_payload)
            and primary_quote_ok
        )
        auxiliary_quote_ok, auxiliary_book_age = _quote_has_fresh_complete_book(
            auxiliary_payload.get("quote"), token_id=token_id, now=current, max_age_sec=max_age_sec
        )
        auxiliary_scope_tokens = _ordered_token_ids(auxiliary_payload.get("scope_token_ids"))
        auxiliary_valid = bool(
            auxiliary_root_valid
            and token_id in set(auxiliary_manifest.get("complete_book_token_ids") or [])
            and auxiliary_payload.get("schema") == HELD_YES_WS_TOKEN_SCHEMA
            and auxiliary_payload.get("token_id") == token_id
            and auxiliary_payload.get("scope_sha256") == expected_scope_sha
            and auxiliary_scope_tokens == expected_scope
            and auxiliary_payload.get("source") == "official_polymarket_clob_websocket"
            and auxiliary_payload.get("evidence_only") is True
            and auxiliary_payload.get("authorizes_order") is False
            and _snapshot_hash_valid(auxiliary_payload)
            and auxiliary_quote_ok
        )
        blocking_reasons: list[str] = []
        if not (primary_valid or auxiliary_valid):
            if not primary_root_valid:
                blocking_reasons.append("primary_manifest_invalid_or_stale")
            if not primary_path.is_file():
                blocking_reasons.append("primary_token_shard_missing")
            elif not _snapshot_hash_valid(primary_payload):
                blocking_reasons.append("primary_token_shard_invalid")
            elif (
                primary_payload.get("schema") != PRIMARY_WS_TOKEN_SCHEMA
                or primary_payload.get("token_id") != token_id
                or primary_payload.get("shard_active") is not True
            ):
                blocking_reasons.append("primary_token_scope_invalid")
            elif not primary_quote_ok:
                blocking_reasons.append("primary_complete_book_missing_or_stale")
            if not auxiliary_root_valid:
                blocking_reasons.append("auxiliary_manifest_invalid_or_stale")
            if token_id not in set(auxiliary_manifest.get("complete_book_token_ids") or []):
                blocking_reasons.append("auxiliary_manifest_missing_complete_book")
            if not auxiliary_path.is_file():
                blocking_reasons.append("auxiliary_token_shard_missing")
            elif not _snapshot_hash_valid(auxiliary_payload):
                blocking_reasons.append("auxiliary_token_shard_invalid")
            elif (
                auxiliary_payload.get("schema") != HELD_YES_WS_TOKEN_SCHEMA
                or auxiliary_payload.get("token_id") != token_id
                or auxiliary_payload.get("scope_sha256") != expected_scope_sha
                or _ordered_token_ids(auxiliary_payload.get("scope_token_ids"))
                != expected_scope
            ):
                blocking_reasons.append("auxiliary_token_scope_invalid")
            elif not auxiliary_quote_ok:
                blocking_reasons.append("auxiliary_complete_book_missing_or_stale")
        records.append({
            "token_id": token_id,
            "valid": primary_valid or auxiliary_valid,
            "accepted_source": "auxiliary" if auxiliary_valid else "primary" if primary_valid else "",
            "blocking_reasons": sorted(set(blocking_reasons)),
            "primary": {
                "path": str(primary_path), "valid": primary_valid,
                "snapshot_sha256": primary_payload.get("snapshot_sha256"),
                "source": primary_payload.get("source"),
                "book_observed_at": (primary_payload.get("quote") or {}).get("book_observed_at"),
                "book_age_sec": round(primary_book_age, 3) if primary_book_age != float("inf") else None,
            },
            "auxiliary": {
                "path": str(auxiliary_path), "valid": auxiliary_valid,
                "snapshot_sha256": auxiliary_payload.get("snapshot_sha256"),
                "source": auxiliary_payload.get("source"),
                "book_observed_at": (auxiliary_payload.get("quote") or {}).get("book_observed_at"),
                "book_age_sec": round(auxiliary_book_age, 3) if auxiliary_book_age != float("inf") else None,
            },
        })
    return records


def age(value: str) -> float:
    observed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    return round((datetime.now(timezone.utc) - observed).total_seconds(), 3)


def service_domains(label: str) -> list[str]:
    domains: list[str] = []
    for domain in (f"gui/{os.getuid()}", "system"):
        result = subprocess.run(("launchctl", "print", f"{domain}/{label}"), capture_output=True, text=True)
        output = result.stdout + result.stderr
        if result.returncode == 0 and "Could not find service" not in output and "Bad request" not in output:
            if domain == "system":
                pid_match = re.search(r"\n\s*pid = (\d+)", output)
                if pid_match:
                    process = subprocess.run(
                        ("ps", "-p", pid_match.group(1), "-o", "command="),
                        capture_output=True, text=True,
                    )
                    if "/bin/sleep 2147483647" in process.stdout:
                        continue
            domains.append(domain)
    return domains


def loaded(label: str) -> bool:
    return bool(service_domains(label))


def loaded_weather_labels() -> set[str]:
    result = subprocess.run(("launchctl", "list"), capture_output=True, text=True, check=True)
    labels: set[str] = set()
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if parts and (parts[-1].startswith("com.weather.") or parts[-1].startswith("com.xiong.weather.")):
            labels.add(parts[-1])
    for label in CORE | set(LEGACY) | {"com.weather.monitor.daemon", "com.xiong.weather.ui"}:
        if loaded(label):
            labels.add(label)
    return labels


def awc_global_station_contract(
    *,
    eligible_stations: set[str],
    registry_lanes_by_station: Mapping[str, set[str]],
    source_routes: Mapping[str, Any],
    source_providers: Mapping[str, Any],
) -> list[str]:
    """Validate the global formal AWC lane without touching execution.

    Every fresh, eligible station must appear exactly once in the AWC formal
    lane.  AMSC may remain configured for historical audit, but its adapter
    and its registry lane are forbidden from the real-time projection.
    """

    failures: list[str] = []
    for station in sorted(eligible_stations):
        provider_id = str(source_routes.get(station) or "")
        provider = source_providers.get(provider_id)
        adapter = (
            str(provider.get("adapter") or "")
            if isinstance(provider, Mapping)
            else ""
        )
        expected_lane = "awc" if provider_id and isinstance(provider, Mapping) else ""
        actual_lanes = {
            str(lane or "").strip().lower()
            for lane in registry_lanes_by_station.get(station, set())
        }
        if adapter == "amsc":
            failures.append(f"{station}:formal_awc_route_uses_amsc_adapter")
        if not expected_lane or actual_lanes != {expected_lane}:
            failures.append(
                f"{station}:expected={expected_lane or 'invalid'}:actual={','.join(sorted(actual_lanes)) or 'missing'}"
            )
    return failures


def gateway_health() -> dict[str, Any]:
    with urllib.request.urlopen("http://127.0.0.1:8765/health", timeout=2.0) as response:
        return dict(json.loads(response.read().decode("utf-8")))


def main() -> int:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    registry = conn.execute(
        """SELECT MAX(refreshed_at) refreshed_at,
                  COUNT(DISTINCT CASE WHEN source_eligible=1 AND source_lane='amsc' THEN slug||'|'||event_date END) amsc,
                  COUNT(DISTINCT CASE WHEN source_eligible=1 AND source_lane='awc' THEN slug||'|'||event_date END) awc,
                  COUNT(DISTINCT CASE WHEN source_eligible=0 THEN slug||'|'||event_date END) blocked,
                  SUM(CASE WHEN source_eligible=1
                             AND LOWER(COALESCE(source_lane,''))<>'awc'
                           THEN 1 ELSE 0 END) non_awc_eligible
           FROM reported_trajectory_market_registry"""
    ).fetchone()
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='reported_trajectory_market_registry_refresh_state'"
    ).fetchone():
        refresh_row = conn.execute(
            "SELECT last_successful_refresh_at "
            "FROM reported_trajectory_market_registry_refresh_state "
            "WHERE singleton=1"
        ).fetchone()
        if refresh_row is not None:
            registry = dict(registry)
            registry["refreshed_at"] = str(refresh_row[0])
    polls = {
        str(row["source_lane"]): dict(row)
        for row in conn.execute(
            """SELECT p.* FROM reported_trajectory_source_polls p
               JOIN (SELECT source_lane,MAX(response_completed_at) t FROM reported_trajectory_source_polls GROUP BY source_lane) x
                 ON x.source_lane=p.source_lane AND x.t=p.response_completed_at"""
        )
    }
    latest_awc_poll = conn.execute(
        "SELECT MAX(response_completed_at) FROM reported_trajectory_source_polls WHERE source_lane='awc'"
    ).fetchone()[0]
    awc_enabled = config.get("production_source_allowlist") == ["awc"]
    awc_poll_ok = bool(latest_awc_poll)
    status = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {}
    market_ws = json.loads(MARKET_WS_HEARTBEAT.read_text(encoding="utf-8")) if MARKET_WS_HEARTBEAT.exists() else {}
    market_manifest = json.loads(MARKET_WS_MANIFEST.read_text(encoding="utf-8")) if MARKET_WS_MANIFEST.exists() else {}
    held_yes_inventory = json.loads(HELD_YES_INVENTORY.read_text(encoding="utf-8")) if HELD_YES_INVENTORY.exists() else {}
    try:
        held_yes_auxiliary_value = (
            json.loads(HELD_YES_WS_MANIFEST.read_text(encoding="utf-8"))
            if HELD_YES_WS_MANIFEST.exists()
            else {}
        )
        held_yes_auxiliary_manifest = (
            dict(held_yes_auxiliary_value)
            if isinstance(held_yes_auxiliary_value, Mapping)
            else {}
        )
    except (OSError, json.JSONDecodeError):
        held_yes_auxiliary_manifest = {}
    try:
        held_yes_reconcile_value = (
            json.loads(HELD_YES_CLOB_RECONCILE.read_text(encoding="utf-8"))
            if HELD_YES_CLOB_RECONCILE.exists()
            else {}
        )
        held_yes_reconcile = (
            dict(held_yes_reconcile_value)
            if isinstance(held_yes_reconcile_value, Mapping)
            else {}
        )
    except (OSError, json.JSONDecodeError):
        held_yes_reconcile = {}
    held_yes_tokens = {str(value) for value in held_yes_inventory.get("token_ids") or [] if str(value)}
    active_held_yes_tokens = {
        str(value) for value in held_yes_inventory.get("active_token_ids") or [] if str(value)
    }
    try:
        held_yes_registry_records = classify_held_yes_tokens(
            conn,
            token_ids=sorted(active_held_yes_tokens),
            official_source_lane="awc",
        )
    except (RuntimeError, ValueError):
        # A registry read failure must never turn a held token into a quote
        # candidate.  Preserve it as an explicit unresolved classification so
        # the separate classification check remains fail closed.
        held_yes_registry_records = [{
            "token_id": token_id,
            "classification": "REGISTRY_UNRESOLVED",
            "registry_resolved": False,
            "quote_required": False,
            "tradable": False,
            "non_tradeable_reasons": ["official_registry_classification_unavailable"],
            "registry": None,
        } for token_id in sorted(active_held_yes_tokens)]
    quote_required_held_yes_tokens = quote_required_token_ids(
        held_yes_registry_records
    )
    held_yes_registry_is_classified = registry_classification_complete(
        held_yes_registry_records
    )
    held_yes_quote_records = held_yes_quote_evidence_records(
        active_token_ids=quote_required_held_yes_tokens,
        primary_manifest=(market_manifest if isinstance(market_manifest, Mapping) else {}),
        auxiliary_manifest=held_yes_auxiliary_manifest,
        primary_shard_dir=MARKET_WS_QUOTE_SHARDS,
        auxiliary_shard_dir=HELD_YES_WS_QUOTE_SHARDS,
    )
    held_yes_quote_files_ready = all(record["valid"] for record in held_yes_quote_records)
    health = gateway_health()
    reconcile_projection_bound = any(
        held_yes_inventory.get(key) is not None
        for key in (
            "active_token_ids_source",
            "active_token_ids_receipt_sha256",
            "active_token_ids_observed_at",
        )
    )
    held_yes_reconcile_receipt_is_valid = held_yes_reconcile_receipt_valid(
        inventory=held_yes_inventory,
        receipt=held_yes_reconcile,
    )
    held_yes_balances_are_ready = active_held_yes_balances_ready(
        health=health,
        active_token_ids=active_held_yes_tokens,
    )
    held_yes_inventory_is_fresh = held_yes_inventory_fresh(
        inventory=held_yes_inventory,
        receipt=held_yes_reconcile,
        health=health,
    )
    weather_labels = loaded_weather_labels()
    source_registry = load_official_source_config()
    eligible_stations = {
        str(row[0])
        for row in conn.execute(
            """SELECT DISTINCT station_id FROM reported_trajectory_market_registry
               WHERE source_eligible=1 AND COALESCE(station_id,'')<>''
                 AND julianday(refreshed_at)>=julianday('now','-10 minutes')"""
        )
    }
    source_routes = source_registry.get("station_routes") or {}
    source_providers = source_registry.get("providers") or {}
    registry_lanes_by_station: dict[str, set[str]] = {}
    for row in conn.execute(
        """SELECT DISTINCT station_id,source_lane
             FROM reported_trajectory_market_registry
             WHERE source_eligible=1 AND COALESCE(station_id,'')<>''
               AND julianday(refreshed_at)>=julianday('now','-10 minutes')"""
    ):
        station = str(row[0] or "").strip().upper()
        lane = str(row[1] or "").strip().lower()
        if station:
            registry_lanes_by_station.setdefault(station, set()).add(lane)
    route_lane_contract_failures = awc_global_station_contract(
        eligible_stations=eligible_stations,
        registry_lanes_by_station=registry_lanes_by_station,
        source_routes=source_routes,
        source_providers=source_providers,
    )
    origin_table_exists = bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reported_trajectory_observation_origins'"
        ).fetchone()
    )
    latest_awc_poll_json = json.loads(str(polls.get("awc", {}).get("poll_json") or "{}"))
    checks = {
        "production_source_allowlist_valid": awc_enabled,
        "legacy_execution_disabled": not any(loaded(label) for label in LEGACY),
        "unique_owner_loaded": loaded("com.weather.weather_basket_yes"),
        "market_ws_loaded": loaded("com.weather.market_ws"),
        "clob_gateway_single_domain_loaded": loaded("com.weather.clob_order_service"),
        "core_services_single_domain": all(len(service_domains(label)) == 1 for label in CORE),
        "only_core_weather_services_loaded": weather_labels == CORE,
        "ui_disabled": not loaded("com.xiong.weather.ui"),
        "monitor_disabled": not loaded("com.weather.monitor.daemon"),
        "hard_no_execution_removed": (
            config.get("schema") == "reported_trajectory_awc_ingress_config_v1"
            and config.get("strategy_id") == "awc_canonical_truth_ingress_v1"
            and "order_policy" not in config
            and "position_policy" not in config
            and "exit_policy" not in config
            and "prewarm_policy" not in config
        ),
        # This applies only to the realtime truth poll.  The independently
        # bounded forward AWC TAF lane below remains available to form P0-M
        # once it has a complete revision chain and exact OOS artifact.
        "truth_ingress_taf_fetch_disabled": all(
            config["source_policy"][lane]["taf_requested"] is False
            for lane in ("amsc", "awc")
        ),
        "forward_awc_taf_capture_isolated": (
            forward_awc_taf_capture_isolated(status)
        ),
        "awc_production_ingest_contract": config["source_policy"]["awc"].get("production_ingest") is awc_enabled,
        "global_awc_primary_contract": (
            config["source_policy"]["amsc"].get("city_slugs") == []
            and config["source_policy"]["amsc"].get("enabled") is False
            and config["source_policy"]["amsc"].get("role") == "retired_history_only"
            and config["source_policy"]["awc"].get("production_ingest") is True
            and config["source_policy"]["awc"].get("exclude_city_slugs") == []
            and config["source_policy"]["awc"].get("global_primary") is True
            and config["source_policy"]["awc"].get("replaces_amsc") is True
            and config.get("guards", {}).get("source_lanes_disjoint") is True
            and config.get("guards", {}).get("awc_fallback_for_amsc_forbidden") is True
            and config.get("guards", {}).get("amsc_fallback_for_awc_forbidden") is True
        ),
        "awc_poll_fresh_or_suppressed": awc_poll_ok,
        "country_source_registry_complete": not (eligible_stations - set(source_routes)),
        "country_source_routes_valid": all(
            str(source_routes.get(station) or "") in source_providers for station in eligible_stations
        ),
        # The registry is a single global AWC truth projection; no city-based
        # AMSC exception and no cross-lane fallback can become formal.
        "station_authority_lane_contract": not route_lane_contract_failures,
        "country_origin_audit_schema": origin_table_exists,
        # The production resident is intentionally AWC-only.  A historical
        # country-router receipt may still be present in old status files, but
        # the current live heartbeat must prove one global AWC batch with no
        # fallback reasons/provider race.
        "awc_country_router_live": (
            latest_awc_poll_json.get("transport") in {
                "official_awc_raw_metar_batch_direct",
                "official_awc_raw_metar_batch_curl_system_ca",
                "official_awc_global_metar_cache",
                "official_awc_global_metar_cache_curl_system_ca",
                "official_awc_api_batch_fallback",
                # Preserve acceptance of a pre-cutover receipt while the
                # daemon heartbeat rolls over; it is not a live-path target.
                "country_official_router_with_explicit_awc_fallback",
                "awc_global_primary",
            }
            and not (latest_awc_poll_json.get("fallback_reasons") or {})
            and (
                any(
                    str(item.get("state") or "") == "awc_global_primary"
                    for item in latest_awc_poll_json.get("provider_states") or []
                    if isinstance(item, Mapping)
                )
                or latest_awc_poll_json.get("transport")
                in {
                    "country_official_router_with_explicit_awc_fallback",
                    "awc_global_primary",
                }
            )
        ),
        "awc_release_latency_policy_exact": config.get("poll_policy", {}).get("awc_relay_release_delay_sec") == 30
        and config.get("poll_policy", {}).get("awc_release_burst_retry_sec") == 0.25
        and config.get("poll_policy", {}).get("awc_release_burst_window_sec") == 90
        and config.get("poll_policy", {}).get("awc_background_poll_sec") == 2,
        "awc_provider_incremental_commit_live": (
            (
                any(
                    str(item.get("state") or "") == "awc_global_primary"
                    for item in latest_awc_poll_json.get("provider_states") or []
                    if isinstance(item, Mapping)
                )
            )
            or "provider_completed_after_router_start_ms" in latest_awc_poll_json
        ),
        "source_lanes_disjoint": int(registry["non_awc_eligible"] or 0) == 0,
        "amsc_registry_absent_from_production": int(registry["amsc"] or 0) == 0,
        "awc_registry_nonempty": int(registry["awc"] or 0) > 0,
        "registry_fresh": bool(registry["refreshed_at"] and age(str(registry["refreshed_at"])) <= 600),
        "daemon_heartbeat_fresh": bool(status.get("heartbeat_at") and age(str(status["heartbeat_at"])) <= 45),
        "market_ws_heartbeat_fresh": bool(
            market_ws.get("updated_at") and age(str(market_ws["updated_at"])) <= 15
        ),
        "market_ws_connected": market_ws.get("ok") is True and market_ws.get("status") == "connected",
        "market_ws_no_tick_archive": int(market_ws.get("ticks_written") or 0) == 0,
        "market_ws_token_shards": (
            market_manifest.get("state") == "connected"
            and market_manifest.get("quote_storage") == "sha256_token_shards_v1"
            and market_manifest.get("quotes_by_token") == {}
        ),
        "market_ws_active_shards_materialized": (
            int(market_manifest.get("expected_shards") or 0) > 0
            and 0 < len(market_manifest.get("active_shards") or [])
            <= int(market_manifest.get("expected_shards") or 0)
            and int(market_manifest.get("subscribed_token_count") or 0) > 0
            and int(market_manifest.get("materialized_token_count") or 0)
            == int(market_manifest.get("subscribed_token_count") or 0)
        ),
        "market_ws_pure_yes_token_snapshots": primary_ws_pure_yes_token_snapshots(
            manifest=(market_manifest if isinstance(market_manifest, Mapping) else {}),
        ),
        "held_yes_inventory_fresh": held_yes_inventory_is_fresh,
        "held_yes_clob_reconcile_receipt_valid": (
            not reconcile_projection_bound or held_yes_reconcile_receipt_is_valid
        ),
        "held_yes_registry_classified": held_yes_registry_is_classified,
        "active_held_yes_quotes_materialized": held_yes_quote_files_ready,
        "active_held_yes_balances_ready": held_yes_balances_are_ready,
        "registry_watchdog_healthy": (
            status.get("registry_watchdog", {}).get("healthy") is True
        ),
        "clob_gateway_ready": (
            health.get("ready") is True
            and health.get("read_only") is True
            and health.get("live_pause_present") is True
        ),
        "clob_gateway_prefetched": health.get("local_prefetched_evidence_ready") is True,
        "clob_gateway_clean": not health.get("inflight_count") and not health.get("idempotency_unresolved_count"),
    }
    latest_poll_summary = {}
    for lane, row in polls.items():
        poll_json = json.loads(str(row.get("poll_json") or "{}"))
        latest_poll_summary[lane] = {
            "response_completed_at": row["response_completed_at"],
            "age_sec": age(str(row["response_completed_at"])),
            "transport_duration_ms": round(
                1000 * (
                    datetime.fromisoformat(str(row["response_completed_at"]).replace("Z", "+00:00"))
                    - datetime.fromisoformat(str(row["request_started_at"]).replace("Z", "+00:00"))
                ).total_seconds(), 3
            ),
            "canonical_processing_ms": row["duration_ms"],
            "report_count": row["report_count"],
            "new_report_count": row["new_report_count"],
            "transport": poll_json.get("transport"),
            "taf_requested": poll_json.get("taf_requested"),
        }
        checks[f"{lane}_poll_temperature_only"] = poll_json.get("taf_requested") is False
    output = {
        "schema": "weather_basket_yes_production_acceptance_v1",
        "ok": all(checks.values()),
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "checks": checks,
        "registry": {
            "refreshed_at": registry["refreshed_at"], "amsc_market_days": registry["amsc"],
            "awc_market_days": registry["awc"], "blocked_market_days": registry["blocked"],
        },
        "country_sources": {
            "eligible_station_count": len(eligible_stations),
            "unmapped_stations": sorted(eligible_stations - set(source_routes)),
            "configured_provider_count": len(source_providers),
            "station_authority_lane_contract_failures": route_lane_contract_failures,
            "latest_awc_provider_states": latest_awc_poll_json.get("provider_states") or [],
            "latest_awc_fallback_reasons": latest_awc_poll_json.get("fallback_reasons") or {},
        },
        "latest_polls": latest_poll_summary,
        "core_services": sorted(weather_labels),
        "market_ws": market_ws,
        "clob_gateway": {
            key: health.get(key)
            for key in (
                "pid", "ready", "ws_snapshot_ready", "ws_partial_snapshot_ready", "ws_snapshot_age_ms",
                "ws_active_shards", "ws_expected_shards", "ws_materialized_token_count",
                "ws_subscribed_token_count", "collateral_cache_ready",
                "collateral_balance_fetch_disabled", "read_only", "live_pause_present",
                "active_held_yes_balance_ready",
                "conditional_balance_ready_token_ids",
                "all_shards_prefetched_evidence_ready",
                "local_prefetched_evidence_ready", "collateral_cache_age_ms",
                "inflight_count", "idempotency_unresolved_count",
            )
        },
        "held_yes_reconcile": {
            "path": str(HELD_YES_CLOB_RECONCILE),
            "projection_bound": reconcile_projection_bound,
            "receipt_valid": held_yes_reconcile_receipt_is_valid,
            "schema": held_yes_reconcile.get("schema"),
            "source": held_yes_reconcile.get("source"),
            "receipt_sha256": held_yes_reconcile.get("receipt_sha256"),
            "nonzero_token_ids": held_yes_reconcile.get("nonzero_token_ids"),
        },
        "held_yes_quote_evidence": {
            "nonzero_token_ids": sorted(active_held_yes_tokens),
            "quote_required_token_ids": sorted(quote_required_held_yes_tokens),
            "nontradeable_token_ids": sorted(
                str(record["token_id"])
                for record in held_yes_registry_records
                if record.get("quote_required") is not True
                and record.get("registry_resolved") is True
            ),
            "registry_classified": held_yes_registry_is_classified,
            "registry": held_yes_registry_records,
            "auxiliary_manifest_path": str(HELD_YES_WS_MANIFEST),
            "auxiliary_manifest_schema": held_yes_auxiliary_manifest.get("schema"),
            "auxiliary_manifest_state": held_yes_auxiliary_manifest.get("state"),
            "auxiliary_manifest_snapshot_sha256": held_yes_auxiliary_manifest.get("snapshot_sha256"),
            "tokens": held_yes_quote_records,
        },
    }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
