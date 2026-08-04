#!/usr/bin/env python3
"""Materialize station or explicit global ΔBasketScore v2 no-live authority.

``station_id='*'`` is a production-no-live-only bootstrap fallback.  Exact
station authority always wins in the resolver.  The fallback exists only so
new stations can materialize pre-event decisions and observational facts
before station-specific historical strict-as-of OOS parameters are promoted.
Forward observations are never OOS and never a hard gate.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.basket_kernel.contracts import (
    ContractError,
    content_sha256,
)
from bayes_basket_target.delta_v2_artifact import (
    ACTIVATION_SCHEMA,
    ARTIFACT_SCHEMA,
    PROMOTION_SCHEMA,
    commit_delta_v2_authority,
    resolve_delta_v2_policy,
)
from bayes_basket_target.delta_v2_fill_artifact import (
    FILL_DISTRIBUTION_ARTIFACT_SCHEMA,
    FILL_DISTRIBUTION_PROMOTION_SCHEMA,
    commit_delta_v2_fill_distribution_artifact,
)
from bayes_basket_target.resource_bounds import MAX_TARGET_WAL_BYTES
from bayes_basket_target.ssot import apply_delta_v2_authority_schema


DEFAULT_DB = (ROOT / "data/bayes_basket_target_ssot.db").resolve()
DEFAULT_MANIFEST = (
    ROOT
    / "config/trading/"
    "delta_basket_score_v2_rkpk_no_live_bootstrap_v1.json"
).resolve()
SERVICE_LABEL = f"gui/{os.getuid()}/com.weather.weather_basket_yes"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_WAL_BYTES = MAX_TARGET_WAL_BYTES
MIN_FREE_BYTES = 1024 * 1024 * 1024
MAX_FILL_PRICE_POINTS = 1_000


def _service_running() -> bool:
    result = subprocess.run(
        ["/bin/launchctl", "print", SERVICE_LABEL],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and "state = running" in result.stdout


def _load_manifest(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve(strict=True)
    size = int(resolved.stat().st_size)
    if size <= 0 or size > MAX_MANIFEST_BYTES:
        raise ContractError("delta_v2_bootstrap_manifest_size_invalid")
    raw = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ContractError("delta_v2_bootstrap_manifest_mapping_required")
    if raw.get("schema") != (
        "delta_basket_score_v2_no_live_bootstrap_manifest_v1"
    ):
        raise ContractError("delta_v2_bootstrap_manifest_schema_invalid")
    if (
        raw.get("authority_scope") != "PRODUCTION_NO_LIVE"
        or raw.get("decision") != "NO_LIVE_ACTIVATED"
        or raw.get("oos_gate_passed") is not False
        or raw.get("production_no_live") is not True
        or raw.get("live_submit_authorized") is not False
        or raw.get("calls_order_gateway") is not False
        or int(raw.get("orders_submitted", -1)) != 0
    ):
        raise ContractError("delta_v2_bootstrap_boundary_invalid")
    if (
        int(raw.get("sample_n", -1)) != 0
        or int(raw.get("holdout_n", -1)) != 0
        or int(raw.get("consecutive_forward_windows", -1)) != 0
        or raw.get("compared_baselines") != []
        or raw.get("oos_metrics") != {}
    ):
        raise ContractError("delta_v2_bootstrap_must_not_claim_oos")
    maturity_bins = raw.get("maturity_bins")
    if (
        not isinstance(maturity_bins, list)
        or not maturity_bins
        or len(maturity_bins) > 4
        or len(set(maturity_bins)) != len(maturity_bins)
        or any(
            not isinstance(item, str) or not item.strip()
            for item in maturity_bins
        )
    ):
        raise ContractError("delta_v2_bootstrap_maturity_bins_invalid")
    generation = raw.get("activation_generation", 1)
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
    ):
        raise ContractError("delta_v2_bootstrap_activation_generation_invalid")
    raw["activation_generation"] = generation
    # A mill-tick manifest would otherwise repeat 999 mechanical values.
    # Materialize the bounded grid once from the declared unit-dividing tick.
    # The generated values remain part of every content-addressed artifact.
    fill_prices = _fill_price_grid(raw)
    if raw.get("price_grid") is None:
        raw["price_grid"] = list(fill_prices)
    return raw


def _fill_price_grid(manifest: dict[str, Any]) -> tuple[float, ...]:
    tick_raw = manifest.get("fill_price_tick_size")
    if tick_raw is None:
        values = manifest.get("price_grid")
        if not isinstance(values, list) or not values:
            raise ContractError("delta_v2_bootstrap_price_grid_invalid")
        prices = tuple(float(value) for value in values)
    else:
        try:
            tick = Decimal(str(tick_raw))
        except Exception as exc:
            raise ContractError(
                "delta_v2_bootstrap_fill_price_tick_invalid"
            ) from exc
        if tick <= 0 or tick >= 1:
            raise ContractError("delta_v2_bootstrap_fill_price_tick_invalid")
        point_count = int(Decimal("1") / tick) - 1
        if tick * Decimal(point_count + 1) != Decimal("1"):
            raise ContractError(
                "delta_v2_bootstrap_fill_price_tick_not_unit_divisor"
            )
        prices = tuple(
            float(tick * Decimal(index))
            for index in range(1, point_count + 1)
        )
    if (
        len(prices) > MAX_FILL_PRICE_POINTS
        or len(set(prices)) != len(prices)
        or any(not 0.0 < price < 1.0 for price in prices)
    ):
        raise ContractError("delta_v2_bootstrap_fill_price_grid_invalid")
    return prices


def _fill_price_id(manifest: dict[str, Any], price: float) -> str:
    tick_raw = manifest.get("fill_price_tick_size")
    if tick_raw is None:
        return f"p{int(round(float(price) * 100)):02d}"
    tick = Decimal(str(tick_raw))
    numerator = int(Decimal(str(price)) / tick)
    denominator = int(Decimal("1") / tick)
    width = len(str(denominator - 1))
    return f"p{numerator:0{width}d}of{denominator}"


def _sha_body(raw: dict[str, Any]) -> dict[str, Any]:
    result = dict(raw)
    result["content_sha256"] = content_sha256(result)
    return result


def _authority_packet(
    manifest: dict[str, Any],
    *,
    maturity_bin: str,
) -> dict[str, Any]:
    fill_prices = _fill_price_grid(manifest)
    binding_price_scope = (
        {"price_grid": manifest["price_grid"]}
        if manifest.get("fill_price_tick_size") is None
        else {
            "fill_price_tick_size": manifest["fill_price_tick_size"],
            "fill_price_grid": fill_prices,
        }
    )
    fill_binding_sha = content_sha256(
        {
            "schema": "delta_v2_no_live_fill_prior_binding_v1",
            "bootstrap_id": manifest["bootstrap_id"],
            "station_id": manifest["station_id"],
            "stage": manifest["stage"],
            "season_bin": manifest["season_bin"],
            "maturity_bin": maturity_bin,
            "liquidity_bin": manifest["liquidity_bin"],
            **binding_price_scope,
            "timeout_grid_seconds": manifest["timeout_grid_seconds"],
            "fill_prior": manifest["fill_prior"],
        }
    )
    training_evidence_sha = content_sha256(
        {
            "schema": "delta_v2_no_live_bootstrap_evidence_v1",
            "bootstrap_id": manifest["bootstrap_id"],
            "oos_gate_passed": False,
            "historical_strict_oos_required_for_live_or_parameter_promotion": True,
            "future_data_counts_as_oos": False,
        }
    )
    common = {
        key: manifest[key]
        for key in (
            "station_id",
            "stage",
            "season_bin",
            "liquidity_bin",
            "train_start_date",
            "train_end_date",
            "holdout_start_date",
            "holdout_end_date",
            "sample_n",
            "holdout_n",
            "consecutive_forward_windows",
            "compared_baselines",
            "oos_metrics",
            "oos_gate_version",
            "oos_gate_passed",
            "trained_through_utc",
            "valid_from_utc",
            "valid_until_utc",
            "authority_scope",
        )
    }
    common["maturity_bin"] = maturity_bin
    scope_id = f"{manifest['bootstrap_id']}-{maturity_bin}"
    artifact = _sha_body(
        {
            "schema": ARTIFACT_SCHEMA,
            "artifact_id": f"delta-v2-policy-{scope_id}",
            **common,
            "strategy_version": manifest["strategy_version"],
            "value_weights": manifest["value_weights"],
            "execution_weights": manifest["execution_weights"],
            "cash_clip_grid": manifest["cash_clip_grid"],
            "price_grid": manifest["price_grid"],
            "timeout_grid_seconds": manifest["timeout_grid_seconds"],
            "fill_distribution_artifact_sha256": fill_binding_sha,
            "training_evidence_sha256": training_evidence_sha,
        }
    )
    promotion = _sha_body(
        {
            "schema": PROMOTION_SCHEMA,
            "promotion_id": (
                f"delta-v2-policy-no-live-{scope_id}"
            ),
            "artifact_id": artifact["artifact_id"],
            "decision_seq": 1,
            "decision": "NO_LIVE_ACTIVATED",
            "gate_version": manifest["oos_gate_version"],
            "decided_at_utc": manifest["decided_at_utc"],
            "receipt_sha256": content_sha256(
                {
                    "schema": "delta_v2_no_live_activation_receipt_v1",
                    "bootstrap_id": scope_id,
                }
            ),
        }
    )
    activation = _sha_body(
        {
            "schema": ACTIVATION_SCHEMA,
            "activation_id": (
                f"delta-v2-policy-activation-{scope_id}"
            ),
            "artifact_id": artifact["artifact_id"],
            "station_id": manifest["station_id"],
            "stage": manifest["stage"],
            "season_bin": manifest["season_bin"],
            "maturity_bin": maturity_bin,
            "liquidity_bin": manifest["liquidity_bin"],
            "strategy_version": manifest["strategy_version"],
            "generation": 1,
            "valid_from_utc": manifest["valid_from_utc"],
            "valid_until_utc": manifest["valid_until_utc"],
            "activated_at_utc": manifest["decided_at_utc"],
            "authority_scope": "PRODUCTION_NO_LIVE",
        }
    )
    fill_packets: list[dict[str, Any]] = []
    timeout = float(manifest["timeout_grid_seconds"][0])
    activation["generation"] = int(manifest["activation_generation"])
    activation["content_sha256"] = content_sha256(
        {
            key: value
            for key, value in activation.items()
            if key != "content_sha256"
        }
    )
    for price in fill_prices:
        fill_artifact = _sha_body(
            {
                "schema": FILL_DISTRIBUTION_ARTIFACT_SCHEMA,
                "artifact_id": (
                    f"delta-v2-fill-{scope_id}-"
                    f"{_fill_price_id(manifest, float(price))}"
                ),
                **common,
                "bucket": "*",
                "price": float(price),
                "timeout_seconds": timeout,
                **manifest["fill_prior"],
                "fill_distribution_artifact_sha256": fill_binding_sha,
            }
        )
        fill_promotion = _sha_body(
            {
                "schema": FILL_DISTRIBUTION_PROMOTION_SCHEMA,
                "promotion_id": (
                    f"delta-v2-fill-no-live-{fill_artifact['artifact_id']}"
                ),
                "artifact_id": fill_artifact["artifact_id"],
                "decision_seq": 1,
                "decision": "NO_LIVE_ACTIVATED",
                "gate_version": manifest["oos_gate_version"],
                "decided_at_utc": manifest["decided_at_utc"],
                "receipt_sha256": content_sha256(
                    {
                        "schema": "delta_v2_fill_no_live_receipt_v1",
                        "artifact_id": fill_artifact["artifact_id"],
                    }
                ),
            }
        )
        fill_packets.append(
            {"artifact": fill_artifact, "promotion": fill_promotion}
        )
    return {
        "artifact": artifact,
        "promotion": promotion,
        "activation": activation,
        "fill_packets": fill_packets,
        "fill_binding_sha256": fill_binding_sha,
    }


def apply_bootstrap(
    *,
    db_path: Path,
    manifest_path: Path = DEFAULT_MANIFEST,
    confirmed_writer_stopped: bool,
) -> dict[str, Any]:
    requested = db_path.expanduser()
    if requested.is_symlink():
        raise ContractError("delta_v2_bootstrap_db_symlink_forbidden")
    database = requested.resolve(strict=True)
    production_target = database == DEFAULT_DB
    if production_target and (
        not confirmed_writer_stopped or _service_running()
    ):
        raise ContractError("delta_v2_bootstrap_writer_not_quiesced")
    wal = Path(str(database) + "-wal")
    wal_bytes = int(wal.stat().st_size) if wal.exists() else 0
    free_bytes = int(shutil.disk_usage(database.parent).free)
    if wal_bytes > MAX_WAL_BYTES:
        raise ContractError("delta_v2_bootstrap_wal_limit_exceeded")
    if free_bytes < MIN_FREE_BYTES:
        raise ContractError("delta_v2_bootstrap_free_space_too_low")
    manifest = _load_manifest(manifest_path)
    packets = [
        _authority_packet(manifest, maturity_bin=str(maturity_bin))
        for maturity_bin in manifest["maturity_bins"]
    ]

    conn = sqlite3.connect(database, timeout=30.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    savepoint = f"delta_v2_bootstrap_{id(conn):x}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        apply_delta_v2_authority_schema(conn, commit=False)
        policy_receipts = []
        fill_receipts = []
        for packet in packets:
            policy_receipts.append(
                commit_delta_v2_authority(
                    conn,
                    artifact=packet["artifact"],
                    promotion=packet["promotion"],
                    activation=packet["activation"],
                )
            )
            fill_receipts.extend(
                commit_delta_v2_fill_distribution_artifact(
                    conn,
                    artifact=item["artifact"],
                    promotion=item["promotion"],
                )
                for item in packet["fill_packets"]
            )
        scope_rows = tuple(
            (
                str(packet["activation"]["station_id"]),
                str(packet["activation"]["stage"]),
                str(packet["activation"]["season_bin"]),
                str(packet["activation"]["maturity_bin"]),
                str(packet["activation"]["liquidity_bin"]),
                str(packet["activation"]["authority_scope"]),
            )
            for packet in packets
        )
        superseded_activations: list[dict[str, Any]] = []
        inactive_legacy_activations: list[dict[str, Any]] = []
        successor_by_scope = {
            (
                str(packet["activation"]["station_id"]),
                str(packet["activation"]["stage"]),
                str(packet["activation"]["season_bin"]),
                str(packet["activation"]["maturity_bin"]),
                str(packet["activation"]["liquidity_bin"]),
                str(packet["activation"]["authority_scope"]),
            ): packet
            for packet in packets
        }
        for scope in scope_rows:
            successor = successor_by_scope[scope]
            rows = conn.execute(
                "SELECT activation_id,artifact_id,strategy_version,generation "
                "FROM basket_delta_v2_policy_activation "
                "WHERE station_id=? AND stage=? AND season_bin=? "
                "AND maturity_bin=? AND liquidity_bin=? "
                "AND authority_scope=? AND artifact_id<>? "
                "ORDER BY activation_id",
                (*scope, str(successor["artifact"]["artifact_id"])),
            ).fetchall()
            for (
                activation_id,
                artifact_id,
                strategy_version,
                generation,
            ) in rows:
                if str(strategy_version) not in {"target-v1", "target-v2"}:
                    raise ContractError(
                        "delta_v2_bootstrap_unknown_competing_strategy:"
                        f"{strategy_version}"
                    )
                if (
                    str(strategy_version) == str(manifest["strategy_version"])
                    and int(generation)
                    >= int(successor["activation"]["generation"])
                ):
                    raise ContractError(
                        "delta_v2_bootstrap_competing_generation_not_older:"
                        f"{artifact_id}"
                    )
                artifact_row = conn.execute(
                    "SELECT artifact_json FROM "
                    "basket_delta_v2_policy_artifact WHERE artifact_id=?",
                    (str(artifact_id),),
                ).fetchone()
                if artifact_row is None:
                    raise ContractError(
                        "delta_v2_bootstrap_competing_artifact_missing"
                    )
                artifact_raw = json.loads(str(artifact_row[0]))
                latest = conn.execute(
                    "SELECT decision_seq,decision FROM "
                    "basket_delta_v2_policy_promotion "
                    "WHERE artifact_id=? ORDER BY decision_seq DESC LIMIT 1",
                    (str(artifact_id),),
                ).fetchone()
                if latest is None:
                    raise ContractError(
                        "delta_v2_bootstrap_competing_promotion_missing"
                    )
                legacy_identity = {
                    "activation_id": str(activation_id),
                    "artifact_id": str(artifact_id),
                    "strategy_version": str(strategy_version),
                    "maturity_bin": scope[3],
                    "generation": int(generation),
                }
                if str(latest[1]) == "SUPERSEDED":
                    inactive_legacy_activations.append(legacy_identity)
                    continue
                if str(latest[1]) not in {
                    "NO_LIVE_ACTIVATED",
                    "PROMOTED",
                }:
                    raise ContractError(
                        "delta_v2_bootstrap_competing_promotion_state_invalid:"
                        f"{latest[1]}"
                    )
                superseded = _sha_body(
                    {
                        "schema": PROMOTION_SCHEMA,
                        "promotion_id": (
                            "delta-v2-policy-superseded-by-"
                            f"{manifest['strategy_version']}-"
                            f"{artifact_id}"
                        ),
                        "artifact_id": str(artifact_id),
                        "decision_seq": int(latest[0]) + 1,
                        "decision": "SUPERSEDED",
                        "gate_version": str(
                            artifact_raw["oos_gate_version"]
                        ),
                        "decided_at_utc": str(manifest["decided_at_utc"]),
                        "receipt_sha256": content_sha256(
                            {
                                "schema": (
                                    "delta_v2_policy_supersession_receipt_v1"
                                ),
                                "superseded_artifact_id": str(artifact_id),
                                "successor_strategy_version": str(
                                    manifest["strategy_version"]
                                ),
                            }
                        ),
                    }
                )
                commit_delta_v2_authority(
                    conn,
                    artifact=artifact_raw,
                    promotion=superseded,
                    activation=None,
                )
                superseded_activations.append(legacy_identity)
                inactive_legacy_activations.append(legacy_identity)
        for packet in packets:
            resolved = resolve_delta_v2_policy(
                conn,
                station_id=str(manifest["station_id"]),
                stage="P1",
                season_bin=str(manifest["season_bin"]),
                maturity_bin=str(packet["activation"]["maturity_bin"]),
                liquidity_bin=str(manifest["liquidity_bin"]),
                strategy_version=str(manifest["strategy_version"]),
                evaluation_at_utc=str(manifest["valid_from_utc"]),
                required_authority_scope=str(manifest["authority_scope"]),
            )
            if resolved.artifact_id != str(packet["artifact"]["artifact_id"]):
                raise ContractError(
                    "delta_v2_bootstrap_active_strategy_not_unique:"
                    f"{resolved.artifact_id}"
                )
        for row in inactive_legacy_activations:
            latest = conn.execute(
                "SELECT decision FROM basket_delta_v2_policy_promotion "
                "WHERE artifact_id=? ORDER BY decision_seq DESC LIMIT 1",
                (row["artifact_id"],),
            ).fetchone()
            if latest is None or str(latest[0]) != "SUPERSEDED":
                raise ContractError(
                    "delta_v2_bootstrap_legacy_strategy_still_resolvable"
                )
        foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise ContractError("delta_v2_bootstrap_foreign_key_check_failed")
        conn.execute(f"RELEASE {savepoint}")
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check != "ok":
            raise ContractError("delta_v2_bootstrap_quick_check_failed")
    except BaseException:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        conn.close()
        raise
    conn.close()
    rows_written = sum(
        int(item["rows_written"]) for item in policy_receipts
    ) + sum(
        int(item["rows_written"]) for item in fill_receipts
    ) + len(superseded_activations)
    return {
        "schema": "delta_basket_score_v2_no_live_bootstrap_receipt_v1",
        "state": "COMMITTED" if rows_written else "UNCHANGED_ZERO_WRITE",
        "database": str(database),
        "production_target": production_target,
        "station_id": manifest["station_id"],
        "maturity_bins": list(manifest["maturity_bins"]),
        "policy_artifact_ids": [
            packet["artifact"]["artifact_id"] for packet in packets
        ],
        "policy_artifact_sha256s": [
            packet["artifact"]["content_sha256"] for packet in packets
        ],
        "fill_binding_sha256s": [
            packet["fill_binding_sha256"] for packet in packets
        ],
        "fill_artifact_count": len(fill_receipts),
        "superseded_activation_count": len(superseded_activations),
        "superseded_activations": superseded_activations,
        "inactive_legacy_activation_count": len(
            inactive_legacy_activations
        ),
        "rows_written": rows_written,
        "quick_check": quick_check,
        "oos_gate_passed": False,
        "future_data_counts_as_oos": False,
        "future_data_blocks_production_no_live": False,
        "future_data_blocks_live_or_parameter_promotion": False,
        "historical_strict_oos_required_for_live_or_parameter_promotion": True,
        "execution_mode": "production_no_live",
        "live_submit_authorized": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "created_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--confirm-reported-trajectory-no-stopped",
        action="store_true",
    )
    args = parser.parse_args()
    if not args.apply:
        print(
            json.dumps(
                {
                    "schema": (
                        "delta_basket_score_v2_no_live_bootstrap_dry_run_v1"
                    ),
                    "database": str(args.db.expanduser().resolve()),
                    "manifest": str(args.manifest.expanduser().resolve()),
                    "execution_mode": "production_no_live",
                    "oos_gate_passed": False,
                    "live_submit_authorized": False,
                    "calls_order_gateway": False,
                    "orders_submitted": 0,
                },
                sort_keys=True,
            )
        )
        return 0
    result = apply_bootstrap(
        db_path=args.db,
        manifest_path=args.manifest,
        confirmed_writer_stopped=(
            args.confirm_reported_trajectory_no_stopped
        ),
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
