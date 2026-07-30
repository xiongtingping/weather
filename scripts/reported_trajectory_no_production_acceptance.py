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
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reported_trajectory_country_sources import load_official_source_config

DB = ROOT / "data" / "archive.db"
CONFIG = ROOT / "config" / "trading" / "reported_trajectory_no_v1.json"
STATUS = ROOT / "data" / "reported_trajectory_no_status.json"
MARKET_WS_HEARTBEAT = ROOT / "data" / "market_ws_heartbeat.json"
MARKET_WS_MANIFEST = ROOT / "data" / "market_ws_latest_quotes.json"
HELD_YES_INVENTORY = ROOT / "data" / "reported_trajectory_held_yes_tokens.json"
MARKET_WS_QUOTE_SHARDS = ROOT / "data" / "market_ws_quote_shards"
CORE = {
    "com.weather.reported_trajectory_no",
    "com.weather.market_ws",
    "com.weather.clob_order_service",
}
LEGACY = (
    "com.weather.dynamic_three_leg_execution", "com.weather.trade_engine",
    "com.weather.auto_trade_entry_1m", "com.weather.auto_trade_worker",
    "com.weather.target_realization_worker", "com.weather.trade_sync_loop",
)


def canonical_sha(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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


def gateway_health() -> dict[str, Any]:
    with urllib.request.urlopen("http://127.0.0.1:8765/health", timeout=2.0) as response:
        return dict(json.loads(response.read().decode("utf-8")))


def main() -> int:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    activation = conn.execute("SELECT * FROM reported_trajectory_activation_state WHERE singleton=1").fetchone()
    registry = conn.execute(
        """SELECT MAX(refreshed_at) refreshed_at,
                  COUNT(DISTINCT CASE WHEN source_eligible=1 AND source_lane='amsc' THEN slug||'|'||event_date END) amsc,
                  COUNT(DISTINCT CASE WHEN source_eligible=1 AND source_lane='awc' THEN slug||'|'||event_date END) awc,
                  COUNT(DISTINCT CASE WHEN source_eligible=0 THEN slug||'|'||event_date END) blocked,
                  SUM(CASE WHEN source_lane='amsc' AND city_slug NOT IN ('beijing','chengdu','chongqing','guangzhou','qingdao','shanghai','shenzhen','taipei','wuhan') THEN 1 ELSE 0 END) bad_amsc,
                  SUM(CASE WHEN source_lane='awc' AND city_slug IN ('beijing','chengdu','chongqing','guangzhou','hong-kong','qingdao','shanghai','shenzhen','taipei','wuhan') THEN 1 ELSE 0 END) bad_awc
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
    unprotected_filled = int(
        conn.execute(
            """SELECT COUNT(*)
               FROM reported_trajectory_capital_ledger c
               LEFT JOIN reported_trajectory_exit_orders x
                 ON x.entry_order_intent_id=c.order_intent_id
                AND x.state IN ('prepared','submitting','exchange_accepted','reconcile_required','filled')
               WHERE c.state='filled' AND x.exit_intent_id IS NULL"""
        ).fetchone()[0]
    )
    failed_exits = int(
        conn.execute(
            "SELECT COUNT(*) FROM reported_trajectory_exit_orders WHERE state='submit_failed'"
        ).fetchone()[0]
    )
    latest_awc_poll = conn.execute(
        "SELECT MAX(response_completed_at) FROM reported_trajectory_source_polls WHERE source_lane='awc'"
    ).fetchone()[0]
    activation_updated_at = str(activation["updated_at"] or "") if activation else ""
    awc_enabled = config.get("production_source_allowlist") == ["amsc", "awc"]
    awc_poll_ok = bool(latest_awc_poll) if awc_enabled else (not latest_awc_poll or (
        bool(activation_updated_at) and str(latest_awc_poll) <= activation_updated_at
    ))
    status = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {}
    market_ws = json.loads(MARKET_WS_HEARTBEAT.read_text(encoding="utf-8")) if MARKET_WS_HEARTBEAT.exists() else {}
    market_manifest = json.loads(MARKET_WS_MANIFEST.read_text(encoding="utf-8")) if MARKET_WS_MANIFEST.exists() else {}
    held_yes_inventory = json.loads(HELD_YES_INVENTORY.read_text(encoding="utf-8")) if HELD_YES_INVENTORY.exists() else {}
    held_yes_tokens = {str(value) for value in held_yes_inventory.get("token_ids") or [] if str(value)}
    active_held_yes_tokens = {
        str(value) for value in held_yes_inventory.get("active_token_ids") or [] if str(value)
    }
    held_yes_quote_files_ready = all(
        (MARKET_WS_QUOTE_SHARDS / f"{hashlib.sha256(token.encode()).hexdigest()}.json").exists()
        for token in active_held_yes_tokens
    )
    health = gateway_health()
    ready_held_yes_tokens = {
        str(value) for value in health.get("conditional_balance_ready_token_ids") or [] if str(value)
    }
    weather_labels = loaded_weather_labels()
    source_registry = load_official_source_config()
    eligible_stations = {
        str(row[0])
        for row in conn.execute(
            """SELECT DISTINCT station_id FROM reported_trajectory_market_registry
               WHERE source_eligible=1 AND COALESCE(station_id,'')<>''"""
        )
    }
    source_routes = source_registry.get("station_routes") or {}
    source_providers = source_registry.get("providers") or {}
    origin_table_exists = bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reported_trajectory_observation_origins'"
        ).fetchone()
    )
    latest_awc_poll_json = json.loads(str(polls.get("awc", {}).get("poll_json") or "{}"))
    checks = {
        "activation_enabled": bool(activation and activation["state"] == "enabled"),
        "production_source_allowlist_valid": config.get("production_source_allowlist") in (["amsc"], ["amsc", "awc"]),
        "config_hash_exact": bool(activation and activation["config_sha256"] == canonical_sha(config)),
        "legacy_execution_disabled": not any(loaded(label) for label in LEGACY),
        "unique_owner_loaded": loaded("com.weather.reported_trajectory_no"),
        "market_ws_loaded": loaded("com.weather.market_ws"),
        "clob_gateway_single_domain_loaded": loaded("com.weather.clob_order_service"),
        "core_services_single_domain": all(len(service_domains(label)) == 1 for label in CORE),
        "only_core_weather_services_loaded": weather_labels == CORE,
        "ui_disabled": not loaded("com.xiong.weather.ui"),
        "monitor_disabled": not loaded("com.weather.monitor.daemon"),
        "unified_capital_pool": (
            config["order_policy"].get("unified_capital_pool") is True
            and config["order_policy"].get("deploy_all_available") is False
            and config["order_policy"].get("maximum_order_notional_usdc") == 50.0
            and "capital_share" not in config["source_policy"]["amsc"]
            and "capital_share" not in config["source_policy"]["awc"]
        ),
        "per_order_notional_cap_50_exact": config["order_policy"].get("maximum_order_notional_usdc") == 50.0,
        "fixed_bucket_target_50_exact": (
            config["order_policy"].get("maximum_order_notional_usdc") == 50.0
            and config["order_policy"].get("depth_participation_ratio") == 1.0
        ),
        "buy_collateral_balance_fetch_disabled": (
            config["order_policy"].get("collateral_balance_check") is False
            and health.get("collateral_balance_fetch_disabled") is True
        ),
        "no_position_priority_yes_sell_parallel": config.get("position_policy") == {
            "entry_priority": "NO",
            "existing_yes_positions": "liquidate_same_bucket_before_no_entry",
            "accepted_sell_terminal_before_subsequent_buy": True,
            "automatic_yes_liquidation": True,
            "yes_liquidation_order_type": "FAK",
            "partial_yes_liquidation_blocks_no": True,
            "concurrent_yes_sell_no_buy": False,
        },
        "exit_0_99_exact": config.get("exit_policy") == {
            "enabled": True, "side": "SELL", "limit_price": 0.99,
            "order_type": "GTC", "post_only": False,
        },
        "filled_positions_have_exit": unprotected_filled == 0,
        "no_failed_exit_orders": failed_exits == 0,
        "max_buy_price_exact": config["order_policy"]["maximum_buy_price"] == 0.99,
        "fak_no_only": config["order_policy"]["order_type"] == "FAK" and config["order_policy"]["outcome_side"] == "NO",
        "naked_market_order_disabled": config["order_policy"].get("allow_market") is False and config["order_policy"].get("naked_market_order_forbidden") is True,
        "taf_disabled": all(config["source_policy"][lane]["taf_requested"] is False for lane in ("amsc", "awc")),
        "awc_production_ingest_contract": config["source_policy"]["awc"].get("production_ingest") is awc_enabled,
        "awc_poll_fresh_or_suppressed": awc_poll_ok,
        "country_source_registry_complete": not (eligible_stations - set(source_routes)),
        "country_source_routes_valid": all(
            str(source_routes.get(station) or "") in source_providers for station in eligible_stations
        ),
        "country_origin_audit_schema": origin_table_exists,
        "awc_country_router_live": (
            latest_awc_poll_json.get("transport") == "country_official_router_with_explicit_awc_fallback"
        ) if awc_enabled else True,
        "awc_release_latency_policy_exact": config.get("poll_policy", {}).get("awc_relay_release_delay_sec") == 30
        and config.get("poll_policy", {}).get("awc_release_burst_retry_sec") == 0.25
        and config.get("poll_policy", {}).get("awc_release_burst_window_sec") == 90
        and config.get("poll_policy", {}).get("awc_background_poll_sec") == 2,
        "awc_provider_incremental_commit_live": (
            "provider_completed_after_router_start_ms" in latest_awc_poll_json
        ) if awc_enabled else True,
        "source_lanes_disjoint": int(registry["bad_amsc"] or 0) == 0 and int(registry["bad_awc"] or 0) == 0,
        "amsc_registry_nonempty": int(registry["amsc"] or 0) > 0,
        "awc_registry_nonempty_when_enabled": (int(registry["awc"] or 0) > 0) if awc_enabled else (int(registry["awc"] or 0) == 0),
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
        "held_yes_inventory_fresh": bool(
            held_yes_inventory.get("observed_at")
            and held_yes_inventory.get("complete") is True
            and isinstance(held_yes_inventory.get("active_token_ids"), list)
            and active_held_yes_tokens <= held_yes_tokens
            and age(str(held_yes_inventory["observed_at"])) <= 15
        ),
        "unheld_yes_fast_attestation_policy_exact": (
            config.get("prewarm_policy", {}).get("held_yes_inventory_refresh_sec") == 5.0
            and config.get("prewarm_policy", {}).get("unheld_yes_inventory_max_age_sec") == 10.0
        ),
        "active_held_yes_quotes_materialized": held_yes_quote_files_ready,
        "active_held_yes_balances_ready": (
            health.get("active_held_yes_balance_ready") is True
            and active_held_yes_tokens <= ready_held_yes_tokens
        ),
        "registry_watchdog_healthy": (
            status.get("registry_watchdog", {}).get("healthy") is True
        ),
        "clob_gateway_ready": health.get("ready") is True and health.get("read_only") is not True,
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
        "schema": "reported_trajectory_no_production_acceptance_v1",
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
            "latest_awc_provider_states": latest_awc_poll_json.get("provider_states") or [],
            "latest_awc_fallback_reasons": latest_awc_poll_json.get("fallback_reasons") or {},
        },
        "latest_polls": latest_poll_summary,
        "activation": dict(activation) if activation else None,
        "core_services": sorted(weather_labels),
        "market_ws": market_ws,
        "clob_gateway": {
            key: health.get(key)
            for key in (
                "pid", "ready", "ws_snapshot_ready", "ws_partial_snapshot_ready", "ws_snapshot_age_ms",
                "ws_active_shards", "ws_expected_shards", "ws_materialized_token_count",
                "ws_subscribed_token_count", "collateral_cache_ready",
                "collateral_balance_fetch_disabled",
                "all_shards_prefetched_evidence_ready",
                "local_prefetched_evidence_ready", "collateral_cache_age_ms",
                "inflight_count", "idempotency_unresolved_count",
            )
        },
        "exit_orders": {
            "unprotected_filled_positions": unprotected_filled,
            "failed_exit_orders": failed_exits,
        },
    }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
