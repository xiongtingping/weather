"""PR12 single-source AWC evidence export and canonical fact builder.

The source archive is read once through a query-only connection.  The
resulting evidence packet carries every field needed by the deterministic
builder; the builder never opens the archive or fills a missing field from a
second table.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .awc_observation_contract import (
    AUTHORITY_SCOPE,
    AWC_ORIGIN_AUTHORITIES,
    AWC_ORIGIN_SOURCE_IDS,
    AWC_ORIGIN_URL,
    CONTRACT_SCHEMA,
    COVERAGE_RECEIPT_SCHEMA,
    RAW_OBJECT_KIND,
    SETTLEMENT_TIMEZONE,
    CanonicalAWCObservationContract,
)
from .canonical_observation_export import _source_poll_receipt
from .contracts import ContractError, content_sha256
from .metar_precision import official_temperature_c
from .resource_bounds import assert_process_memory_budget
from .source_policy import (
    report_seen_within_early_transmission_tolerance,
    source_policy_reason,
)
from .pr12_authority_scope_projection import (
    validate_pr12_authority_scope_projection,
)


EVIDENCE_SCHEMA = "bayes_basket_target_pr12_awc_ingress_evidence_v4"
BUILDER_RECEIPT_SCHEMA = "bayes_basket_target_pr12_awc_observation_builder_receipt_v1"
BUILDER_ID = "pr12.awc_observation_builder.v1"
LEGACY_ORIGIN_AUTHORITY_BINDING_SCHEMA = (
    "bayes_basket_target_pr12_legacy_awc_origin_authority_binding_v1"
)
MAX_ROWS = 20_000
# A local day runs 25 hours across a DST fall-back and starts as much as 14
# hours before its UTC date, so 48 hours behind the caller's bound reaches
# the first report of any day that bound can land inside.
WHOLE_DAY_READ_MARGIN = timedelta(hours=48)
_CORRECTION_TOKEN = re.compile(r"(?:^|\s)(COR|AMD)(?:\s|$)")


def _utc(value: Any, label: str) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"pr12_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"pr12_{label}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _sha(value: Any, label: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ContractError(f"pr12_{label}_invalid")
    return text


def _seal(value: Mapping[str, Any]) -> dict[str, Any]:
    identity = dict(value)
    return {**identity, "content_sha256": content_sha256(identity)}


def _table_required(conn: sqlite3.Connection, table: str) -> None:
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is None:
        raise ContractError(f"pr12_source_table_missing:{table}")


def _legacy_awc_origin_authority_binding(
    origin_identity: Mapping[str, Any],
    *,
    authority_row: Mapping[str, Any],
    authority_scope_projection_content_sha256: str,
    bound_at_utc: str,
) -> dict[str, Any]:
    """Bind one exact pre-router AWC origin to the explicit authority parent.

    This is not a reconstruction of the missing historical registry value.
    The immutable legacy origin remains the predecessor and the projection
    row is an explicit successor authority receipt.
    """

    legacy_profile = {
        (0, 1, "historical_observation_pre_country_router"):
            "PRE_COUNTRY_ROUTER_GLOBAL_AWC",
        (0, 0, ""): "LEGACY_GLOBAL_AWC_RELAY_DIRECT",
        (0, 1, "legacy_awc_poll_without_country_origin_metadata"):
            "LEGACY_AWC_POLL_WITHOUT_COUNTRY_METADATA",
    }.get((
        int(origin_identity.get("direct_country_official") or 0),
        int(origin_identity.get("fallback_used") or 0),
        str(origin_identity.get("fallback_reason") or ""),
    ))
    if (
        origin_identity.get("source_lane") != "awc"
        or origin_identity.get("origin_source_id") != "global_noaa_awc_relay"
        or origin_identity.get("origin_authority") not in AWC_ORIGIN_AUTHORITIES
        or origin_identity.get("origin_url") != AWC_ORIGIN_URL
        or origin_identity.get("origin_transport")
        != "official_awc_raw_metar_batch_direct"
        or legacy_profile is None
        or origin_identity.get("preferred_origin_source_id") != ""
        or origin_identity.get("preferred_origin_authority") != ""
        or origin_identity.get("official_source_registry_sha256") != ""
        or authority_row.get("station_id") != origin_identity.get("station_id")
        or authority_row.get("source_lane") != "AWC"
        or not str(authority_row.get("source_authority_version") or "").strip()
    ):
        raise ContractError("pr12_legacy_awc_origin_authority_binding_ineligible")
    predecessor_sha = content_sha256(origin_identity)
    return _seal({
        "schema": LEGACY_ORIGIN_AUTHORITY_BINDING_SCHEMA,
        "observation_id": origin_identity["observation_id"],
        "station_id": origin_identity["station_id"],
        "predecessor_origin_identity": dict(origin_identity),
        "predecessor_origin_record_sha256": predecessor_sha,
        "predecessor_official_source_registry_present": False,
        "successor_station_authority_row_content_sha256": _sha(
            authority_row.get("content_sha256"), "station_authority_row_sha256"
        ),
        "successor_source_authority_content_sha256": _sha(
            authority_row.get("source_authority_content_sha256"),
            "source_authority_content_sha256",
        ),
        "successor_source_authority_version": str(
            authority_row["source_authority_version"]
        ),
        "authority_scope_projection_content_sha256": _sha(
            authority_scope_projection_content_sha256,
            "authority_scope_projection_content_sha256",
        ),
        "legacy_origin_profile": legacy_profile,
        "reason": "EXACT_LEGACY_GLOBAL_AWC_AUTHORITY_BINDING",
        "bound_at_utc": _stamp(_utc(bound_at_utc, "legacy_origin_bound_at_utc")),
        "authority_scope": AUTHORITY_SCOPE,
    })


def export_awc_ingress_evidence_v4(
    source_db: Path | str,
    *,
    station_registry: Mapping[str, Mapping[str, Any]],
    authority_scope_projection: Mapping[str, Any],
    station_ids: Sequence[str] = (),
    as_of_utc: str,
    since_valid_at_utc: str = "",
) -> dict[str, Any]:
    """Freeze exact AWC observation, poll, origin, halt and review evidence.

    ``since_valid_at_utc`` selects which station-days are exported: every
    local day holding a report at or after that instant, and each of them
    whole.  The pass is a full rebuild by construction -- it re-derives every
    station-day it can see -- and the archive is append-only, so an unbounded
    read grows without limit and eventually trips ``MAX_ROWS``: measured
    2026-08-21 at 44,710 AWC rows against a 20,000 cap, which fails the
    whole lane closed rather than the days it cannot fit.  Callers that have
    already materialised the older station-days pass their watermark here so
    the pass reads only what is still open.

    The instant is a UTC one and a day is a local one, so the bound lands
    inside whichever day straddles it.  The read therefore reaches
    ``WHOLE_DAY_READ_MARGIN`` further back than the bound and completes that
    day rather than exporting its tail: an exported day is always the whole
    day, because the builder numbers a day's reports from one and seals the
    ordinal into a fact whose scope key does not carry it, so a day exported
    from its tail hands the same scope different content and canonical
    ingress refuses it -- for the entire round, not just for the day.
    """

    source = Path(source_db).expanduser().resolve(strict=True)
    as_of = _utc(as_of_utc, "as_of_utc")
    frozen_registry = {
        str(station).strip().upper(): dict(record)
        for station, record in station_registry.items()
    }
    if not frozen_registry:
        raise ContractError("pr12_station_registry_missing")
    projection = validate_pr12_authority_scope_projection(authority_scope_projection)
    projection_rows = {str(row["station_id"]): dict(row) for row in projection["rows"]}
    if set(projection_rows) != set(frozen_registry):
        raise ContractError("pr12_authority_projection_station_scope_mismatch")
    timezone_map = {
        station: str(projection_rows[station]["settlement_timezone"])
        for station in frozen_registry
    }
    requested = tuple(sorted({str(x).strip().upper() for x in station_ids if str(x).strip()}))
    if requested and any(station not in timezone_map for station in requested):
        raise ContractError("pr12_station_timezone_scope_missing")
    selected = requested or tuple(sorted(timezone_map))
    for station in selected:
        try:
            ZoneInfo(timezone_map[station])
        except Exception as exc:
            raise ContractError(f"pr12_station_timezone_invalid:{station}") from exc

    placeholders = ",".join("?" for _ in selected)
    sql = f"""
        SELECT o.*,p.source_lane AS poll_source_lane,
               p.station_ids_json AS poll_station_ids_json,
               p.request_started_at AS poll_request_started_at,
               p.response_started_at AS poll_response_started_at,
               p.response_completed_at AS poll_response_completed_at,
               p.response_status AS poll_response_status,
               p.duration_ms AS poll_duration_ms,p.report_count AS poll_report_count,
               p.new_report_count AS poll_new_report_count,p.error_code AS poll_error_code,
               p.poll_json,
               g.origin_source_id,g.origin_country_code,g.origin_authority,g.origin_url,
               g.origin_transport,g.direct_country_official,g.fallback_used,
               g.fallback_reason,g.preferred_origin_source_id,
               g.preferred_origin_authority,g.official_source_registry_sha256,
               g.origin_json,g.committed_at AS origin_committed_at
          FROM reported_trajectory_observations o
          LEFT JOIN reported_trajectory_source_polls p
            ON p.poll_id=o.source_poll_id AND p.source_lane=o.source_lane
          LEFT JOIN reported_trajectory_observation_origins g
            ON g.observation_id=o.observation_id
           AND g.source_lane=o.source_lane
           AND g.station_id=o.station_id
         WHERE o.source_lane='awc' AND o.station_id IN ({placeholders})
           AND o.canonical_committed_at<=?
           AND o.valid_at>=?
         ORDER BY o.station_id,o.valid_at,o.report_type,o.observation_id
         LIMIT ?
    """
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=60.0) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        for table in (
            "reported_trajectory_observations",
            "reported_trajectory_source_polls",
            "reported_trajectory_observation_origins",
            "reported_trajectory_station_halts",
            "reported_trajectory_station_halt_reviews",
        ):
            _table_required(conn, table)
        # Read behind the caller's bound by a whole day plus the widest
        # settlement offset, so the local day the bound lands inside can be
        # completed rather than truncated.  The bound still decides which days
        # are exported; the margin only decides how much of them is visible.
        since_bound = (
            _stamp(
                _utc(since_valid_at_utc, "since_valid_at_utc")
                - WHOLE_DAY_READ_MARGIN
            )
            if str(since_valid_at_utc or "").strip()
            else ""
        )
        rows = [
            dict(row)
            for row in conn.execute(
                sql, (*selected, _stamp(as_of), since_bound, MAX_ROWS + 1)
            )
        ]
        if len(rows) > MAX_ROWS:
            raise ContractError("pr12_source_row_limit_exceeded")
        halts = [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM reported_trajectory_station_halts WHERE source_lane='awc' "
                f"AND station_id IN ({placeholders}) AND created_at<=? "
                "ORDER BY station_id,event_date,created_at",
                (*selected, _stamp(as_of)),
            )
        ]
        reviews = [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM reported_trajectory_station_halt_reviews WHERE source_lane='awc' "
                f"AND station_id IN ({placeholders}) AND reviewed_at<=? "
                "ORDER BY station_id,event_date,reviewed_at,review_id",
                (*selected, _stamp(as_of)),
            )
        ]
        conn.rollback()

    evidence_rows: list[dict[str, Any]] = []
    extraction_gaps: list[dict[str, Any]] = []
    for raw in rows:
        observation_id = str(raw.get("observation_id") or "")
        station = str(raw.get("station_id") or "").upper()
        try:
            raw_text = str(raw.get("raw_text") or "")
            raw_body_sha = _sha(raw.get("raw_sha256"), "raw_body_sha256")
            if hashlib.sha256(raw_text.encode("utf-8")).hexdigest() != raw_body_sha:
                raise ContractError("pr12_raw_body_sha256_mismatch")
            valid_at = _utc(raw.get("valid_at"), "valid_at")
            first_seen = _utc(raw.get("first_seen_at"), "first_seen_at")
            parsed_at = _utc(raw.get("parsed_at"), "parsed_at")
            committed_at = _utc(raw.get("canonical_committed_at"), "canonical_committed_at")
            fetch_started = _utc(raw.get("fetch_started_at"), "fetch_started_at")
            fetch_completed = _utc(raw.get("fetch_completed_at"), "fetch_completed_at")
            if not (
                report_seen_within_early_transmission_tolerance(valid_at, first_seen)
                and first_seen <= parsed_at <= committed_at
                and fetch_started <= first_seen <= fetch_completed <= committed_at
            ):
                raise ContractError("pr12_observation_timestamp_order_invalid")
            poll_payload = json.loads(str(raw.get("poll_json") or ""))
            poll_object_sha = content_sha256(poll_payload)
            source_receipt_sha = _source_poll_receipt(raw, expected_lane="awc")
            origin_json = json.loads(str(raw.get("origin_json") or ""))
            if not isinstance(origin_json, Mapping):
                raise ContractError("pr12_origin_json_invalid")
            origin_committed = _utc(
                raw.get("origin_committed_at"), "origin_committed_at"
            )
            if origin_committed > as_of:
                raise ContractError("pr12_origin_committed_after_as_of")
            origin_identity = {
                "observation_id": observation_id,
                "source_lane": "awc",
                "station_id": station,
                "origin_source_id": str(raw.get("origin_source_id") or ""),
                "origin_country_code": str(raw.get("origin_country_code") or ""),
                "origin_authority": str(raw.get("origin_authority") or ""),
                "origin_url": str(raw.get("origin_url") or ""),
                "origin_transport": str(raw.get("origin_transport") or ""),
                "direct_country_official": int(raw.get("direct_country_official")),
                "fallback_used": int(raw.get("fallback_used")),
                "fallback_reason": str(raw.get("fallback_reason") or ""),
                "preferred_origin_source_id": str(
                    raw.get("preferred_origin_source_id") or ""
                ),
                "preferred_origin_authority": str(
                    raw.get("preferred_origin_authority") or ""
                ),
                "official_source_registry_sha256": str(
                    raw.get("official_source_registry_sha256") or ""
                ),
                "origin_json": origin_json,
                "committed_at": _stamp(origin_committed),
            }
            authority_binding = None
            try:
                registry_sha = _sha(
                    origin_identity["official_source_registry_sha256"],
                    "origin_registry_sha256",
                )
            except ContractError as exc:
                try:
                    authority_binding = _legacy_awc_origin_authority_binding(
                        origin_identity,
                        authority_row=projection_rows[station],
                        authority_scope_projection_content_sha256=projection[
                            "content_sha256"
                        ],
                        bound_at_utc=projection["as_of_utc"],
                    )
                    registry_sha = authority_binding["content_sha256"]
                except ContractError as binding_exc:
                    raise ContractError(
                        "pr12_lineage_missing_official_source_registry"
                    ) from binding_exc
            origin_ok = (
                origin_identity["origin_source_id"] in AWC_ORIGIN_SOURCE_IDS
                and origin_identity["origin_url"] == AWC_ORIGIN_URL
                and str(origin_identity["origin_transport"]).startswith("official_awc_")
                and origin_identity["origin_authority"] in AWC_ORIGIN_AUTHORITIES
            )
            zone = ZoneInfo(timezone_map[station])
            report_type = str(raw.get("report_type") or "").upper()
            if report_type not in {"METAR", "SPECI"}:
                raise ContractError("pr12_report_type_invalid")
            # The raw body was hash-verified above, so the report's own
            # ``Tsnnnsnnn`` remark group can be read here.  Reports captured
            # before that group was parsed carry only whole degrees in the
            # column; the bytes carry the tenth either way.  A remark group
            # that contradicts the body group falls back to the body group,
            # which is what this builder read before.
            temperature_c, temperature_resolution_c = official_temperature_c(
                raw_text,
                whole_degree_c=float(raw.get("reported_temperature_c")),
            )
            # The lane's own minute rule used to live here: a routine METAR
            # was formal only at :00.  Reporting cadence is a per-station
            # property of the network -- 18 of the 45 publish only away from
            # the hour (Europe at :20/:50 or :25/:55, the ASOS-fed United
            # States at :51-:58) -- so that rule identified none of their
            # routine reports at all, and eight stations held no revision
            # fact of any kind.  The operator settled this on 2026-08-10 for
            # the truth lane (2b73cfdb); the ruling is the same one, so this
            # asks the same function rather than restating it, and the two
            # lanes admit the identical set by construction.
            policy_excluded = bool(source_policy_reason(
                source_lane="awc",
                report_type=report_type,
                valid_at=valid_at,
                expected_lane="awc",
            ))
            correction = _CORRECTION_TOKEN.search(raw_text)
            evidence_rows.append(
                {
                    "observation_id": observation_id,
                    "station_id": station,
                    "event_date": valid_at.astimezone(zone).date().isoformat(),
                    "settlement_timezone": timezone_map[station],
                    "source_lane": "AWC",
                    "report_type": report_type,
                    "observed_at_utc": _stamp(valid_at),
                    "received_at_utc": _stamp(first_seen),
                    "first_seen_at_utc": _stamp(first_seen),
                    "parsed_at_utc": _stamp(parsed_at),
                    "fetch_started_at_utc": _stamp(fetch_started),
                    "fetch_completed_at_utc": _stamp(fetch_completed),
                    "canonical_committed_at_utc": _stamp(committed_at),
                    "reported_temperature_c": temperature_c,
                    "temperature_resolution_c": temperature_resolution_c,
                    "raw_text": raw_text,
                    "raw_object_kind": RAW_OBJECT_KIND,
                    "raw_object_sha256": raw_body_sha,
                    "source_poll_payload_sha256": poll_object_sha,
                    "awc_source_receipt_sha256": source_receipt_sha,
                    "origin_source_id": origin_identity["origin_source_id"],
                    "origin_authority": origin_identity["origin_authority"],
                    "origin_url": origin_identity["origin_url"],
                    "origin_transport": origin_identity["origin_transport"],
                    "origin_registry_sha256": registry_sha,
                    "origin_record_sha256": content_sha256(origin_identity),
                    "origin_authority_binding": authority_binding,
                    "origin_committed_at_utc": origin_identity["committed_at"],
                    "origin_lineage_ok": origin_ok,
                    "policy_excluded": policy_excluded,
                    "correction_kind": correction.group(1) if correction else "NONE",
                    "correction_suspected": int(raw.get("correction_suspected") or 0),
                }
            )
        except Exception as exc:
            failure_code = (
                "LINEAGE_MISSING_OFFICIAL_SOURCE_REGISTRY"
                if "lineage_missing_official_source_registry" in str(exc)
                else "SOURCE_HASH_MISMATCH"
                if "sha256_mismatch" in str(exc)
                else "LINEAGE_MISMATCH"
            )
            extraction_gaps.append(
                {
                    "station_id": station,
                    "observation_id": observation_id,
                    "failure_code": failure_code,
                    "detail": str(exc),
                }
            )
        assert_process_memory_budget()

    # A local day the read can only see part of must not be exported at all.
    #
    # ``event_date`` is a local day, but the read is bounded by a UTC instant,
    # so the bound lands inside whichever local day straddles it.  The builder
    # numbers a day's usable reports from one in observed order and seals that
    # ordinal into the fact, while the fact's scope key does not carry it -- so
    # a day rebuilt from its tail hands the same scope different content, and
    # canonical ingress refuses that (``canonical_scope_content_conflict``) for
    # the whole savepoint rather than for the day.  On 2026-08-26 CYYZ
    # 2026-08-23 did exactly that: 48 usable reports of which 44 fell before
    # the bound, so its 45th report came back renumbered as its 1st, and one
    # renumbered day refused all 1,135 facts of the round.
    #
    # Cutting the FRONT of a day renumbers it; extending its BACK does not,
    # because the ordinal counts forward from the day's first report -- which
    # is what lets an open day be exported now and re-exported later, once
    # more of it has arrived, with its earlier facts byte-identical.  Refusing
    # partly-visible days is therefore what makes a rebuild idempotent.  It is
    # a refusal and not a repair: how far back to read is the caller's
    # decision, and days older than that belong to the historical lane.
    truncated_day_scopes: list[dict[str, str]] = []
    if since_bound:
        since_instant = _utc(since_valid_at_utc, "since_valid_at_utc")
        read_instant = since_instant - WHOLE_DAY_READ_MARGIN
        # A day is in the window if any of its reports is, and it is then
        # exported in full -- including the reports that precede the caller's
        # bound, which is what the margin was read for.  Dropping such a day
        # instead was tried first and is worse than the disease: measured on
        # the 2026-08-26 recovery it would have discarded 18 station-days of
        # 2026-08-22 that nothing had ever identified, permanently, because a
        # day only gets older and no later round reaches back for it.
        in_window: set[tuple[str, str]] = set()
        day_start: dict[tuple[str, str], datetime] = {}
        for row in evidence_rows:
            key = (str(row["station_id"]), str(row["event_date"]))
            if key not in day_start:
                day_start[key] = datetime.combine(
                    date.fromisoformat(str(row["event_date"])),
                    datetime.min.time(),
                    ZoneInfo(str(row["settlement_timezone"])),
                )
            if _utc(row["observed_at_utc"], "observed_at_utc") >= since_instant:
                in_window.add(key)
        # The margin covers the longest local day against the widest offset in
        # the fleet, so a day in the window is whole in the read.  The check
        # stays because a margin that is ever too small must say so rather
        # than renumber a day, which is the failure this whole rule exists for.
        truncated = {
            key: day_start[key]
            for key in in_window
            if day_start[key] < read_instant
        }
        evidence_rows = [
            row for row in evidence_rows
            if (str(row["station_id"]), str(row["event_date"])) in in_window
            and (str(row["station_id"]), str(row["event_date"])) not in truncated
        ]
        truncated_day_scopes = [
            {
                "station_id": station,
                "event_date": event_date,
                "local_day_start_utc": _stamp(truncated[(station, event_date)]),
                "since_valid_at_utc": _stamp(since_instant),
                "read_bound_utc": _stamp(read_instant),
                "failure_code": "PR12_LOCAL_DAY_PARTLY_BEFORE_READ_BOUND",
            }
            for station, event_date in sorted(truncated)
        ]

    observed_stations = {str(row["station_id"]) for row in evidence_rows}
    for station in selected:
        if station not in observed_stations:
            extraction_gaps.append(
                {
                    "station_id": station,
                    "observation_id": "",
                    "failure_code": "DATA_MISSING",
                    "detail": "pr12_awc_observation_rows_missing",
                }
            )

    halt_rows = [dict(sorted({k: row[k] for k in row}.items())) for row in halts]
    review_rows = [dict(sorted({k: row[k] for k in row}.items())) for row in reviews]
    identity = {
        "schema": EVIDENCE_SCHEMA,
        "as_of_utc": _stamp(as_of),
        "source_database": str(source),
        "station_registry": {
            station: frozen_registry[station] for station in selected
        },
        "authority_scope_projection": projection,
        "station_timezones": dict(sorted((s, timezone_map[s]) for s in selected)),
        "rows": evidence_rows,
        "station_day_halts": halt_rows,
        "station_day_reviews": review_rows,
        "extraction_gaps": extraction_gaps,
        "since_valid_at_utc": (
            _stamp(_utc(since_valid_at_utc, "since_valid_at_utc"))
            if str(since_valid_at_utc or "").strip()
            else ""
        ),
        "read_bound_utc": since_bound,
        "truncated_day_scopes": truncated_day_scopes,
        "source_read_only": True,
        "source_query_only": True,
    }
    return {**identity, "receipt_sha256": content_sha256(identity)}


def _expected_hourly_slots(event_date: str, zone_name: str) -> list[str]:
    zone = ZoneInfo(zone_name)
    local_start = datetime.combine(date.fromisoformat(event_date), datetime.min.time(), zone)
    local_end = datetime.combine(
        date.fromisoformat(event_date) + timedelta(days=1), datetime.min.time(), zone
    )
    cursor = local_start.astimezone(timezone.utc)
    end = local_end.astimezone(timezone.utc)
    slots: list[str] = []
    while cursor < end:
        slots.append(_stamp(cursor))
        cursor += timedelta(hours=1)
    return slots


def build_awc_observation_contract(
    evidence: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build clean closed-day facts and a complete terminal gap ledger."""

    if evidence.get("schema") != EVIDENCE_SCHEMA:
        raise ContractError("pr12_evidence_schema_invalid")
    supplied = _sha(evidence.get("receipt_sha256"), "evidence_receipt_sha256")
    identity = {k: v for k, v in evidence.items() if k != "receipt_sha256"}
    if supplied != content_sha256(identity):
        raise ContractError("pr12_evidence_receipt_sha256_mismatch")
    as_of = _utc(evidence.get("as_of_utc"), "evidence_as_of_utc")
    rows = [dict(row) for row in evidence.get("rows") or ()]
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["station_id"], row["event_date"], row["settlement_timezone"])].append(row)
    halt_by_day = {
        (str(x["station_id"]), str(x["event_date"])): x
        for x in evidence.get("station_day_halts") or ()
    }
    reviews_by_day: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for review in evidence.get("station_day_reviews") or ():
        reviews_by_day[(str(review["station_id"]), str(review["event_date"]))].append(review)

    revision_facts: list[dict[str, Any]] = []
    day_bounds_facts: list[dict[str, Any]] = []
    coverage_receipts: list[dict[str, Any]] = []
    terminal_gaps: list[dict[str, Any]] = [dict(x) for x in evidence.get("extraction_gaps") or ()]
    registry = evidence.get("station_registry")
    if not isinstance(registry, Mapping):
        raise ContractError("pr12_evidence_station_registry_missing")
    projection_raw = evidence.get("authority_scope_projection")
    if not isinstance(projection_raw, Mapping):
        raise ContractError("pr12_evidence_authority_projection_missing")
    projection = validate_pr12_authority_scope_projection(projection_raw)
    projection_rows = {str(row["station_id"]): row for row in projection["rows"]}
    for (station, event_date, zone_name), day_rows in sorted(grouped.items()):
        day_scope = {"station_id": station, "event_date": event_date, "settlement_timezone": zone_name}
        formal = [x for x in day_rows if not x["policy_excluded"]]
        # One slot, one report (operator ruling 2026-09-03, "discard becomes
        # replace").  A COR/AMD is the observer withdrawing a report and
        # restating it, so it stands in the slot of the report it corrects
        # and publication order decides between them.  Both used to survive
        # here and the correction was then excluded from everything below,
        # which cost this lane twice: two bodies in one slot tripped
        # ``REVISION_LINEAGE_MISSING``, and an hour whose only METAR was a
        # correction counted as unobserved -- ``DATA_MISSING``, so the day
        # never closed.  Seventeen such hours in the archive from 2026-08-20
        # (KMIA six, KLGA three, KDAL two, FACT two).
        by_slot: dict[tuple[str, str], dict[str, Any]] = {}
        superseded_observation_ids: list[str] = []
        for row in sorted(
            formal,
            key=lambda x: (
                x["observed_at_utc"],
                x["report_type"],
                x["canonical_committed_at_utc"],
                x["observation_id"],
            ),
        ):
            slot = (str(row["report_type"]), str(row["observed_at_utc"]))
            held = by_slot.get(slot)
            if held is not None:
                superseded_observation_ids.append(str(held["observation_id"]))
            by_slot[slot] = row
        formal = sorted(
            by_slot.values(),
            key=lambda x: (
                x["observed_at_utc"], x["report_type"], x["observation_id"]
            ),
        )
        reasons: set[str] = set()
        registry_row = registry.get(station)
        authority_row = projection_rows.get(station)
        if (
            not isinstance(registry_row, Mapping)
            or set(registry_row) != {
                "station_id", "settlement_definition", "content_sha256"
            }
            or registry_row.get("station_id") != station
            or registry_row.get("settlement_definition") != "official_daily_tmax_c"
            or len(str(registry_row.get("content_sha256") or "")) != 64
            or not isinstance(authority_row, Mapping)
            or authority_row.get("legacy_station_registry_content_sha256")
            != registry_row.get("content_sha256")
        ):
            reasons.add("LINEAGE_MISMATCH")
        if (
            not isinstance(authority_row, Mapping)
            or authority_row.get("settlement_timezone") != zone_name
        ):
            reasons.add("TIMEZONE_MISMATCH")
        for row in formal:
            binding = row.get("origin_authority_binding")
            if binding is None:
                continue
            try:
                if not isinstance(binding, Mapping):
                    raise ContractError("pr12_legacy_origin_binding_mapping_required")
                predecessor = binding.get("predecessor_origin_identity")
                if not isinstance(predecessor, Mapping) or not isinstance(
                    authority_row, Mapping
                ):
                    raise ContractError("pr12_legacy_origin_binding_parent_missing")
                rebuilt = _legacy_awc_origin_authority_binding(
                    predecessor,
                    authority_row=authority_row,
                    authority_scope_projection_content_sha256=projection[
                        "content_sha256"
                    ],
                    bound_at_utc=projection["as_of_utc"],
                )
                if (
                    rebuilt != binding
                    or binding["observation_id"] != row["observation_id"]
                    or binding["station_id"] != station
                    or binding["predecessor_origin_record_sha256"]
                    != row["origin_record_sha256"]
                    or binding["content_sha256"] != row["origin_registry_sha256"]
                ):
                    raise ContractError("pr12_legacy_origin_binding_scope_mismatch")
            except (ContractError, KeyError, TypeError):
                reasons.add("LINEAGE_MISMATCH")
        if any(not x["origin_lineage_ok"] for x in formal):
            reasons.add("LINEAGE_MISMATCH")
        slot_hashes: dict[tuple[str, str], set[str]] = defaultdict(set)
        for row in formal:
            slot_hashes[(row["report_type"], row["observed_at_utc"])].add(
                row["raw_object_sha256"]
            )
        # A structural invariant now rather than a data condition: the slot
        # resolver above leaves one report per slot, so two bodies here would
        # mean it failed to collapse one.  Before that resolver this fired on
        # every correction, which is the ordinary life of a METAR.
        if any(len(values) > 1 for values in slot_hashes.values()):
            reasons.add("REVISION_LINEAGE_MISSING")
        correction_rows = [
            x
            for x in day_rows
            if x["correction_kind"] != "NONE" or x["correction_suspected"] == 1
        ]
        halt = halt_by_day.get((station, event_date))
        reviews = sorted(
            reviews_by_day.get((station, event_date), ()),
            key=lambda x: (str(x.get("reviewed_at") or ""), str(x.get("review_id") or "")),
        )
        # A correction is resolved by its review receipt, the same way the
        # settled-truth lane judges it (``assert_station_day_corrections_
        # resolved``): the latest review naming that observation clears it
        # with either CLEAR decision.  This used to demand, in addition, that
        # the day's halt row read ``cleared`` -- and nothing but the manual
        # review script ever wrote that state.  The standing auto-clear policy
        # appends the receipt and leaves the row ``active``, so every one of
        # the 356 halts in the archive was active on 2026-09-02, 41 of the 43
        # recent ones with a matching CLEAR receipt already on file, and this
        # gate refused revision facts for the whole station-day anyway: SAEZ
        # 09-02 was cleared at 10:11:49Z and its facts stopped at 03:36Z, so
        # the six P1 checkpoints minted after that carried no revision hash
        # and the station-day was refused at the atomic-fact boundary on
        # every cycle.  The halt row is a wait, not a fact; the receipt is
        # the fact.  A quarantine still stops the day, by receipt or by the
        # manual script's own halt reason.
        latest_review_by_trigger: dict[str, Mapping[str, Any]] = {}
        for review in reviews:
            latest_review_by_trigger[
                str(review.get("triggering_observation_id") or "")
            ] = review
        unresolved_corrections = [
            row
            for row in correction_rows
            if int(row.get("correction_suspected") or 0) == 1
            and str(
                latest_review_by_trigger.get(
                    str(row.get("observation_id") or ""), {}
                ).get("decision") or ""
            ) not in {"CLEAR_REFERENCE_ONLY", "CLEAR_WITH_REPLACEMENT"}
        ]
        if unresolved_corrections:
            reasons.add("REVISION_REVIEW_REQUIRED")
        if halt and str(halt.get("reason_code") or "") == (
            "manual_review_quarantine_station_day"
        ):
            reasons.add("REVISION_REVIEW_REQUIRED")
        if reviews and reviews[-1].get("decision") == "QUARANTINE_STATION_DAY":
            reasons.add("REVISION_REVIEW_REQUIRED")
        expected_slots = _expected_hourly_slots(event_date, zone_name)
        # A slot is an hour of the day, not an instant in it.  The expected
        # slots are the day's clock hours, so comparing report instants
        # against them only ever agreed for stations that publish exactly on
        # the hour; a station on :51 produced 24 missing and 24 extra slots
        # every day and could never close one.  Each routine report is
        # therefore counted against the hour it falls in.  For a station that
        # does publish at :00 this is the identity -- the hour of :00 is :00 --
        # so no coverage receipt that already exists changes value.
        observed_slots = sorted(
            {
                _stamp(
                    _utc(x["observed_at_utc"], "observed_at_utc").replace(
                        minute=0, second=0, microsecond=0
                    )
                )
                for x in formal
                if x["report_type"] == "METAR"
            }
        )
        missing_slots = sorted(set(expected_slots) - set(observed_slots))
        extra_slots = sorted(set(observed_slots) - set(expected_slots))
        zone = ZoneInfo(zone_name)
        day_start = datetime.combine(date.fromisoformat(event_date), datetime.min.time(), zone)
        day_end = datetime.combine(
            date.fromisoformat(event_date) + timedelta(days=1), datetime.min.time(), zone
        )
        if as_of < day_end.astimezone(timezone.utc):
            reasons.add("TRUTH_NOT_CLOSED")
        if missing_slots or extra_slots:
            reasons.add("DATA_MISSING")
        # Two different facts, two different preconditions.  A day-bounds fact
        # states the day's maximum, so it genuinely cannot exist before the
        # day closes with complete hourly coverage.  A revision fact states
        # only "this report, canonically identified" -- immutable the moment
        # the report is committed, and true of an open day's reports exactly
        # as of a closed day's.  Gating both on closure meant the live P1
        # stage could never carry the revision hash its own atomic root
        # requires (2026-08-06 made that hash mandatory; P1 has not reached a
        # commit since 2026-08-01), while the offline lane that does produce
        # them only ever covers yesterday.
        #
        # A review hold is different in kind and still stops everything: a
        # correction nobody has reviewed, or a quarantine, means these reports
        # are disputed, and the charter says such a station-day halts for
        # review rather than producing facts.  A reviewed correction is not a
        # dispute, whatever its halt row still says.
        review_blocked = "REVISION_REVIEW_REQUIRED" in reasons
        day_bounds_blocked = bool(reasons)
        if reasons:
            terminal_gaps.append(
                {
                    **day_scope,
                    "failure_code": sorted(reasons)[0],
                    "failure_codes": sorted(reasons),
                    "expected_hourly_metar_count": len(expected_slots),
                    "observed_hourly_metar_count": len(observed_slots),
                    "missing_hourly_slots": missing_slots,
                    "extra_hourly_slots": extra_slots,
                    "superseded_observation_ids": superseded_observation_ids,
                    "revision_facts_emitted": not review_blocked,
                }
            )
        if review_blocked:
            continue

        usable = sorted(
            formal,
            key=lambda x: (x["observed_at_utc"], x["report_type"], x["observation_id"]),
        )
        day_revisions: list[dict[str, Any]] = []
        for sequence, row in enumerate(usable, start=1):
            fact = _seal(
                {
                    "station_id": station,
                    "observation_id": row["observation_id"],
                    "revision_no": 1,
                    "event_date": event_date,
                    "settlement_timezone": zone_name,
                    "source_lane": "AWC",
                    "authority_scope": AUTHORITY_SCOPE,
                    "authority_scope_projection_content_sha256": projection[
                        "content_sha256"
                    ],
                    "report_type": row["report_type"],
                    "sequence": sequence,
                    "observed_at_utc": row["observed_at_utc"],
                    "received_at_utc": row["received_at_utc"],
                    "first_seen_at_utc": row["first_seen_at_utc"],
                    "parsed_at_utc": row["parsed_at_utc"],
                    "fetch_started_at_utc": row["fetch_started_at_utc"],
                    "fetch_completed_at_utc": row["fetch_completed_at_utc"],
                    "origin_committed_at_utc": row["origin_committed_at_utc"],
                    "source_observation_committed_at_utc": row[
                        "canonical_committed_at_utc"
                    ],
                    "canonical_committed_at_utc": max(
                        row["canonical_committed_at_utc"],
                        row["origin_committed_at_utc"],
                    ),
                    "reported_temperature_c": row["reported_temperature_c"],
                    "raw_object_kind": row["raw_object_kind"],
                    "raw_object_sha256": row["raw_object_sha256"],
                    "awc_source_receipt_sha256": row["awc_source_receipt_sha256"],
                    "origin_source_id": row["origin_source_id"],
                    "origin_authority": row["origin_authority"],
                    "origin_url": row["origin_url"],
                    "origin_transport": row["origin_transport"],
                    "origin_registry_sha256": row["origin_registry_sha256"],
                    "origin_record_sha256": row["origin_record_sha256"],
                    "correction_kind": "NONE",
                    "correction_state": "NO_CORRECTION",
                    "supersedes_observation_fact_sha256": None,
                    "qc_state": "ACCEPTED",
                    "review_receipt_sha256": None,
                    "halt_state": "CLEAR",
                }
            )
            day_revisions.append(fact)
        if day_bounds_blocked:
            # The reports are identified; the day's maximum is not yet a fact.
            # Emitting the revisions alone is valid by the contract's own
            # cross-check, which requires every day-bounds fact to resolve its
            # revisions but never the converse.
            revision_facts.extend(day_revisions)
            continue
        if not day_revisions:
            continue
        maximum = min(
            (
                fact
                for fact in day_revisions
                if fact["reported_temperature_c"]
                == max(x["reported_temperature_c"] for x in day_revisions)
            ),
            key=lambda x: (x["sequence"], x["content_sha256"]),
        )
        day_evidence_receipt = _seal(
            {
                "schema": "bayes_basket_target_pr12_awc_day_evidence_receipt_v1",
                **day_scope,
                "formal_evidence_row_sha256s": sorted(
                    content_sha256(row) for row in formal
                ),
            }
        )
        coverage = _seal(
            {
                "schema": COVERAGE_RECEIPT_SCHEMA,
                **day_scope,
                "as_of_utc": _stamp(as_of),
                "expected_hourly_slots": expected_slots,
                "observed_hourly_slots": observed_slots,
                "expected_hourly_metar_count": len(expected_slots),
                "observed_hourly_metar_count": len(observed_slots),
                "coverage_complete": True,
                # Bind only this station-day evidence.  Binding the mutable
                # outer batch receipt made an unchanged closed day acquire a
                # new hash whenever another day arrived, violating the
                # append-only canonical scope.
                "source_evidence_receipt_sha256": day_evidence_receipt[
                    "content_sha256"
                ],
            }
        )
        max_ties = sorted(
            x["content_sha256"]
            for x in day_revisions
            if x["reported_temperature_c"] == maximum["reported_temperature_c"]
        )
        bounds = _seal(
            {
                **day_scope,
                "bounds_version": 1,
                "source_lane": "AWC",
                "authority_scope_projection_content_sha256": projection[
                    "content_sha256"
                ],
                "day_start_utc": _stamp(day_start.astimezone(timezone.utc)),
                "day_end_utc": _stamp(day_end.astimezone(timezone.utc)),
                "running_max_c": maximum["reported_temperature_c"],
                "max_observation_fact_sha256": maximum["content_sha256"],
                "observation_revision_vector_sha256": content_sha256(
                    [x["content_sha256"] for x in day_revisions]
                ),
                "max_tie_vector_sha256": content_sha256(max_ties),
                "max_tie_break_rule": "EARLIEST_SEQUENCE_THEN_FACT_SHA",
                "coverage_receipt": coverage,
                "coverage_receipt_sha256": coverage["content_sha256"],
                "correction_state": "NO_CORRECTION",
                "halt_state": "CLEAR",
                "latest_review_receipt_sha256": None,
                "closed_day": True,
                "canonical_committed_at_utc": _stamp(as_of),
                "authority_scope": AUTHORITY_SCOPE,
            }
        )
        revision_facts.extend(day_revisions)
        day_bounds_facts.append(bounds)
        coverage_receipts.append(coverage)

    contract_identity = {
        "schema": CONTRACT_SCHEMA,
        "as_of_utc": _stamp(as_of),
        "builder_id": BUILDER_ID,
        "authority_scope_projection": projection,
        "revision_facts": revision_facts,
        "day_bounds_facts": day_bounds_facts,
    }
    contract = {
        **contract_identity,
        "content_sha256": content_sha256(contract_identity),
    }
    CanonicalAWCObservationContract.from_mapping(contract)
    receipt_identity = {
        "schema": BUILDER_RECEIPT_SCHEMA,
        "state": "MATERIALIZED_UNPUBLISHED" if day_bounds_facts else "BLOCKED",
        "authority_scope": AUTHORITY_SCOPE,
        "evidence_receipt_sha256": supplied,
        "contract_content_sha256": contract["content_sha256"],
        "revision_fact_count": len(revision_facts),
        "day_bounds_fact_count": len(day_bounds_facts),
        "coverage_receipts": coverage_receipts,
        "terminal_gaps": terminal_gaps,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "publishes_pr04_pointer": False,
        "starts_services": False,
    }
    return contract, {**receipt_identity, "receipt_sha256": content_sha256(receipt_identity)}


__all__ = [
    "BUILDER_ID",
    "BUILDER_RECEIPT_SCHEMA",
    "EVIDENCE_SCHEMA",
    "build_awc_observation_contract",
    "export_awc_ingress_evidence_v4",
]
