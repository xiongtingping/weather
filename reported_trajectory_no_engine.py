"""Durable temperature-only trajectory-NO decision and execution engine."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from clob_order_service_client import ClobOrderServiceClient
from reported_trajectory_no_runtime import (
    conservative_market_temperature,
    identity,
    parse_metar_temperature,
    select_nearest_impossible_lower_bucket,
)
from reported_trajectory_sources import infer_ddhhmm_time, utc_now


SCHEMA_VERSION = "reported_trajectory_no_engine_v1"
HELD_YES_INVENTORY = Path(__file__).resolve().parent / "data" / "reported_trajectory_held_yes_tokens.json"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _parse_utc(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp_timezone_missing")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _nonnegative_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _response_value(response: Mapping[str, Any], key: str) -> Any:
    if response.get(key) is not None:
        return response.get(key)
    for nested_key in ("response", "exchange_response", "order"):
        nested = response.get(nested_key)
        if isinstance(nested, Mapping) and nested.get(key) is not None:
            return nested.get(key)
    return None


def _available_usdc(balance: Mapping[str, Any], *, decimals: int = 6) -> float | None:
    def value(raw: Any) -> float | None:
        text = str(raw or "").strip()
        return int(text) / (10**decimals) if text.isdigit() else None

    cash = value(balance.get("balance"))
    allowance = value(balance.get("allowance"))
    if allowance is None and isinstance(balance.get("allowances"), Mapping):
        candidates = [value(item) for item in balance["allowances"].values()]
        candidates = [item for item in candidates if item is not None]
        allowance = max(candidates) if candidates else None
    return min(cash, allowance) if cash is not None and allowance is not None else None


def _book_asks(snapshot: Mapping[str, Any]) -> list[tuple[float, float]]:
    book = snapshot.get("order_book") if isinstance(snapshot.get("order_book"), Mapping) else {}
    out: list[tuple[float, float]] = []
    for raw in book.get("asks") or []:
        if not isinstance(raw, Mapping):
            continue
        try:
            price, size = float(raw.get("price")), float(raw.get("size"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(price) and math.isfinite(size) and 0 < price < 1 and size > 0:
            out.append((price, size))
    return sorted(out)


def _book_bids(snapshot: Mapping[str, Any]) -> list[tuple[float, float]]:
    book = snapshot.get("order_book") if isinstance(snapshot.get("order_book"), Mapping) else {}
    out: list[tuple[float, float]] = []
    for raw in book.get("bids") or []:
        if not isinstance(raw, Mapping):
            continue
        price = _nonnegative_float(raw.get("price"))
        size = _nonnegative_float(raw.get("size"))
        if price is not None and size is not None and 0 < price < 1 and size > 0:
            out.append((price, size))
    return sorted(out, reverse=True)


def _activation(conn: sqlite3.Connection, *, config_sha256: str) -> tuple[bool, str, int]:
    row = conn.execute(
        """SELECT generation,state,activation_sha256,config_sha256,
                  old_bayes_execution_disabled,awc_amsc_disjoint
           FROM reported_trajectory_activation_state WHERE singleton=1"""
    ).fetchone()
    if row is None:
        return False, "reported_trajectory_activation_missing", 0
    if str(row[1]) != "enabled":
        return False, "reported_trajectory_activation_not_enabled", int(row[0] or 0)
    if str(row[3]) != config_sha256:
        return False, "reported_trajectory_activation_config_drift", int(row[0] or 0)
    if int(row[4] or 0) != 1:
        return False, "legacy_bayes_execution_not_disabled", int(row[0] or 0)
    if int(row[5] or 0) != 1:
        return False, "source_lane_disjoint_guard_missing", int(row[0] or 0)
    return True, "", int(row[0])


def _production_no_live(config: Mapping[str, Any]) -> bool:
    """Return the enforced execution boundary for the target system resident."""

    # The target system plist supplies the environment value so this boundary
    # is separate from any other live line.  An explicit no-live environment
    # value always wins over config, making a stale config unable to re-enable
    # submits.  Direct tests/legacy tools retain their existing behavior unless
    # they opt into this boundary explicitly.
    env_mode = str(os.environ.get("PM_TARGET_EXECUTION_MODE") or "").strip().lower()
    configured_mode = str(config.get("execution_mode") or "").strip().lower()
    mode = env_mode or configured_mode
    if mode in {"production_no_live", "no_live", "disabled"}:
        return True
    return str(os.environ.get("PM_TARGET_NO_LIVE") or "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _write_decision(
    conn: sqlite3.Connection,
    *,
    decision_id: str,
    slug: str,
    event_date: str,
    outcome: str,
    will_order: bool,
    reason_codes: Sequence[str],
    details: Mapping[str, Any],
    created_at: str,
    order_intent_id: str = "",
    action: str = "entry",
) -> None:
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(auto_trade_decisions)")
    }
    reason_codes_json = _json(list(reason_codes))
    details_payload = dict(details)
    details_payload.setdefault("decision_id", decision_id)
    details_payload.setdefault("order_intent_id", order_intent_id)
    details_json = _json(details_payload)
    if "run_id" in columns:
        # Compatibility for isolated legacy tests/tools.  The production
        # cutover uses the target-owned SSOT schema below.
        conn.execute(
            """INSERT INTO auto_trade_decisions(
                 run_id,run_date,slug,event_date,title,outcome,side,action,decided_state,will_order,
                 reason_codes_json,reasons_json,details_json,created_at,correlation_id,fact_snapshot_id,
                 posterior_run_id,decision_id,signal_id,order_intent_id
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                0, event_date, slug, event_date, "Reported trajectory NO", outcome, "NO", action,
                "will_order" if will_order else "blocked", int(will_order), reason_codes_json,
                reason_codes_json, details_json, created_at,
                identity("rtcorr_", (slug, event_date, details.get("observation_id"))),
                str(details.get("observation_id") or ""), None, decision_id,
                identity("rtsig_", (slug, event_date, outcome, details.get("hard_lower_bound_market_unit"))),
                order_intent_id or None,
            ),
        )
        return

    required = {
        "decision_id", "cycle_id", "plan_id", "station_id", "event_date",
        "settlement_timezone", "strategy_lane",
        "decided_state", "will_order", "reason_codes_json", "details_json", "decision_sha256",
        "created_at",
    }
    if not required.issubset(columns):
        missing = ",".join(sorted(required - columns))
        raise sqlite3.OperationalError(f"auto_trade_decisions_schema_incompatible:{missing}")
    station_id = str(details.get("station_id") or "").strip()
    if not station_id:
        raise ValueError("reported_trajectory_decision_station_id_missing")
    settlement_timezone = str(details.get("settlement_timezone") or "").strip()
    if not settlement_timezone:
        raise ValueError("reported_trajectory_decision_settlement_timezone_missing")
    cycle_id = identity(
        "rtcycle_", (decision_id, slug, event_date, station_id, action, order_intent_id)
    )
    plan_id = identity(
        "rtplan_", (decision_id, slug, event_date, station_id, action, tuple(reason_codes))
    )
    decision_state = "prepared" if will_order else "blocked"
    decision_sha256 = _sha(
        {
            "schema": "reported_trajectory_no_decision_v1",
            "decision_id": decision_id,
            "cycle_id": cycle_id,
            "plan_id": plan_id,
            "station_id": station_id,
            "event_date": event_date,
            "settlement_timezone": settlement_timezone,
            "strategy_lane": "hard_no",
            "decided_state": decision_state,
            "will_order": int(will_order),
            "reason_codes_json": reason_codes_json,
            "details_json": details_json,
            "created_at": created_at,
        }
    )
    conn.execute(
        """INSERT INTO auto_trade_decisions(
             decision_id,cycle_id,plan_id,station_id,event_date,settlement_timezone,
             strategy_lane,decided_state,will_order,reason_codes_json,details_json,
             decision_sha256,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            decision_id, cycle_id, plan_id, station_id, event_date,
            settlement_timezone, "hard_no",
            decision_state, int(will_order), reason_codes_json, details_json,
            decision_sha256, created_at,
        ),
    )


class ReportedTrajectoryNoEngine:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        config: Mapping[str, Any],
        gateway: ClobOrderServiceClient | None = None,
        held_yes_inventory_tokens: set[str] | None = None,
    ) -> None:
        self.conn = conn
        self.config = dict(config)
        self.config_sha256 = _sha(self.config)
        self.production_no_live = _production_no_live(self.config)
        self.gateway = gateway or ClobOrderServiceClient(timeout_seconds=1.0)
        self._held_yes_inventory_tokens_override = (
            set()
            if held_yes_inventory_tokens is None
            and gateway is not None
            and not isinstance(gateway, ClobOrderServiceClient)
            else held_yes_inventory_tokens
        )
        self._last_poll_persist_monotonic: dict[str, float] = {}
        self._known_observation_ids: set[str] = set()

    def _fresh_held_yes_tokens(self) -> tuple[set[str] | None, str, str]:
        if self._held_yes_inventory_tokens_override is not None:
            return set(self._held_yes_inventory_tokens_override), "unit_test_inventory_override", utc_now()
        try:
            payload = json.loads(HELD_YES_INVENTORY.read_text(encoding="utf-8"))
            observed_at = _parse_utc(payload.get("observed_at"))
        except (OSError, ValueError, TypeError):
            return None, "yes_position_inventory_unavailable_or_stale", ""
        policy = self.config.get("prewarm_policy") if isinstance(
            self.config.get("prewarm_policy"), Mapping
        ) else {}
        max_age = float(policy.get("unheld_yes_inventory_max_age_sec", 10.0))
        age = (datetime.now(timezone.utc) - observed_at).total_seconds()
        if (
            payload.get("schema") != "reported_trajectory_held_yes_inventory_v1"
            or payload.get("complete") is not True
            or age < 0
            or age > max_age
            or not isinstance(payload.get("token_ids"), list)
        ):
            return None, "yes_position_inventory_unavailable_or_stale", _iso(observed_at)
        return (
            {str(value) for value in payload["token_ids"] if str(value)},
            str(payload.get("source") or "official_polymarket_data_api_positions"),
            _iso(observed_at),
        )

    def _market_groups(self, station_id: str, valid_at: datetime, source_lane: str) -> list[list[dict[str, Any]]]:
        rows = self.conn.execute(
            """SELECT registry_json FROM reported_trajectory_market_registry
               WHERE station_id=? AND source_lane=? AND source_eligible=1
                 AND market_active=1 AND market_accepting_orders=1
                 AND julianday(refreshed_at)>=julianday('now','-10 minutes')""",
            (station_id, source_lane),
        ).fetchall()
        by_market: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            item = json.loads(str(row[0]))
            timezone_name = str(item.get("settlement_timezone") or "")
            try:
                local_date = valid_at.astimezone(ZoneInfo(timezone_name)).date().isoformat()
            except Exception:
                continue
            if local_date != str(item.get("event_date") or ""):
                continue
            by_market.setdefault((str(item["slug"]), local_date), []).append(item)
        complete: list[list[dict[str, Any]]] = []
        for _key, value in sorted(by_market.items()):
            kinds = [str(item.get("kind") or "") for item in value]
            # A partially closed event can leave only one still-accepting
            # binary market. It is not a complete temperature ladder and must
            # never crash or authorize nearest-lower-bucket selection.
            if kinds.count("lower_tail") != 1 or kinds.count("upper_tail") != 1:
                continue
            if any(not str(item.get("yes_token") or "") or not str(item.get("no_token") or "") for item in value):
                continue
            complete.append(value)
        return complete

    def _insert_observation(
        self,
        *,
        source_lane: str,
        source_poll_id: str,
        raw: Mapping[str, Any],
        fetch_started_at: str,
        fetch_completed_at: str,
    ) -> tuple[dict[str, Any] | None, bool]:
        observation, insert_values = self._preview_observation(
            source_lane=source_lane,
            source_poll_id=source_poll_id,
            raw=raw,
            fetch_started_at=fetch_started_at,
            fetch_completed_at=fetch_completed_at,
        )
        cursor = self.conn.execute(
            """INSERT OR IGNORE INTO reported_trajectory_observations(
                 observation_id,source_lane,station_id,report_type,valid_at,reported_temperature_c,
                 raw_sha256,raw_text,first_seen_at,parsed_at,canonical_committed_at,
                 fetch_started_at,fetch_completed_at,source_poll_id,correction_suspected
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            insert_values,
        )
        if cursor.rowcount != 1:
            return None, False
        if source_lane == "amsc":
            default_origin = {
                "origin_source_id": "cn_amsc",
                "origin_country_code": "CN",
                "origin_authority": "中国民用航空气象服务网（AMSC）",
                "origin_url": "https://www.amsc.net.cn/",
                "origin_transport": "official_amsc_batch_cccc",
                "origin_direct_country_official": True,
                "origin_fallback_used": False,
                "origin_fallback_reason": "",
            }
        else:
            default_origin = {
                "origin_source_id": "global_noaa_awc_relay",
                "origin_country_code": "ZZ",
                "origin_authority": "NOAA/NWS Aviation Weather Center worldwide relay",
                "origin_url": "https://aviationweather.gov/data/api/",
                "origin_transport": "official_awc_raw_metar_batch_direct",
                "origin_direct_country_official": False,
                "origin_fallback_used": True,
                "origin_fallback_reason": "legacy_awc_poll_without_country_origin_metadata",
            }
        origin = {**default_origin, **{key: value for key, value in raw.items() if str(key).startswith(("origin_", "preferred_origin_", "official_source_registry_"))}}
        self.conn.execute(
            """INSERT INTO reported_trajectory_observation_origins(
                 observation_id,source_lane,station_id,origin_source_id,origin_country_code,
                 origin_authority,origin_url,origin_transport,direct_country_official,
                 fallback_used,fallback_reason,preferred_origin_source_id,
                 preferred_origin_authority,official_source_registry_sha256,origin_json,committed_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                observation["observation_id"], source_lane, observation["station_id"],
                str(origin.get("origin_source_id") or ""), str(origin.get("origin_country_code") or ""),
                str(origin.get("origin_authority") or ""), str(origin.get("origin_url") or ""),
                str(origin.get("origin_transport") or ""), int(bool(origin.get("origin_direct_country_official"))),
                int(bool(origin.get("origin_fallback_used"))), str(origin.get("origin_fallback_reason") or ""),
                str(origin.get("preferred_origin_source_id") or ""),
                str(origin.get("preferred_origin_authority") or ""),
                str(origin.get("official_source_registry_sha256") or ""), _json(origin), utc_now(),
            ),
        )
        return observation, True

    def _preview_observation(
        self,
        *,
        source_lane: str,
        source_poll_id: str,
        raw: Mapping[str, Any],
        fetch_started_at: str,
        fetch_completed_at: str,
    ) -> tuple[dict[str, Any], tuple[Any, ...]]:
        """Parse an immutable observation without writing it.

        This preview is used to freeze the local CLOB quote before acquiring
        SQLite's write lock.  The exact same values are subsequently inserted
        in the single canonical transaction.
        """

        parsed = parse_metar_temperature(
            str(raw.get("raw_text") or ""), expected_station=str(raw.get("station_id") or "")
        )
        if raw.get("source_report_time"):
            valid_at = _parse_utc(raw["source_report_time"])
        else:
            valid_at = infer_ddhhmm_time(
                day=parsed.day,
                hour=parsed.hour,
                minute=parsed.minute,
                reference=_parse_utc(fetch_completed_at),
            )
        # Cross-check the AWC reportTime against the immutable DDHHMMZ header.
        if (valid_at.day, valid_at.hour, valid_at.minute) != (parsed.day, parsed.hour, parsed.minute):
            raise ValueError("source_report_time_header_mismatch")
        observation_id = identity(
            "rtobs_", (source_lane, parsed.station_id, _iso(valid_at), parsed.report_type, parsed.raw_sha256)
        )
        parsed_at = utc_now()
        correction = " COR " in f" {parsed.raw_text} " or " AMD " in f" {parsed.raw_text} "
        observation = {
            "observation_id": observation_id,
            "source_lane": source_lane,
            "station_id": parsed.station_id,
            "report_type": parsed.report_type,
            "valid_at": valid_at,
            "reported_temperature_c": parsed.reported_temperature_c,
            "correction_suspected": correction,
            "fetch_completed_at": fetch_completed_at,
        }
        insert_values = (
            observation_id, source_lane, parsed.station_id, parsed.report_type, _iso(valid_at),
            parsed.reported_temperature_c, parsed.raw_sha256, parsed.raw_text, fetch_completed_at,
            parsed_at, utc_now(), fetch_started_at, fetch_completed_at, source_poll_id, int(correction),
        )
        return observation, insert_values

    def _freeze_quote_snapshot(self, *, token_id: str, max_price: float) -> tuple[dict[str, Any] | None, str]:
        """Read the resident gateway outside every SQLite write transaction."""

        # Size=minimum_shares is only a read-only admission probe; exact full
        # sizing is frozen from the returned depth and balance below.
        order_policy = self.config["order_policy"]
        minimum_shares = float(order_policy["minimum_shares"])
        request = {
            "token_id": token_id,
            "side": "BUY",
            "limit_price": max_price,
            "size": minimum_shares,
            "notional": minimum_shares * max_price,
        }
        snapshot: Mapping[str, Any] = {}
        for attempt in range(2):
            try:
                snapshot = self.gateway.freeze_execution_guard_snapshot(request)
            except Exception:
                snapshot = {}
            if snapshot.get("ok") is True:
                return dict(snapshot), ""
            if attempt == 0:
                # One bounded local re-read covers an atomic shard replacement
                # or balance-prefetch handoff without introducing REST fallback,
                # stale evidence, an outbox, or an exchange POST retry.
                time.sleep(0.05)
        errors = [str(value) for value in snapshot.get("evidence_errors") or []]
        if any("collateral" in value for value in errors):
            return None, "clob_collateral_prefetch_missing_or_stale"
        if any(
            marker in value
            for value in errors
            for marker in ("clob_ws", "order_book", "quote_shards", "ENOENT")
        ):
            return None, "clob_ws_target_book_not_materialized"
        return None, "clob_execution_evidence_unavailable"

    def _freeze_quote_snapshots(
        self,
        requests: Mapping[tuple[str, str], str],
        *,
        max_price: float,
    ) -> dict[tuple[str, str], tuple[dict[str, Any] | None, str]]:
        """Read independent resident token snapshots concurrently, outside SQLite locks."""

        if len(requests) <= 1:
            return {
                key: self._freeze_quote_snapshot(token_id=token_id, max_price=max_price)
                for key, token_id in requests.items()
            }
        frozen: dict[tuple[str, str], tuple[dict[str, Any] | None, str]] = {}
        workers = min(8, len(requests))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="clob-snapshot") as executor:
            futures = {
                executor.submit(self._freeze_quote_snapshot, token_id=token_id, max_price=max_price): key
                for key, token_id in requests.items()
            }
            for future in as_completed(futures):
                key = futures[future]
                try:
                    frozen[key] = future.result()
                except Exception:
                    frozen[key] = (None, "clob_execution_evidence_unavailable")
        return frozen

    def prewarm_next_candidates(self, *, source_lane: str) -> dict[str, Any]:
        """Warm read-only evidence for the next plausible hard-bound advances.

        This never creates an observation, decision, reservation, or order. It
        only exercises the same local gateway reads that a later immutable
        METAR advance will consume. +1C covers the normal integer step; +2C
        keeps a direct jump off the event-time cold path.
        """

        started = time.monotonic()
        policy = self.config.get("prewarm_policy") if isinstance(
            self.config.get("prewarm_policy"), Mapping
        ) else {}
        increments = tuple(
            sorted({int(value) for value in policy.get("candidate_increments_c", [1, 2]) if int(value) > 0})
        ) or (1, 2)
        rows = self.conn.execute(
            """SELECT station_id,event_date,reported_max_c,max_valid_at
               FROM reported_trajectory_station_day_bounds
               WHERE source_lane=? ORDER BY station_id,event_date""",
            (source_lane,),
        ).fetchall()
        try:
            inventory = json.loads(HELD_YES_INVENTORY.read_text(encoding="utf-8"))
            held_yes_tokens = {
                str(value) for value in inventory.get("token_ids") or [] if str(value)
            }
        except (OSError, ValueError, TypeError):
            held_yes_tokens = set()
        candidates: dict[tuple[str, str, int, str], dict[str, Any]] = {}
        current_valid_at = datetime.now(timezone.utc)
        for station_id, event_date, reported_max_c, _max_valid_at in rows:
            # Prewarm the market that a report arriving now can actually map
            # to. Never keep yesterday's still-open settlement book hot after
            # the station timezone has crossed into a new market day.
            for group in self._market_groups(str(station_id), current_valid_at, source_lane):
                market = group[0]
                if str(market.get("event_date") or "") != str(event_date):
                    continue
                for increment in increments:
                    hard_bound = conservative_market_temperature(
                        int(reported_max_c) + increment, str(market["settlement_unit"])
                    )
                    target = select_nearest_impossible_lower_bucket(group, hard_lower_bound=hard_bound)
                    if target is None:
                        continue
                    key = (str(station_id), str(market["slug"]), increment, str(target["outcome"]))
                    candidates[key] = {
                        "station_id": str(station_id),
                        "event_date": str(event_date),
                        "slug": str(market["slug"]),
                        "increment_c": increment,
                        "target_outcome": str(target["outcome"]),
                        "no_token": str(target.get("no_token") or ""),
                        "yes_token": str(target.get("yes_token") or ""),
                        "held_yes_expected": str(target.get("yes_token") or "") in held_yes_tokens,
                    }

        max_price = float(self.config["order_policy"]["maximum_buy_price"])
        results: dict[tuple[str, str, int, str], dict[str, Any]] = {}

        def warm(item: dict[str, Any]) -> tuple[bool, str, bool, str]:
            no_snapshot, no_error = self._freeze_quote_snapshot(
                token_id=item["no_token"], max_price=max_price
            )
            yes_snapshot, yes_error = self._freeze_yes_position_snapshot(token_id=item["yes_token"])
            return no_snapshot is not None, no_error, yes_snapshot is not None, yes_error

        if candidates:
            # Bound fan-out so background authenticated position probes cannot
            # starve the gateway's latency-critical order POST transport.
            workers = min(8, len(candidates))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="candidate-prewarm") as executor:
                futures = {executor.submit(warm, item): key for key, item in candidates.items()}
                for future in as_completed(futures):
                    key = futures[future]
                    try:
                        no_ready, no_error, yes_ready, yes_error = future.result()
                    except Exception as exc:
                        no_ready, no_error, yes_ready, yes_error = False, type(exc).__name__, False, type(exc).__name__
                    results[key] = {
                        "no_ready": no_ready,
                        "no_error": no_error,
                        "yes_position_ready": yes_ready,
                        "yes_error": yes_error,
                    }
        failures = [
            {**candidates[key], **result}
            for key, result in results.items()
            if not result["no_ready"]
            or (candidates[key]["held_yes_expected"] and not result["yes_position_ready"])
        ]
        return {
            "schema": "reported_trajectory_candidate_prewarm_v1",
            "ok": not failures,
            "source_lane": source_lane,
            "prewarmed_at": utc_now(),
            "candidate_count": len(candidates),
            "no_ready_count": sum(int(value["no_ready"]) for value in results.values()),
            "yes_position_ready_count": sum(int(value["yes_position_ready"]) for value in results.values()),
            "held_yes_candidate_count": sum(
                int(value["held_yes_expected"]) for value in candidates.values()
            ),
            "held_yes_ready_count": sum(
                int(candidates[key]["held_yes_expected"] and result["yes_position_ready"])
                for key, result in results.items()
            ),
            "unheld_yes_probe_pending_count": sum(
                int(not candidates[key]["held_yes_expected"] and not result["yes_position_ready"])
                for key, result in results.items()
            ),
            "failure_count": len(failures),
            "failures": failures[:20],
            "duration_ms": round((time.monotonic() - started) * 1000.0, 3),
            "writes_or_orders": False,
        }

    def _capital_from_snapshot(
        self,
        *,
        source_lane: str,
        slug: str,
        snapshot: Mapping[str, Any],
        max_price: float,
    ) -> tuple[dict[str, Any] | None, str]:
        """Atomically size against a pre-frozen quote while the ledger is locked."""

        order_policy = self.config["order_policy"]
        minimum_shares = float(order_policy["minimum_shares"])
        metadata = snapshot.get("token_metadata") if isinstance(snapshot.get("token_metadata"), Mapping) else {}
        try:
            tick_size = float(metadata.get("tick_size") or 0.01)
        except (TypeError, ValueError):
            tick_size = 0.01
        if tick_size not in {0.1, 0.01, 0.001, 0.0001}:
            return None, "clob_tick_size_unsupported"
        effective_limit_price = round(math.floor((max_price + 1e-12) / tick_size) * tick_size, 4)
        asks = [
            (price, size) for price, size in _book_asks(snapshot)
            if price <= effective_limit_price + 1e-12
        ]
        if not asks:
            return None, "no_executable_no_ask_at_or_below_max_buy_price"
        executable_depth_usdc = sum(price * size for price, size in asks)
        target_notional = float(order_policy["maximum_order_notional_usdc"])
        blocker = (
            "capital_below_minimum_marketable_order"
            if target_notional + 1e-9 < minimum_shares * effective_limit_price else ""
        )
        capital = {
            "schema": "reported_trajectory_no_fixed_notional_without_balance_probe_v1",
            "executable_depth_usdc": round(executable_depth_usdc, 6),
            "maximum_buy_price": effective_limit_price,
            "minimum_shares": minimum_shares,
            "maximum_order_notional_usdc": target_notional,
            "target_order_notional_usdc": target_notional,
            "frozen_order_notional_usdc": 0.0 if blocker else round(target_notional, 6),
            "capital_block_reason": blocker,
            "collateral_balance_check": False,
            "collateral_balance_assumption": "operator_attested_sufficient_exchange_final_validation",
            "visible_depth_caps_requested_notional": False,
            "fak_partial_fill_allowed": True,
            "existing_yes_positions_sizing_impact": False,
            "unified_pool": True,
            "deploy_all_available": False,
            "source_quota_applied": False,
            "market_quota_applied": False,
        }
        capital["configured_maximum_buy_price"] = max_price
        capital["effective_tick_aligned_limit_price"] = effective_limit_price
        capital["tick_size"] = tick_size
        if capital["capital_block_reason"]:
            return capital, str(capital["capital_block_reason"])
        capital["best_ask"] = asks[0][0]
        capital["executable_ask_levels"] = len(asks)
        capital["source_lane"] = source_lane
        return capital, ""

    def _advance_market_bound_and_decide(
        self,
        observation: Mapping[str, Any],
        market_rows: list[dict[str, Any]],
        *,
        frozen_snapshot: Mapping[str, Any] | None = None,
        frozen_quote_error: str = "",
    ) -> dict[str, Any]:
        market = market_rows[0]
        source_lane = str(observation["source_lane"])
        station = str(observation["station_id"])
        event_date = str(market["event_date"])
        slug = str(market["slug"])
        outcome_unit = str(market["settlement_unit"])
        now = utc_now()
        if observation["correction_suspected"]:
            self.conn.execute(
                """INSERT INTO reported_trajectory_station_halts(
                     source_lane,station_id,event_date,state,reason_code,
                     triggering_observation_id,created_at,cleared_at
                   ) VALUES(?,?,?,'active','metar_correction_requires_manual_review',?,?,NULL)
                   ON CONFLICT(source_lane,station_id,event_date) DO UPDATE SET
                     state='active',reason_code=excluded.reason_code,
                     triggering_observation_id=excluded.triggering_observation_id,
                     created_at=excluded.created_at,cleared_at=NULL""",
                (source_lane, station, event_date, observation["observation_id"], now),
            )
            correction_bound = conservative_market_temperature(
                int(observation["reported_temperature_c"]), outcome_unit
            )
            correction_target = select_nearest_impossible_lower_bucket(
                market_rows, hard_lower_bound=correction_bound
            )
            correction_outcome = str(correction_target["outcome"]) if correction_target else ""
            correction_decision_id = identity(
                "rtdecision_",
                (source_lane, station, slug, event_date, observation["observation_id"], "correction_block"),
            )
            _write_decision(
                self.conn,
                decision_id=correction_decision_id,
                slug=slug,
                event_date=event_date,
                outcome=correction_outcome,
                will_order=False,
                reason_codes=["metar_correction_requires_manual_review"],
                details={
                    "schema": SCHEMA_VERSION,
                    "strategy_id": "reported_trajectory_no_v1",
                    "source_lane": source_lane,
                    "station_id": station,
                    "observation_id": observation["observation_id"],
                    "reported_temperature_c": observation["reported_temperature_c"],
                    "hard_lower_bound_market_unit": correction_bound,
                    "correction_did_not_advance_bound": True,
                    "bayes_consumed": False,
                    "taf_consumed": False,
                    "yes_order_allowed": False,
                },
                created_at=now,
            )
            return {
                "state": "blocked",
                "reason": "metar_correction_requires_manual_review",
                "decision_id": correction_decision_id,
            }
        halt = self.conn.execute(
            """SELECT reason_code FROM reported_trajectory_station_halts
               WHERE source_lane=? AND station_id=? AND event_date=? AND state='active'""",
            (source_lane, station, event_date),
        ).fetchone()
        if halt is not None:
            halted_bound = conservative_market_temperature(
                int(observation["reported_temperature_c"]), outcome_unit
            )
            halted_target = select_nearest_impossible_lower_bucket(
                market_rows, hard_lower_bound=halted_bound
            )
            halted_decision_id = identity(
                "rtdecision_",
                (source_lane, station, slug, event_date, observation["observation_id"], "station_halted"),
            )
            _write_decision(
                self.conn,
                decision_id=halted_decision_id,
                slug=slug,
                event_date=event_date,
                outcome=str(halted_target["outcome"]) if halted_target else "",
                will_order=False,
                reason_codes=["station_day_halted_after_correction"],
                details={
                    "schema": SCHEMA_VERSION,
                    "source_lane": source_lane,
                    "station_id": station,
                    "observation_id": observation["observation_id"],
                    "halt_reason": str(halt[0]),
                    "bayes_consumed": False,
                    "taf_consumed": False,
                    "yes_order_allowed": False,
                },
                created_at=now,
            )
            return {
                "state": "blocked",
                "reason": "station_day_halted_after_correction",
                "decision_id": halted_decision_id,
            }
        existing = self.conn.execute(
            """SELECT reported_max_c,bound_generation FROM reported_trajectory_station_day_bounds
               WHERE source_lane=? AND station_id=? AND event_date=?""",
            (source_lane, station, event_date),
        ).fetchone()
        previous_max = int(existing[0]) if existing else None
        temperature_c = int(observation["reported_temperature_c"])
        if previous_max is not None and temperature_c <= previous_max:
            return {"state": "no_bound_advance", "slug": slug, "previous_max_c": previous_max}
        generation = int(existing[1] or 0) + 1 if existing else 1
        self.conn.execute(
            """INSERT INTO reported_trajectory_station_day_bounds(
                 source_lane,station_id,event_date,settlement_timezone,reported_max_c,
                 max_observation_id,max_valid_at,bound_generation,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_lane,station_id,event_date) DO UPDATE SET
                 reported_max_c=excluded.reported_max_c,max_observation_id=excluded.max_observation_id,
                 max_valid_at=excluded.max_valid_at,bound_generation=excluded.bound_generation,
                 updated_at=excluded.updated_at""",
            (
                source_lane, station, event_date, market["settlement_timezone"], temperature_c,
                observation["observation_id"], _iso(observation["valid_at"]), generation, now,
            ),
        )
        hard_bound = conservative_market_temperature(temperature_c, outcome_unit)
        target = select_nearest_impossible_lower_bucket(market_rows, hard_lower_bound=hard_bound)
        if target is None:
            return {"state": "bound_advanced_without_lower_bucket", "slug": slug, "hard_lower_bound": hard_bound}
        outcome = str(target["outcome"])
        no_token = str(target.get("no_token") or "")
        yes_token = str(target.get("yes_token") or "")
        idempotency_key = identity(
            "rtidem_", (source_lane, station, slug, event_date, hard_bound, outcome, generation)
        )
        order_intent_id = identity("rtintent_", idempotency_key)
        decision_id = identity("rtdecision_", idempotency_key)
        common_details = {
            "schema": SCHEMA_VERSION,
            "strategy_id": "reported_trajectory_no_v1",
            "source_lane": source_lane,
            "station_id": station,
            "settlement_timezone": market["settlement_timezone"],
            "observation_id": observation["observation_id"],
            "report_type": observation["report_type"],
            "reported_max_c": temperature_c,
            "hard_lower_bound_market_unit": hard_bound,
            "settlement_unit": outcome_unit,
            "target_bucket_upper": target["upper"],
            "selection_rule": target["selection_rule"],
            "bayes_consumed": False,
            "taf_consumed": False,
            "physical_fields_consumed": False,
            "yes_order_allowed": False,
            "yes_token": yes_token,
            "existing_yes_position_policy": "liquidate_same_bucket_before_no_entry",
            "unified_capital_pool": True,
            "source_capital_quota_applied": False,
        }
        if self.production_no_live:
            details = {
                **common_details,
                "execution_mode": "production_no_live",
                "live_submit_authorized": False,
                "places_orders": False,
                "calls_order_gateway": False,
            }
            _write_decision(
                self.conn,
                decision_id=decision_id,
                slug=slug,
                event_date=event_date,
                outcome=outcome,
                will_order=False,
                reason_codes=["production_no_live"],
                details=details,
                created_at=now,
            )
            return {"state": "blocked", "reason": "production_no_live", "decision_id": decision_id}
        enabled, activation_block, activation_generation = _activation(self.conn, config_sha256=self.config_sha256)
        max_price = float(self.config["order_policy"]["maximum_buy_price"])
        if frozen_snapshot is None:
            capital, quote_block = None, frozen_quote_error or "clob_execution_evidence_unavailable"
        else:
            capital, quote_block = self._capital_from_snapshot(
                source_lane=source_lane,
                slug=slug,
                snapshot=frozen_snapshot,
                max_price=max_price,
            )
        blocker = activation_block or quote_block
        if blocker:
            details = {**common_details, "activation_generation": activation_generation, "capital": capital or {}}
            _write_decision(
                self.conn, decision_id=decision_id, slug=slug, event_date=event_date, outcome=outcome,
                will_order=False, reason_codes=[blocker], details=details, created_at=now,
            )
            return {"state": "blocked", "reason": blocker, "decision_id": decision_id}
        assert enabled and capital is not None
        notional_cap = float(capital["frozen_order_notional_usdc"])
        order_price = float(capital["effective_tick_aligned_limit_price"])
        size = math.floor((notional_cap / order_price) * 100.0 + 1e-9) / 100.0
        # Reserve and submit the actual tick-rounded cost, never the pre-rounding
        # budget cap. This invariant prevents size rounding from exceeding cash.
        notional = round(size * order_price, 6)
        if notional > notional_cap + 1e-9:
            blocker = "rounded_order_notional_exceeds_cap"
            _write_decision(
                self.conn, decision_id=decision_id, slug=slug, event_date=event_date, outcome=outcome,
                will_order=False, reason_codes=[blocker], details={**common_details, "capital": capital}, created_at=now,
            )
            return {"state": "blocked", "reason": blocker, "decision_id": decision_id}
        if size + 1e-9 < float(self.config["order_policy"]["minimum_shares"]):
            blocker = "rounded_order_size_below_minimum_shares"
            _write_decision(
                self.conn, decision_id=decision_id, slug=slug, event_date=event_date, outcome=outcome,
                will_order=False, reason_codes=[blocker], details={**common_details, "capital": capital}, created_at=now,
            )
            return {"state": "blocked", "reason": blocker, "decision_id": decision_id}
        intent = {
            **common_details,
            "order_intent_id": order_intent_id,
            "decision_id": decision_id,
            "idempotency_key": idempotency_key,
            "token_id": no_token,
            "yes_token_id": yes_token,
            "side": "BUY",
            "outcome_side": "NO",
            "price": order_price,
            "size": size,
            "amount": notional,
            "notional": notional,
            "order_type": "FAK",
            "allow_market": False,
            "post_only": False,
            "direct_target_order_context": True,
            "capital": capital,
        }
        _write_decision(
            self.conn, decision_id=decision_id, slug=slug, event_date=event_date, outcome=outcome,
            will_order=True, reason_codes=["reported_trajectory_no_hard_lower_bucket"],
            details={**common_details, "capital": capital}, created_at=now, order_intent_id=order_intent_id,
        )
        self.conn.execute(
            """INSERT INTO reported_trajectory_no_intents(
                 order_intent_id,decision_id,idempotency_key,source_lane,station_id,slug,event_date,outcome,
                 no_token,hard_lower_bound_market_unit,reported_max_c,observation_id,limit_price,order_type,
                 requested_notional_usdc,requested_size,state,live_submit_authorized,capital_json,intent_json,
                 fetch_completed_at,decision_created_at,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?, 'FAK',?,?,'prepared',1,?,?,?,?,?,?)""",
            (
                order_intent_id, decision_id, idempotency_key, source_lane, station, slug, event_date, outcome,
                no_token, hard_bound, temperature_c, observation["observation_id"], order_price,
                notional, size, _json(capital), _json(intent), observation["fetch_completed_at"], now, now, now,
            ),
        )
        liquidation_intent_id = identity("rtyesliq_", order_intent_id)
        self.conn.execute(
            """INSERT INTO reported_trajectory_yes_liquidations(
                 liquidation_intent_id,entry_order_intent_id,idempotency_key,yes_token,state,
                 created_at,updated_at
               ) VALUES(?,?,?,?,'probe_pending',?,?)""",
            (
                liquidation_intent_id, order_intent_id,
                identity("rtyesliqidem_", (order_intent_id, yes_token)), yes_token, now, now,
            ),
        )
        self.conn.execute(
            """INSERT INTO reported_trajectory_capital_ledger(
                 capital_entry_id,order_intent_id,source_lane,slug,event_date,reserved_notional_usdc,
                 state,capital_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,'reserved',?,?,?)""",
            (
                identity("rtcapital_", order_intent_id), order_intent_id, source_lane, slug, event_date,
                notional, _json(capital), now, now,
            ),
        )
        return {"state": "prepared", "decision_id": decision_id, "order_intent_id": order_intent_id, "intent": intent}

    def process_source_poll(self, poll: Mapping[str, Any], *, request_started_at: str, response_completed_at: str) -> dict[str, Any]:
        lane = str(poll.get("source_lane") or "")
        if lane not in set(self.config.get("production_source_allowlist") or []):
            raise ValueError("production_source_lane_not_allowlisted")
        if poll.get("taf_requested") is not False:
            raise ValueError("temperature_only_source_contract_violated")
        poll_id = identity("rtpoll_", (lane, request_started_at, response_completed_at, poll.get("station_ids")))
        started = time.monotonic()
        new_observations: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        reports = [row for row in poll.get("reports") or [] if isinstance(row, Mapping)]
        candidate_ids: list[str] = []
        for raw in reports:
            try:
                parsed = parse_metar_temperature(
                    str(raw.get("raw_text") or ""), expected_station=str(raw.get("station_id") or "")
                )
                valid_at = (
                    _parse_utc(raw["source_report_time"])
                    if raw.get("source_report_time")
                    else infer_ddhhmm_time(
                        day=parsed.day, hour=parsed.hour, minute=parsed.minute,
                        reference=_parse_utc(response_completed_at),
                    )
                )
                if (valid_at.day, valid_at.hour, valid_at.minute) != (parsed.day, parsed.hour, parsed.minute):
                    continue
                candidate_ids.append(
                    identity(
                        "rtobs_",
                        (lane, parsed.station_id, _iso(valid_at), parsed.report_type, parsed.raw_sha256),
                    )
                )
            except (KeyError, ValueError):
                continue
        existing_ids: set[str] = set()
        unknown_ids = [value for value in candidate_ids if value not in self._known_observation_ids]
        if unknown_ids:
            placeholders = ",".join("?" for _ in unknown_ids)
            existing_ids = {
                str(row[0])
                for row in self.conn.execute(
                    f"SELECT observation_id FROM reported_trajectory_observations WHERE observation_id IN ({placeholders})",
                    unknown_ids,
                )
            }
            self._known_observation_ids.update(existing_ids)
        has_new_candidate = any(value not in self._known_observation_ids for value in candidate_ids)
        now_mono = time.monotonic()
        persist_poll = (
            has_new_candidate
            or bool(poll.get("station_errors"))
            or now_mono - self._last_poll_persist_monotonic.get(lane, 0.0) >= 60.0
        )
        if not persist_poll:
            return {
                "schema": SCHEMA_VERSION,
                "ok": True,
                "poll_id": poll_id,
                "source_lane": lane,
                "new_observation_count": 0,
                "decision_results": [],
                "submit_results": [],
                "duplicate_poll_zero_write": True,
            }
        # Freeze only the quotes that a genuinely new, non-correction report
        # could consume.  Crucially, this happens before BEGIN IMMEDIATE.  The
        # gateway performs a second current-evidence guard immediately before
        # the exchange POST, so this snapshot is used only for atomic sizing.
        quote_requests: dict[tuple[str, str], str] = {}
        enabled, _activation_block, _activation_generation = _activation(
            self.conn, config_sha256=self.config_sha256
        )
        max_price = float(self.config["order_policy"]["maximum_buy_price"])
        for raw in reports:
            try:
                preview, _insert_values = self._preview_observation(
                    source_lane=lane,
                    source_poll_id=poll_id,
                    raw=raw,
                    fetch_started_at=request_started_at,
                    fetch_completed_at=response_completed_at,
                )
            except ValueError:
                continue
            if preview["observation_id"] in self._known_observation_ids or preview["correction_suspected"]:
                continue
            for group in self._market_groups(preview["station_id"], preview["valid_at"], lane):
                market = group[0]
                hard_bound = conservative_market_temperature(
                    int(preview["reported_temperature_c"]), str(market["settlement_unit"])
                )
                target = select_nearest_impossible_lower_bucket(group, hard_lower_bound=hard_bound)
                if self.production_no_live or not enabled or target is None:
                    continue
                key = (str(preview["observation_id"]), str(market["slug"]))
                quote_requests.setdefault(key, str(target.get("no_token") or ""))
        quote_started = time.monotonic()
        quote_snapshots = self._freeze_quote_snapshots(quote_requests, max_price=max_price)
        quote_snapshot_duration_ms = round((time.monotonic() - quote_started) * 1000.0, 3)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            # A first production poll contains a short history window.  Process
            # the highest reported value first so bootstrapping creates one
            # current hard-bound decision, never a burst of historical ladder
            # decisions whose only cause is response ordering.
            def report_temperature(row: Mapping[str, Any]) -> int:
                try:
                    return parse_metar_temperature(
                        str(row.get("raw_text") or ""),
                        expected_station=str(row.get("station_id") or ""),
                    ).reported_temperature_c
                except ValueError:
                    return -10**9

            reports.sort(key=report_temperature, reverse=True)
            for raw in reports:
                try:
                    observation, inserted = self._insert_observation(
                        source_lane=lane, source_poll_id=poll_id, raw=raw,
                        fetch_started_at=request_started_at, fetch_completed_at=response_completed_at,
                    )
                except ValueError:
                    continue
                if inserted and observation is not None:
                    new_observations.append(observation)
                    for group in self._market_groups(observation["station_id"], observation["valid_at"], lane):
                        quote_key = (str(observation["observation_id"]), str(group[0]["slug"]))
                        frozen_snapshot, frozen_error = quote_snapshots.get(
                            quote_key, (None, "clob_execution_evidence_unavailable")
                        )
                        decisions.append(
                            self._advance_market_bound_and_decide(
                                observation,
                                group,
                                frozen_snapshot=frozen_snapshot,
                                frozen_quote_error=frozen_error,
                            )
                        )
            duration_ms = round((time.monotonic() - started) * 1000.0, 3)
            self.conn.execute(
                """INSERT OR IGNORE INTO reported_trajectory_source_polls(
                     poll_id,source_lane,station_ids_json,request_started_at,response_started_at,
                     response_completed_at,response_status,duration_ms,report_count,new_report_count,error_code,poll_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    poll_id, lane, _json(poll.get("station_ids") or []), request_started_at,
                    str(poll.get("response_started_at") or ""), response_completed_at,
                    int(poll.get("status") or 0), duration_ms, len(poll.get("reports") or []), len(new_observations),
                    ",".join(poll.get("station_errors") or []), _json(dict(poll)),
                ),
            )
            self.conn.commit()
            self._last_poll_persist_monotonic[lane] = now_mono
            self._known_observation_ids.update(candidate_ids)
        except Exception:
            self.conn.rollback()
            raise
        submit_results: list[dict[str, Any]] = []
        for decision in decisions:
            if decision.get("state") == "prepared":
                submit_results.append(self.submit_intent(str(decision["order_intent_id"])))
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "poll_id": poll_id,
            "source_lane": lane,
            "new_observation_count": len(new_observations),
            "quote_snapshot_count": len(quote_requests),
            "quote_snapshot_duration_ms": quote_snapshot_duration_ms,
            "decision_results": decisions,
            "submit_results": submit_results,
        }

    def _freeze_yes_position_snapshot(self, *, token_id: str) -> tuple[dict[str, Any] | None, str]:
        request = {
            "token_id": token_id,
            "side": "SELL",
            "limit_price": 0.01,
            "size": float(self.config["order_policy"]["minimum_shares"]),
            "notional": 0.0,
        }
        try:
            snapshot = self.gateway.freeze_execution_guard_snapshot(request)
        except Exception:
            return None, "yes_position_evidence_unavailable"
        if snapshot.get("ok") is True:
            return dict(snapshot), ""
        errors = [str(value) for value in snapshot.get("evidence_errors") or []]
        if any("conditional_balance" in value for value in errors):
            return None, "yes_position_balance_unavailable"
        if any("clob_ws" in value or "order_book" in value for value in errors):
            return None, "yes_position_book_unavailable"
        return None, "yes_position_evidence_unavailable"

    def _block_no_after_yes(self, order_intent_id: str, reason: str) -> dict[str, Any]:
        now = utc_now()
        row = self.conn.execute(
            "SELECT decision_id FROM reported_trajectory_no_intents WHERE order_intent_id=?",
            (order_intent_id,),
        ).fetchone()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "UPDATE reported_trajectory_no_intents SET state='blocked',last_error=?,updated_at=? WHERE order_intent_id=?",
                (reason, now, order_intent_id),
            )
            self.conn.execute(
                "UPDATE reported_trajectory_capital_ledger SET state='failed',reserved_notional_usdc=0,updated_at=? WHERE order_intent_id=?",
                (now, order_intent_id),
            )
            if row is not None:
                self.conn.execute(
                    """UPDATE auto_trade_decisions SET decided_state='blocked',will_order=0,
                         reason_codes_json=?,reasons_json=? WHERE decision_id=?""",
                    (_json([reason]), _json([reason]), str(row[0])),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"ok": False, "state": "blocked", "reason": reason, "order_intent_id": order_intent_id}

    def _liquidate_yes_before_no(self, order_intent_id: str, intent: Mapping[str, Any]) -> dict[str, Any]:
        row = self.conn.execute(
            """SELECT liquidation_intent_id,idempotency_key,yes_token,state,requested_size,
                      limit_price,size_matched
               FROM reported_trajectory_yes_liquidations WHERE entry_order_intent_id=?""",
            (order_intent_id,),
        ).fetchone()
        if row is None:
            return self._block_no_after_yes(order_intent_id, "yes_liquidation_state_missing")
        liquidation_id, key, yes_token, state = map(str, row[:4])
        if state in {"not_held", "filled"}:
            return {"ok": True, "state": state, "liquidation_intent_id": liquidation_id}
        if state in {"incomplete", "submit_failed"}:
            return self._block_no_after_yes(
                order_intent_id,
                "yes_liquidation_not_fully_filled",
            )
        if state in {"submitting", "reconcile_required"}:
            return {"ok": False, "state": state, "liquidation_intent_id": liquidation_id}
        if not yes_token:
            return self._block_no_after_yes(order_intent_id, "yes_token_mapping_missing")

        held_yes_tokens, inventory_source, inventory_observed_at = self._fresh_held_yes_tokens()
        if held_yes_tokens is None:
            return self._block_no_after_yes(order_intent_id, inventory_source)
        if yes_token not in held_yes_tokens:
            evidence = {
                "schema": "reported_trajectory_unheld_yes_attestation_v1",
                "token_id": yes_token,
                "held": False,
                "complete_inventory": True,
                "source": inventory_source,
                "inventory_observed_at": inventory_observed_at,
                "attested_at": utc_now(),
            }
            self.conn.execute(
                """UPDATE reported_trajectory_yes_liquidations
                   SET state='not_held',gateway_response_json=?,last_error=NULL,updated_at=?
                   WHERE liquidation_intent_id=?""",
                (_json(evidence), utc_now(), liquidation_id),
            )
            self.conn.commit()
            return {
                "ok": True,
                "state": "not_held",
                "liquidation_intent_id": liquidation_id,
                "position_evidence": evidence,
            }

        snapshot, evidence_error = self._freeze_yes_position_snapshot(token_id=yes_token)
        if snapshot is None:
            return self._block_no_after_yes(order_intent_id, evidence_error)
        position = _available_usdc(
            snapshot.get("conditional_balance_allowance")
            if isinstance(snapshot.get("conditional_balance_allowance"), Mapping) else {},
            decimals=int(snapshot.get("amount_decimals") or 6),
        )
        if position is None:
            return self._block_no_after_yes(order_intent_id, "yes_position_balance_unavailable")
        now = utc_now()
        if position <= 1e-9:
            self.conn.execute(
                "UPDATE reported_trajectory_yes_liquidations SET state='not_held',updated_at=? WHERE liquidation_intent_id=?",
                (now, liquidation_id),
            )
            self.conn.commit()
            return {"ok": True, "state": "not_held", "liquidation_intent_id": liquidation_id}

        sell_size = math.floor(position * 100.0 + 1e-9) / 100.0
        minimum_shares = float(self.config["order_policy"]["minimum_shares"])
        if sell_size + 1e-9 < minimum_shares:
            self.conn.execute(
                """UPDATE reported_trajectory_yes_liquidations
                   SET state='incomplete',last_error='yes_position_below_minimum_liquidatable_size',updated_at=?
                   WHERE liquidation_intent_id=?""",
                (utc_now(), liquidation_id),
            )
            self.conn.commit()
            return {"ok": True, "state": "incomplete", "liquidation_intent_id": liquidation_id}
        cumulative = 0.0
        limit_price = 0.0
        for price, size in _book_bids(snapshot):
            cumulative += size
            limit_price = price
            if cumulative + 1e-9 >= sell_size:
                break
        if limit_price <= 0:
            self.conn.execute(
                """UPDATE reported_trajectory_yes_liquidations
                   SET state='incomplete',last_error='yes_position_book_has_no_executable_bid',updated_at=?
                   WHERE liquidation_intent_id=?""",
                (utc_now(), liquidation_id),
            )
            self.conn.commit()
            return {"ok": True, "state": "incomplete", "liquidation_intent_id": liquidation_id}
        payload = {
            "schema": SCHEMA_VERSION,
            "strategy_id": "reported_trajectory_no_v1",
            "pre_entry_yes_liquidation": True,
            "entry_order_intent_id": order_intent_id,
            "order_intent_id": liquidation_id,
            "idempotency_key": key,
            "token_id": yes_token,
            "side": "SELL",
            "outcome_side": "YES",
            "price": limit_price,
            "size": sell_size,
            "amount": sell_size,
            "notional": round(sell_size * limit_price, 6),
            "order_type": "FAK",
            "allow_market": False,
            "post_only": False,
            "direct_target_order_context": True,
        }
        cursor = self.conn.execute(
            """UPDATE reported_trajectory_yes_liquidations SET state='submitting',requested_size=?,
                 limit_price=?,liquidation_json=?,updated_at=?
               WHERE liquidation_intent_id=? AND state IN ('probe_pending','prepared')""",
            (sell_size, limit_price, _json(payload), now, liquidation_id),
        )
        self.conn.commit()
        if cursor.rowcount != 1:
            return {"ok": False, "state": "cas_lost", "liquidation_intent_id": liquidation_id}
        try:
            response = self.gateway.place_order(payload)
        except Exception as exc:
            response = {"ok": False, "reconcile_required": True, "error": type(exc).__name__}
        if response.get("ok") is True and response.get("pending") is True:
            self.conn.execute(
                """UPDATE reported_trajectory_yes_liquidations SET state='submitting',
                     gateway_response_json=?,last_error=NULL,updated_at=?
                   WHERE liquidation_intent_id=?""",
                (_json(response), utc_now(), liquidation_id),
            )
            self.conn.commit()
            return {"ok": False, "state": "submitting", "liquidation_intent_id": liquidation_id, "gateway": response}
        matched = _nonnegative_float(_response_value(response, "size_matched")) or 0.0
        exchange_id = str(_response_value(response, "orderID") or "")
        status = str(_response_value(response, "status") or "").strip().lower()
        full = matched + 0.005 >= sell_size and status in {"matched", "filled"}
        ambiguous = response.get("pending") is True or response.get("reconcile_required") is True or response.get("exchange_post_started") is True
        next_state = "filled" if full else "reconcile_required" if ambiguous else "incomplete"
        self.conn.execute(
            """UPDATE reported_trajectory_yes_liquidations SET state=?,size_matched=?,
                 gateway_response_json=?,exchange_order_id=?,last_error=?,updated_at=?
               WHERE liquidation_intent_id=?""",
            (
                next_state, matched, _json(response), exchange_id or None,
                str(response.get("error") or "") or None, utc_now(), liquidation_id,
            ),
        )
        self.conn.commit()
        if next_state != "filled":
            return self._block_no_after_yes(
                order_intent_id,
                "yes_liquidation_not_fully_filled",
            )
        return {
            "ok": True,
            "state": next_state,
            "liquidation_intent_id": liquidation_id,
            "gateway": response,
        }

    def submit_intent(self, order_intent_id: str) -> dict[str, Any]:
        if self.production_no_live:
            return {
                "ok": False,
                "state": "blocked",
                "reason": "production_no_live",
                "order_intent_id": order_intent_id,
                "calls_order_gateway": False,
            }
        row = self.conn.execute(
            "SELECT state,idempotency_key,intent_json FROM reported_trajectory_no_intents WHERE order_intent_id=?",
            (order_intent_id,),
        ).fetchone()
        if row is None:
            return {"ok": False, "state": "missing", "order_intent_id": order_intent_id}
        if str(row[0]) != "prepared":
            return {"ok": True, "state": str(row[0]), "idempotent": True, "order_intent_id": order_intent_id}
        intent = json.loads(str(row[2]))
        liquidation = self._liquidate_yes_before_no(order_intent_id, intent)
        if liquidation.get("ok") is not True:
            return {**liquidation, "order_intent_id": order_intent_id}
        now = utc_now()
        cursor = self.conn.execute(
            "UPDATE reported_trajectory_no_intents SET state='submitting',gateway_received_at=?,updated_at=? WHERE order_intent_id=? AND state='prepared'",
            (now, now, order_intent_id),
        )
        self.conn.commit()
        if cursor.rowcount != 1:
            return {"ok": True, "state": "cas_lost", "order_intent_id": order_intent_id}
        try:
            response = self.gateway.place_order(intent)
        except Exception as exc:
            response = {"ok": False, "reconcile_required": True, "error": type(exc).__name__}
        if response.get("ok") is True and response.get("pending") is True:
            state, capital_state = "gateway_accepted", "submitted"
        elif response.get("ok") is True and response.get("orderID"):
            state, capital_state = "exchange_accepted", "submitted"
        elif response.get("reconcile_required") is True or response.get("exchange_post_started") is True:
            state, capital_state = "reconcile_required", "submitted"
        else:
            state, capital_state = "submit_failed", "failed"
        updated = utc_now()
        self.conn.execute(
            """UPDATE reported_trajectory_no_intents
               SET state=?,gateway_response_json=?,expected_order_id=?,exchange_order_id=?,last_error=?,
                   exchange_post_started_at=?,updated_at=? WHERE order_intent_id=?""",
            (
                state, _json(response), str(response.get("expected_order_id") or "") or None,
                str(response.get("orderID") or "") or None, str(response.get("error") or "") or None,
                str(response.get("exchange_post_started_at") or "") or None, updated, order_intent_id,
            ),
        )
        self.conn.execute(
            "UPDATE reported_trajectory_capital_ledger SET state=?,updated_at=? WHERE order_intent_id=?",
            (capital_state, updated, order_intent_id),
        )
        self.conn.commit()
        return {"ok": response.get("ok") is True, "state": state, "order_intent_id": order_intent_id, "gateway": response}

    def _prepare_exit_for_fill(
        self, order_intent_id: str, response: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """SELECT source_lane,slug,event_date,outcome,no_token,requested_size,limit_price
               FROM reported_trajectory_no_intents WHERE order_intent_id=?""",
            (order_intent_id,),
        ).fetchone()
        if row is None:
            return None
        source_lane, slug, event_date, outcome, no_token = map(str, row[:5])
        requested_size, entry_limit_price = float(row[5]), float(row[6])
        evidence = dict(response)
        matched = _nonnegative_float(_response_value(evidence, "size_matched"))
        exchange_id = str(_response_value(evidence, "orderID") or "")
        status = str(_response_value(evidence, "status") or "").strip().lower()
        if exchange_id and matched is None and hasattr(self.gateway, "lookup_exchange_order"):
            try:
                exact = self.gateway.lookup_exchange_order({"order_id": exchange_id})
            except Exception:
                exact = {}
            if isinstance(exact, Mapping) and exact.get("ok") is True:
                evidence = dict(exact)
                matched = _nonnegative_float(_response_value(evidence, "size_matched"))
                status = str(_response_value(evidence, "status") or status).strip().lower()
        terminal = status in {"matched", "filled", "cancelled", "canceled", "expired", "unmatched"}
        if matched is None:
            return None
        matched = min(requested_size, matched)
        now = utc_now()
        if matched <= 1e-9 and terminal:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute(
                    "UPDATE reported_trajectory_no_intents SET state='cancelled',last_error='fak_zero_fill',updated_at=? WHERE order_intent_id=?",
                    (now, order_intent_id),
                )
                self.conn.execute(
                    "UPDATE reported_trajectory_capital_ledger SET state='failed',reserved_notional_usdc=0,updated_at=? WHERE order_intent_id=?",
                    (now, order_intent_id),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            return {"ok": True, "state": "entry_zero_fill", "order_intent_id": order_intent_id}
        if matched <= 1e-9:
            return None

        exit_policy = self.config.get("exit_policy") if isinstance(self.config.get("exit_policy"), Mapping) else {}
        if exit_policy.get("enabled") is not True:
            return None
        exit_price = float(exit_policy.get("limit_price") or 0.99)
        exit_intent_id = identity("rtexit_", (order_intent_id, matched, exit_price))
        idempotency_key = identity("rtexitidem_", (order_intent_id, matched, exit_price))
        exit_decision_id = identity("rtdecision_", (exit_intent_id, "sell", exit_price))
        exit_payload = {
            "schema": SCHEMA_VERSION,
            "strategy_id": "reported_trajectory_no_v1",
            "exit_family": "reported_trajectory_no_exit_0_99",
            "entry_order_intent_id": order_intent_id,
            "order_intent_id": exit_intent_id,
            "decision_id": exit_decision_id,
            "idempotency_key": idempotency_key,
            "token_id": no_token,
            "side": "SELL",
            "outcome_side": "NO",
            "price": exit_price,
            "size": matched,
            "amount": matched,
            "notional": matched * exit_price,
            "order_type": "GTC",
            "allow_market": False,
            "post_only": bool(exit_policy.get("post_only", False)),
            "direct_target_order_context": True,
            "direct_target_exit_order": True,
        }
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self.conn.execute(
                "SELECT exit_intent_id,state FROM reported_trajectory_exit_orders WHERE entry_order_intent_id=?",
                (order_intent_id,),
            ).fetchone()
            if existing is not None:
                self.conn.commit()
                return {"ok": True, "state": str(existing[1]), "exit_intent_id": str(existing[0]), "idempotent": True}
            self.conn.execute(
                """INSERT INTO reported_trajectory_exit_orders(
                     exit_intent_id,entry_order_intent_id,idempotency_key,source_lane,slug,event_date,outcome,
                     no_token,entry_filled_size,limit_price,order_type,state,exit_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,'GTC','prepared',?,?,?)""",
                (
                    exit_intent_id, order_intent_id, idempotency_key, source_lane, slug, event_date,
                    outcome, no_token, matched, exit_price, _json(exit_payload), now, now,
                ),
            )
            actual_notional = min(matched * entry_limit_price, requested_size * entry_limit_price)
            self.conn.execute(
                "UPDATE reported_trajectory_capital_ledger SET state='filled',reserved_notional_usdc=?,updated_at=? WHERE order_intent_id=?",
                (actual_notional, now, order_intent_id),
            )
            if matched + 1e-9 < requested_size:
                self.conn.execute(
                    "UPDATE reported_trajectory_no_intents SET state='partially_filled',updated_at=? WHERE order_intent_id=?",
                    (now, order_intent_id),
                )
            _write_decision(
                self.conn,
                decision_id=exit_decision_id,
                slug=slug,
                event_date=event_date,
                outcome=outcome,
                will_order=True,
                reason_codes=["reported_trajectory_no_exit_0_99"],
                details={
                    "schema": SCHEMA_VERSION,
                    "entry_order_intent_id": order_intent_id,
                    "entry_filled_size": matched,
                    "exit_limit_price": exit_price,
                    "unified_capital_pool": True,
                },
                created_at=now,
                order_intent_id=exit_intent_id,
                action="exit",
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.submit_exit(exit_intent_id)

    def submit_exit(self, exit_intent_id: str) -> dict[str, Any]:
        if self.production_no_live:
            return {
                "ok": False,
                "state": "blocked",
                "reason": "production_no_live",
                "exit_intent_id": exit_intent_id,
                "calls_order_gateway": False,
            }
        row = self.conn.execute(
            "SELECT state,exit_json FROM reported_trajectory_exit_orders WHERE exit_intent_id=?",
            (exit_intent_id,),
        ).fetchone()
        if row is None:
            return {"ok": False, "state": "missing", "exit_intent_id": exit_intent_id}
        if str(row[0]) != "prepared":
            return {"ok": True, "state": str(row[0]), "exit_intent_id": exit_intent_id, "idempotent": True}
        now = utc_now()
        cursor = self.conn.execute(
            "UPDATE reported_trajectory_exit_orders SET state='submitting',updated_at=? WHERE exit_intent_id=? AND state='prepared'",
            (now, exit_intent_id),
        )
        self.conn.commit()
        if cursor.rowcount != 1:
            return {"ok": True, "state": "cas_lost", "exit_intent_id": exit_intent_id}
        payload = json.loads(str(row[1]))
        try:
            response = self.gateway.place_order(payload)
        except Exception as exc:
            response = {"ok": False, "reconcile_required": True, "error": type(exc).__name__}
        if response.get("ok") is True and response.get("orderID"):
            state = "exchange_accepted"
        elif response.get("pending") is True or response.get("reconcile_required") is True or response.get("exchange_post_started") is True:
            state = "reconcile_required"
        else:
            state = "submit_failed"
        updated = utc_now()
        next_check = _iso(_parse_utc(updated) + timedelta(seconds=10)) if state == "exchange_accepted" else updated
        self.conn.execute(
            """UPDATE reported_trajectory_exit_orders SET state=?,gateway_response_json=?,
                 exchange_order_id=?,last_error=?,next_check_at=?,updated_at=? WHERE exit_intent_id=?""",
            (
                state, _json(response), str(response.get("orderID") or "") or None,
                str(response.get("error") or "") or None, next_check, updated, exit_intent_id,
            ),
        )
        self.conn.commit()
        return {"ok": response.get("ok") is True, "state": state, "exit_intent_id": exit_intent_id, "gateway": response}

    def _reconcile_exit_one(self) -> dict[str, Any]:
        row = self.conn.execute(
            """SELECT exit_intent_id,entry_order_intent_id,idempotency_key,exchange_order_id,
                      entry_filled_size,state
               FROM reported_trajectory_exit_orders
               WHERE state IN ('submitting','reconcile_required')
                  OR (state='exchange_accepted' AND COALESCE(next_check_at,'')<=?)
               ORDER BY updated_at LIMIT 1""",
            (utc_now(),),
        ).fetchone()
        if row is None:
            return {"ok": True, "state": "idle"}
        exit_id, entry_id, key, exchange_id = map(str, row[:4])
        requested_size, prior_state = float(row[4]), str(row[5])
        try:
            if exchange_id and hasattr(self.gateway, "lookup_exchange_order"):
                response = self.gateway.lookup_exchange_order({"order_id": exchange_id})
            else:
                response = self.gateway.lookup_order({"idempotency_key": key})
        except Exception as exc:
            return {"ok": False, "state": "exit_reconcile_deferred", "error": type(exc).__name__}
        found_id = str(_response_value(response, "orderID") or exchange_id)
        status = str(_response_value(response, "status") or "").strip().lower()
        matched = _nonnegative_float(_response_value(response, "size_matched")) or 0.0
        if matched + 1e-9 >= requested_size or status in {"matched", "filled"}:
            next_state = "filled"
        elif found_id and status not in {"cancelled", "canceled", "expired"}:
            next_state = "exchange_accepted"
        elif response.get("pending") is True or response.get("reconcile_required") is True:
            next_state = "reconcile_required"
        else:
            next_state = "submit_failed"
        now = utc_now()
        next_check = _iso(_parse_utc(now) + timedelta(seconds=10)) if next_state == "exchange_accepted" else now
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                """UPDATE reported_trajectory_exit_orders SET state=?,gateway_response_json=?,
                     exchange_order_id=COALESCE(exchange_order_id,?),size_matched=?,last_error=?,
                     next_check_at=?,updated_at=? WHERE exit_intent_id=?""",
                (
                    next_state, _json(response), found_id or None, matched,
                    str(response.get("error") or "") or None, next_check, now, exit_id,
                ),
            )
            if next_state == "filled":
                self.conn.execute(
                    "UPDATE reported_trajectory_capital_ledger SET state='released',updated_at=? WHERE order_intent_id=?",
                    (now, entry_id),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"ok": next_state in {"exchange_accepted", "filled"}, "state": next_state, "exit_intent_id": exit_id, "prior_state": prior_state}

    def _reconcile_yes_liquidation_one(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            """SELECT liquidation_intent_id,entry_order_intent_id,idempotency_key,
                      exchange_order_id,requested_size,state
               FROM reported_trajectory_yes_liquidations
               WHERE state IN ('submitting','reconcile_required')
               ORDER BY updated_at LIMIT 1"""
        ).fetchone()
        if row is None:
            return None
        liquidation_id, entry_id, key = map(str, row[:3])
        exchange_id = str(row[3] or "")
        requested_size = float(row[4] or 0)
        try:
            if exchange_id and hasattr(self.gateway, "lookup_exchange_order"):
                response = self.gateway.lookup_exchange_order({"order_id": exchange_id})
            else:
                response = self.gateway.lookup_order({"idempotency_key": key})
        except Exception as exc:
            return {"ok": False, "state": "yes_liquidation_reconcile_deferred", "error": type(exc).__name__}
        matched = _nonnegative_float(_response_value(response, "size_matched")) or 0.0
        status = str(_response_value(response, "status") or "").strip().lower()
        full = requested_size > 0 and matched + 0.005 >= requested_size and status in {"matched", "filled"}
        terminal = status in {"matched", "filled", "cancelled", "canceled", "expired", "unmatched"}
        next_state = "filled" if full else "incomplete" if terminal else "reconcile_required"
        now = utc_now()
        self.conn.execute(
            """UPDATE reported_trajectory_yes_liquidations SET state=?,size_matched=?,
                 gateway_response_json=?,exchange_order_id=COALESCE(exchange_order_id,?),
                 last_error=?,updated_at=? WHERE liquidation_intent_id=?""",
            (
                next_state, matched, _json(response), str(_response_value(response, "orderID") or "") or None,
                str(response.get("error") or "") or None, now, liquidation_id,
            ),
        )
        self.conn.commit()
        if next_state == "filled":
            return {"ok": True, "state": "yes_liquidation_filled", "entry_order_intent_id": entry_id}
        if next_state == "incomplete":
            return {"ok": False, "state": "yes_liquidation_incomplete", "entry_order_intent_id": entry_id}
        return {"ok": False, "state": next_state, "liquidation_intent_id": liquidation_id}

    def reconcile_one(self) -> dict[str, Any]:
        liquidation = self._reconcile_yes_liquidation_one()
        if liquidation is not None:
            return liquidation
        row = self.conn.execute(
            """SELECT i.order_intent_id,i.idempotency_key FROM reported_trajectory_no_intents i
               JOIN reported_trajectory_capital_ledger c ON c.order_intent_id=i.order_intent_id
               WHERE i.state IN ('submitting','gateway_accepted','reconcile_required','exchange_accepted')
                 AND c.state='submitted'
               ORDER BY i.updated_at LIMIT 1"""
        ).fetchone()
        if row is None:
            return self._reconcile_exit_one()
        order_intent_id, key = str(row[0]), str(row[1])
        try:
            response = self.gateway.lookup_order({"idempotency_key": key})
        except Exception as exc:
            return {"ok": False, "state": "reconcile_deferred", "error": type(exc).__name__}
        exchange_id = str(_response_value(response, "orderID") or "")
        raw_state = str(response.get("state") or "")
        if response.get("pending") is True:
            next_state, capital_state = "gateway_accepted", "submitted"
        elif exchange_id or raw_state == "exchange_accepted":
            next_state, capital_state = "exchange_accepted", "submitted"
        elif raw_state == "submit_failed" or (response.get("ok") is False and response.get("reconcile_required") is not True):
            next_state, capital_state = "submit_failed", "failed"
        else:
            next_state, capital_state = "reconcile_required", "submitted"
        now = utc_now()
        self.conn.execute(
            """UPDATE reported_trajectory_no_intents SET state=?,gateway_response_json=?,
                  expected_order_id=COALESCE(expected_order_id,?),exchange_order_id=COALESCE(exchange_order_id,?),
                  last_error=?,updated_at=? WHERE order_intent_id=?""",
            (
                next_state, _json(response), str(response.get("expected_order_id") or "") or None,
                exchange_id or None, str(response.get("error") or "") or None, now, order_intent_id,
            ),
        )
        self.conn.execute(
            "UPDATE reported_trajectory_capital_ledger SET state=?,updated_at=? WHERE order_intent_id=?",
            (capital_state, now, order_intent_id),
        )
        self.conn.commit()
        exit_result = self._prepare_exit_for_fill(order_intent_id, response) if next_state == "exchange_accepted" else None
        return {
            "ok": next_state not in {"reconcile_required", "submit_failed"},
            "state": next_state,
            "order_intent_id": order_intent_id,
            "exit_result": exit_result,
        }


__all__ = ["ReportedTrajectoryNoEngine", "SCHEMA_VERSION"]
