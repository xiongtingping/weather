"""Canonical AWC METAR/SPECI ingress for P1 and the YES-basket fact lane.

This module deliberately has no market-order, CLOB, capital, or decision
dependencies.  It persists only the official report, its source lineage, the
station-day running maximum, and a correction halt for the downstream P1 and
basket compilers to consume.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from bayes_basket_target.source_policy import source_policy_reason
from weather_basket_yes_runtime import identity, parse_metar_temperature
from reported_trajectory_sources import infer_ddhhmm_time, utc_now


SCHEMA_VERSION = "reported_trajectory_awc_ingress_v1"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _parse_utc(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp_timezone_missing")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ReportedTrajectoryAwcIngress:
    """Bounded, deduplicated official AWC ingress with no execution surface."""

    def __init__(self, conn: sqlite3.Connection, *, config: Mapping[str, Any]) -> None:
        self.conn = conn
        self.config = dict(config)
        self._last_poll_persist_monotonic: dict[str, float] = {}
        self._known_observation_ids: set[str] = set()

    def _formal_station_lanes(self) -> dict[str, str]:
        """Return current registry stations that are formally owned by AWC."""

        source_policy = self.config.get("source_policy")
        awc_policy = source_policy.get("awc") if isinstance(source_policy, Mapping) else None
        if not isinstance(awc_policy, Mapping) or not (
            awc_policy.get("enabled") is True
            and awc_policy.get("production_ingest") is True
            and awc_policy.get("global_primary") is True
            and awc_policy.get("replaces_amsc") is True
        ):
            return {}
        rows = self.conn.execute(
            """SELECT DISTINCT station_id,city_slug,source_lane
               FROM reported_trajectory_market_registry
               WHERE source_eligible=1 AND COALESCE(station_id,'')<>''
                 AND market_active=1 AND market_accepting_orders=1
                 AND (
                   julianday(refreshed_at)>=julianday('now','-10 minutes')
                   OR (
                     EXISTS (
                       SELECT 1 FROM sqlite_master
                        WHERE type='table' AND name='reported_trajectory_market_registry_refresh_state'
                     )
                     AND julianday(COALESCE((
                       SELECT last_successful_refresh_at
                         FROM reported_trajectory_market_registry_refresh_state
                        WHERE singleton=1
                     ),'1970-01-01T00:00:00Z'))>=julianday('now','-10 minutes')
                   )
                 )
               LIMIT 129"""
        ).fetchall()
        if len(rows) > 128:
            return {}
        cities_by_station: dict[str, set[str]] = {}
        lanes_by_station: dict[str, set[str]] = {}
        for station_id, city_slug, source_lane in rows:
            station = str(station_id or "").strip().upper()
            city = str(city_slug or "").strip().lower()
            lane = str(source_lane or "").strip().lower()
            if not station or not city or lane != "awc":
                continue
            cities_by_station.setdefault(station, set()).add(city)
            lanes_by_station.setdefault(station, set()).add(lane)
        return {
            station: "awc"
            for station, cities in cities_by_station.items()
            if len(cities) == 1 and lanes_by_station.get(station) == {"awc"}
        }

    def _preview_observation(
        self,
        *,
        raw: Mapping[str, Any],
        source_poll_id: str,
        fetch_started_at: str,
        fetch_completed_at: str,
        formal_station_lanes: Mapping[str, str],
    ) -> tuple[dict[str, Any], tuple[Any, ...]]:
        parsed = parse_metar_temperature(
            str(raw.get("raw_text") or ""),
            expected_station=str(raw.get("station_id") or ""),
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
        if (valid_at.day, valid_at.hour, valid_at.minute) != (
            parsed.day,
            parsed.hour,
            parsed.minute,
        ):
            raise ValueError("source_report_time_header_mismatch")
        expected_lane = str(formal_station_lanes.get(parsed.station_id) or "")
        if expected_lane != "awc":
            raise ValueError("station_authority_lane_mismatch")
        correction = (
            " COR " in f" {parsed.raw_text} "
            or " AMD " in f" {parsed.raw_text} "
        )
        policy_reason = source_policy_reason(
            source_lane="awc",
            report_type=parsed.report_type,
            valid_at=valid_at,
            expected_lane=expected_lane,
        )
        # Routine METARs are formal at the provider's own official minute --
        # :00 in much of Asia, :20/:50 or :25/:55 in Europe, :51-:58 for the
        # ASOS-fed United States stations -- so any surviving policy reason
        # here is a genuine contract violation, not a cadence artifact.  An
        # official COR/AMD is retained as correction evidence at any minute so
        # the whole station-day fails closed pending manual review; the
        # resident P1 reader independently excludes every correction row,
        # including after a review receipt exists.
        if policy_reason:
            raise ValueError(f"nonformal_source_report:{policy_reason}")
        observation_id = identity(
            "rtobs_",
            ("awc", parsed.station_id, _iso(valid_at), parsed.report_type, parsed.raw_sha256),
        )
        observation = {
            "observation_id": observation_id,
            "source_lane": "awc",
            "station_id": parsed.station_id,
            "report_type": parsed.report_type,
            "valid_at": valid_at,
            "reported_temperature_c": parsed.reported_temperature_c,
            "correction_suspected": correction,
            "fetch_completed_at": fetch_completed_at,
        }
        return observation, (
            observation_id,
            "awc",
            parsed.station_id,
            parsed.report_type,
            _iso(valid_at),
            parsed.reported_temperature_c,
            parsed.raw_sha256,
            parsed.raw_text,
            fetch_completed_at,
            utc_now(),
            utc_now(),
            fetch_started_at,
            fetch_completed_at,
            source_poll_id,
            int(correction),
        )

    def _insert_observation(
        self,
        *,
        raw: Mapping[str, Any],
        source_poll_id: str,
        fetch_started_at: str,
        fetch_completed_at: str,
        formal_station_lanes: Mapping[str, str],
    ) -> tuple[dict[str, Any] | None, bool]:
        observation, insert_values = self._preview_observation(
            raw=raw,
            source_poll_id=source_poll_id,
            fetch_started_at=fetch_started_at,
            fetch_completed_at=fetch_completed_at,
            formal_station_lanes=formal_station_lanes,
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
        default_origin = {
            "origin_source_id": "global_noaa_awc_relay",
            "origin_country_code": "ZZ",
            "origin_authority": "NOAA/NWS Aviation Weather Center worldwide relay",
            "origin_url": "https://aviationweather.gov/data/api/",
            "origin_transport": "official_awc_raw_metar_batch_direct",
            "origin_direct_country_official": False,
            "origin_fallback_used": False,
            "origin_fallback_reason": "",
        }
        origin = {
            **default_origin,
            **{
                key: value
                for key, value in raw.items()
                if str(key).startswith(("origin_", "preferred_origin_", "official_source_registry_"))
            },
        }
        self.conn.execute(
            """INSERT INTO reported_trajectory_observation_origins(
                 observation_id,source_lane,station_id,origin_source_id,origin_country_code,
                 origin_authority,origin_url,origin_transport,direct_country_official,
                 fallback_used,fallback_reason,preferred_origin_source_id,
                 preferred_origin_authority,official_source_registry_sha256,origin_json,committed_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                observation["observation_id"],
                "awc",
                observation["station_id"],
                str(origin.get("origin_source_id") or ""),
                str(origin.get("origin_country_code") or ""),
                str(origin.get("origin_authority") or ""),
                str(origin.get("origin_url") or ""),
                str(origin.get("origin_transport") or ""),
                int(bool(origin.get("origin_direct_country_official"))),
                int(bool(origin.get("origin_fallback_used"))),
                str(origin.get("origin_fallback_reason") or ""),
                str(origin.get("preferred_origin_source_id") or ""),
                str(origin.get("preferred_origin_authority") or ""),
                str(origin.get("official_source_registry_sha256") or ""),
                _json(origin),
                utc_now(),
            ),
        )
        return observation, True

    def _station_day_scopes(self, observation: Mapping[str, Any]) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            """SELECT DISTINCT event_date,settlement_timezone
               FROM reported_trajectory_market_registry
               WHERE station_id=? AND source_lane='awc' AND source_eligible=1
                 AND market_active=1 AND market_accepting_orders=1
                 AND (
                   julianday(refreshed_at)>=julianday('now','-10 minutes')
                   OR (
                     EXISTS (
                       SELECT 1 FROM sqlite_master
                        WHERE type='table' AND name='reported_trajectory_market_registry_refresh_state'
                     )
                     AND julianday(COALESCE((
                       SELECT last_successful_refresh_at
                         FROM reported_trajectory_market_registry_refresh_state
                        WHERE singleton=1
                     ),'1970-01-01T00:00:00Z'))>=julianday('now','-10 minutes')
                   )
                 )""",
            (str(observation["station_id"]),),
        ).fetchall()
        scopes: set[tuple[str, str]] = set()
        valid_at = observation["valid_at"]
        for event_date, timezone_name in rows:
            name = str(timezone_name or "")
            try:
                local_date = valid_at.astimezone(ZoneInfo(name)).date().isoformat()
            except Exception:
                continue
            if local_date == str(event_date or ""):
                scopes.add((str(event_date), name))
        return sorted(scopes)

    def _advance_station_day_fact(self, observation: Mapping[str, Any]) -> list[dict[str, Any]]:
        now = utc_now()
        source_lane = "awc"
        station_id = str(observation["station_id"])
        results: list[dict[str, Any]] = []
        for event_date, settlement_timezone in self._station_day_scopes(observation):
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
                    (source_lane, station_id, event_date, observation["observation_id"], now),
                )
                results.append({
                    "event_date": event_date,
                    "state": "halted_for_correction_review",
                    "reason": "metar_correction_requires_manual_review",
                })
                continue
            halted = self.conn.execute(
                """SELECT reason_code FROM reported_trajectory_station_halts
                   WHERE source_lane=? AND station_id=? AND event_date=? AND state='active'""",
                (source_lane, station_id, event_date),
            ).fetchone()
            if halted is not None:
                results.append({
                    "event_date": event_date,
                    "state": "halted_pending_correction_review",
                    "reason": str(halted[0]),
                })
                continue
            existing = self.conn.execute(
                """SELECT reported_max_c,bound_generation FROM reported_trajectory_station_day_bounds
                   WHERE source_lane=? AND station_id=? AND event_date=?""",
                (source_lane, station_id, event_date),
            ).fetchone()
            temperature_c = int(observation["reported_temperature_c"])
            if existing is not None and temperature_c <= int(existing[0]):
                results.append({
                    "event_date": event_date,
                    "state": "running_max_unchanged",
                    "reported_max_c": int(existing[0]),
                })
                continue
            generation = int(existing[1]) + 1 if existing else 1
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
                    source_lane,
                    station_id,
                    event_date,
                    settlement_timezone,
                    temperature_c,
                    observation["observation_id"],
                    _iso(observation["valid_at"]),
                    generation,
                    now,
                ),
            )
            results.append({
                "event_date": event_date,
                "state": "running_max_advanced",
                "reported_max_c": temperature_c,
                "bound_generation": generation,
            })
        return results

    def process_source_poll(
        self,
        poll: Mapping[str, Any],
        *,
        request_started_at: str,
        response_completed_at: str,
    ) -> dict[str, Any]:
        lane = str(poll.get("source_lane") or "").strip().lower()
        if lane != "awc" or self.config.get("production_source_allowlist") != ["awc"]:
            raise ValueError("awc_canonical_ingress_source_lane_not_allowlisted")
        if poll.get("taf_requested") is not False:
            raise ValueError("temperature_only_source_contract_violated")
        poll_id = identity("rtpoll_", (lane, request_started_at, response_completed_at, poll.get("station_ids")))
        started = time.monotonic()
        raw_reports = [row for row in poll.get("reports") or [] if isinstance(row, Mapping)]
        formal_station_lanes = self._formal_station_lanes()
        reports: list[Mapping[str, Any]] = []
        authority_rejected_station_ids: set[str] = set()
        previews: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
        malformed_count = 0
        for raw in raw_reports:
            station_id = str(raw.get("station_id") or "").strip().upper()
            if formal_station_lanes.get(station_id) != lane:
                if station_id:
                    authority_rejected_station_ids.add(station_id)
                continue
            try:
                preview, _ = self._preview_observation(
                    raw=raw,
                    source_poll_id=poll_id,
                    fetch_started_at=request_started_at,
                    fetch_completed_at=response_completed_at,
                    formal_station_lanes=formal_station_lanes,
                )
            except (KeyError, ValueError):
                malformed_count += 1
                continue
            reports.append(raw)
            previews.append((raw, preview))
        authority_rejected_count = len(raw_reports) - len(reports) - malformed_count
        candidate_ids = [str(preview["observation_id"]) for _raw, preview in previews]
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
        base = {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "poll_id": poll_id,
            "source_lane": lane,
            "authority_rejected_report_count": authority_rejected_count,
            "authority_rejected_station_ids": sorted(authority_rejected_station_ids),
            "nonformal_or_malformed_report_count": malformed_count,
            "calls_order_gateway": False,
            "places_orders": False,
            "orders_submitted": 0,
        }
        if not persist_poll:
            return {
                **base,
                "new_observation_count": 0,
                "station_day_fact_updates": [],
                "duplicate_poll_zero_write": True,
            }
        stored_poll = dict(poll)
        stored_poll["source_lane"] = lane
        stored_poll["station_ids"] = sorted({
            str(raw.get("station_id") or "").strip().upper()
            for raw in reports
            if str(raw.get("station_id") or "").strip()
        })
        stored_poll["reports"] = [dict(raw) for raw in reports]
        new_observations: list[dict[str, Any]] = []
        fact_updates: list[dict[str, Any]] = []
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for raw, _preview in sorted(
                previews,
                key=lambda item: int(item[1]["reported_temperature_c"]),
                reverse=True,
            ):
                observation, inserted = self._insert_observation(
                    raw=raw,
                    source_poll_id=poll_id,
                    fetch_started_at=request_started_at,
                    fetch_completed_at=response_completed_at,
                    formal_station_lanes=formal_station_lanes,
                )
                if inserted and observation is not None:
                    new_observations.append(observation)
                    fact_updates.extend(self._advance_station_day_fact(observation))
            duration_ms = round((time.monotonic() - started) * 1000.0, 3)
            self.conn.execute(
                """INSERT OR IGNORE INTO reported_trajectory_source_polls(
                     poll_id,source_lane,station_ids_json,request_started_at,response_started_at,
                     response_completed_at,response_status,duration_ms,report_count,new_report_count,error_code,poll_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    poll_id,
                    lane,
                    _json(stored_poll["station_ids"]),
                    request_started_at,
                    str(poll.get("response_started_at") or ""),
                    response_completed_at,
                    int(poll.get("status") or 0),
                    duration_ms,
                    len(reports),
                    len(new_observations),
                    ",".join(poll.get("station_errors") or []),
                    _json(stored_poll),
                ),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        self._last_poll_persist_monotonic[lane] = now_mono
        self._known_observation_ids.update(candidate_ids)
        return {
            **base,
            "new_observation_count": len(new_observations),
            "station_day_fact_updates": fact_updates,
        }


__all__ = ["ReportedTrajectoryAwcIngress", "SCHEMA_VERSION"]
