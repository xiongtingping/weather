"""Charter 225: re-settle a station-day from the market's official settlement source.

User ruling 2026-09-25, 「全部按结算」: truth is what the market settled on.
Where a settled market paid a bucket the standing truth is not in, the day is
re-settled from that market's own resolution page, as a revision of the
SETTLEMENT_SOURCE lane -- never relabelled AWC, never a withdrawal.  The old
revision stays as audit.

Two resolution pages exist, each read the way its market's rules read it:

* Wunderground daily history (``WU_DAILY_OBSERVATIONS``) -- the page's own
  observation table, in the market's unit (the ``wu_units`` cookie), bound to
  the station and date by the page's ``airport-body`` attributes.  The value is
  the table's highest whole-degree temperature.
* NOAA's time series (``NOAA_WRH_TIMESERIES``) -- the page's own Synoptic
  response, summarised in the page: the day's highest reading, and for a US
  Fahrenheit market the "Show Hourly Data" view its rules name (routine
  reports plus SPECI).

The decision, in this order:

1. the page's value, rounded to the market's whole degree, is inside the paid
   bucket: the page's value is the truth (``SOURCE_PAGE_IN_PAID_BUCKET``);
2. otherwise, the paid bucket is one whole degree: that degree is the truth
   (``PAID_WHOLE_DEGREE_SOURCE_REVISED_AFTER_FINALITY``).  The market's rules
   stop considering revisions once the next day's first datapoint is out, and
   NOAA later backfilled five Asian stations' 2026-09-20 afternoons -- the
   page now reads what AWC read, the market paid what the page read then;
3. otherwise (a 2°F range or an open edge the page does not support) the
   value is undetermined and the day fails closed.

A day is repaired only while its standing truth is outside the paid bucket,
and never over a settlement-source revision that already stands.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import math
import re
import sqlite3
from typing import Any, Mapping

from .canonical_contract import (
    CONTRACT_VERSION,
    SCHEMA as CANONICAL_CONTRACT_SCHEMA,
    SETTLEMENT_SOURCE_TRUTH_LANE,
    SETTLEMENT_SOURCE_TRUTH_ROLE,
)
from .canonical_ingress import (
    AWC_DAILY_TRUTH_PROJECTION_SCHEMA,
    ingest_canonical_contract,
)
from .contracts import ContractError, canonical_json, content_sha256
from .settlement_time import settlement_day_window


SCHEMA = "settlement_source_truth_repair_v1"
EVIDENCE_SCHEMA = "settlement_source_truth_evidence_v1"
BUILDER_ID = "settlement_source.truth_repair.v1"
AUTHORITY_SCOPE = "FORMAL_NATIVE_ELIGIBLE"
SETTLEMENT_DEFINITION = "official_daily_tmax_c"
WU_FAMILY = "WU_DAILY_OBSERVATIONS"
NOAA_FAMILY = "NOAA_WRH_TIMESERIES"
BASIS_SOURCE_IN_BUCKET = "SOURCE_PAGE_IN_PAID_BUCKET"
BASIS_PAID_DEGREE = "PAID_WHOLE_DEGREE_SOURCE_REVISED_AFTER_FINALITY"
MAX_SOURCE_ROWS = 400

_WU_ROW_RE = re.compile(
    r"<tr>\s*<td>([^<]+)</td>\s*"
    r"<td class=\"temp(?: temp-high| temp-low)?\">\s*"
    r"(?:(-?\d+)\s*°([CF])|<span class=\"no-data\">-</span>)\s*</td>"
)
_WU_BODY_RE = re.compile(r"<airport-body[^>]*>")
_WU_ATTR_RE = re.compile(r'data-([a-z-]+)="([^"]*)"')


def paid_bucket_bounds(label: str) -> tuple[float, float, str]:
    """(low, high, unit) of a winning bucket label, whole degrees inclusive."""

    text = str(label or "").strip()
    match = re.fullmatch(r"(-?\d+)-(-?\d+)°([CF])", text)
    if match:
        return float(match.group(1)), float(match.group(2)), match.group(3)
    match = re.fullmatch(r"(-?\d+)°([CF]) or higher", text)
    if match:
        return float(match.group(1)), math.inf, match.group(2)
    match = re.fullmatch(r"(-?\d+)°([CF]) or below", text)
    if match:
        return -math.inf, float(match.group(1)), match.group(2)
    match = re.fullmatch(r"(-?\d+)°([CF])", text)
    if match:
        return float(match.group(1)), float(match.group(1)), match.group(2)
    raise ContractError("settlement_source_paid_bucket_label_invalid")


def whole_degree(value: float) -> int:
    """The market's whole degree: half up, the way the pages round."""

    return math.floor(float(value) + 0.5)


def _page_date(event_date: str) -> str:
    year, month, day = str(event_date).split("-")
    return f"{int(year)}-{int(month)}-{int(day)}"


def parse_wu_daily_history(
    html: str,
    *,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    unit: str,
) -> dict[str, Any]:
    """The daily observation table of one Wunderground history page."""

    body = _WU_BODY_RE.search(html)
    if body is None:
        raise ContractError("settlement_source_wu_station_binding_missing")
    attrs = dict(_WU_ATTR_RE.findall(body.group(0)))
    identity = {
        "icao_code": attrs.get("icao-code", ""),
        "time_zone": attrs.get("time-zone", ""),
        "mode": attrs.get("mode", ""),
        "date": attrs.get("date", ""),
    }
    if (
        identity["icao_code"] != station_id
        or identity["mode"] != "daily"
        or identity["date"] != _page_date(event_date)
    ):
        raise ContractError("settlement_source_wu_page_identity_mismatch")
    if identity["time_zone"] != settlement_timezone:
        raise ContractError("settlement_source_wu_timezone_mismatch")
    start = html.find("Daily Observations")
    table_start = html.find("<tbody", start) if start >= 0 else -1
    table_end = html.find("</tbody>", table_start) if table_start >= 0 else -1
    if start < 0 or table_start < 0 or table_end < 0:
        raise ContractError("settlement_source_wu_observation_table_missing")
    rows = _WU_ROW_RE.findall(html[table_start:table_end])
    if not rows or len(rows) > MAX_SOURCE_ROWS:
        raise ContractError("settlement_source_wu_observation_rows_invalid")
    readings = [
        [local_time.strip(), int(value)]
        for local_time, value, row_unit in rows
        if value
    ]
    units = {row_unit for _time, value, row_unit in rows if value}
    if not readings:
        raise ContractError("settlement_source_wu_temperature_missing")
    if units != {unit}:
        raise ContractError("settlement_source_wu_unit_mismatch")
    return {
        "identity": identity,
        "row_count": len(rows),
        "readings": readings,
        "value": float(max(value for _time, value in readings)),
    }


def read_noaa_summary(
    summary: Mapping[str, Any],
    *,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    unit: str,
) -> dict[str, Any]:
    """The page-side summary of one NOAA time-series response."""

    station = summary.get("station") or {}
    expected_units = {"C": "Celsius", "F": "Fahrenheit"}[unit]
    if (
        summary.get("site") != station_id
        or summary.get("day") != event_date
        or station.get("STID") != station_id
    ):
        raise ContractError("settlement_source_noaa_identity_mismatch")
    if station.get("TIMEZONE") != settlement_timezone:
        raise ContractError("settlement_source_noaa_timezone_mismatch")
    if summary.get("units_air_temp") != expected_units:
        raise ContractError("settlement_source_noaa_unit_mismatch")
    # A US Fahrenheit market settles on the page's hourly view; every other
    # NOAA market on the full series.
    view = "hourly_view" if unit == "F" else "all"
    block = (
        (summary.get("hourly_view") or {}).get("max")
        if view == "hourly_view"
        else summary.get("max_all")
    )
    if not isinstance(block, Mapping) or block.get("value") is None:
        raise ContractError("settlement_source_noaa_temperature_missing")
    return {
        "identity": {
            "stid": station.get("STID"),
            "time_zone": station.get("TIMEZONE"),
            "network": station.get("SHORTNAME"),
            "response_sha256": summary.get("response_sha256"),
            "response_bytes": summary.get("response_bytes"),
            "request_url": summary.get("request_url"),
        },
        "view": view,
        "row_count": int(summary.get("n_rows_day") or 0),
        "readings": [list(row) for row in block.get("rows") or []],
        "value": float(block["value"]),
    }


def decide(
    *, paid_bucket: str, unit: str, source_value: float
) -> tuple[str, float]:
    """(basis, value in the market's unit) -- or fail closed."""

    low, high, bucket_unit = paid_bucket_bounds(paid_bucket)
    if bucket_unit != unit:
        raise ContractError("settlement_source_paid_bucket_unit_mismatch")
    if low <= whole_degree(source_value) <= high:
        return BASIS_SOURCE_IN_BUCKET, float(source_value)
    if low == high:
        return BASIS_PAID_DEGREE, float(low)
    raise ContractError("settlement_source_truth_value_undetermined")


def to_celsius(value: float, unit: str) -> float:
    """Degrees Celsius, to a millionth: a whole Fahrenheit reading has no
    finite Celsius form, and the float noise of the conversion is not data."""

    if unit == "C":
        return float(value)
    return round((float(value) - 32.0) * 5.0 / 9.0, 6)


def _stamp(value: str) -> str:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ContractError("settlement_source_timestamp_timezone_required")
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _revisions(
    conn: sqlite3.Connection, *, station_id: str, event_date: str
) -> list[Mapping[str, Any]]:
    """Every projected revision of the station-day, newest first."""

    return [
        {
            "truth_id": str(row[0]),
            "revision_no": int(row[1]),
            "official_tmax_c": float(row[2]),
            "source_lane": str(row[3]),
            "source_role": str(row[4]),
            "qc_status": str(row[5]),
            "source_payload_sha256": str(row[6]),
        }
        for row in conn.execute(
            "SELECT truth_id,revision_no,official_tmax_c,source_lane,"
            "source_role,qc_status,source_payload_sha256 "
            "FROM calibration_station_daily_high_truth "
            "WHERE station_id=? AND local_date=? AND settlement_definition=? "
            "ORDER BY revision_no DESC",
            (station_id, event_date, SETTLEMENT_DEFINITION),
        )
    ]


def _next_revision(
    conn: sqlite3.Connection, *, station_id: str, event_date: str
) -> int:
    """Revision numbers are one sequence per station-day across every lane,
    in the projection and in the canonical facts alike."""

    projected = conn.execute(
        "SELECT MAX(revision_no) FROM calibration_station_daily_high_truth "
        "WHERE station_id=? AND local_date=? AND settlement_definition=?",
        (station_id, event_date, SETTLEMENT_DEFINITION),
    ).fetchone()[0]
    canonical = -1
    for (scope_key,) in conn.execute(
        "SELECT scope_key FROM canonical_ssot_fact "
        "WHERE fact_type='truth' AND scope_key LIKE ?",
        (f"{station_id}|{event_date}|%",),
    ):
        try:
            canonical = max(canonical, int(str(scope_key).rsplit("|", 1)[1]))
        except (IndexError, ValueError) as exc:
            raise ContractError("settlement_source_truth_scope_key_invalid") from exc
    return max(-1 if projected is None else int(projected), canonical) + 1


def build_evidence(case: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one case end to end and return its evidence; no database."""

    station_id = str(case["station_id"]).strip().upper()
    event_date = str(case["event_date"]).strip()
    zone = str(case["settlement_timezone"]).strip()
    market = case["market"]
    source = case["source"]
    unit = str(market["unit"])
    if unit not in {"C", "F"}:
        raise ContractError("settlement_source_market_unit_invalid")
    if market.get("closed") is not True:
        raise ContractError("settlement_source_market_not_resolved")
    if source.get("family") != market.get("family"):
        raise ContractError("settlement_source_family_mismatch")
    raw_sha = hashlib.sha256(source["raw_bytes"]).hexdigest()
    if raw_sha != str(source.get("raw_sha256") or ""):
        raise ContractError("settlement_source_raw_sha256_mismatch")
    if market["family"] == WU_FAMILY:
        parsed = parse_wu_daily_history(
            source["raw_bytes"].decode("utf-8", "replace"),
            station_id=station_id, event_date=event_date,
            settlement_timezone=zone, unit=unit,
        )
    elif market["family"] == NOAA_FAMILY:
        import json  # noqa: PLC0415 - a NOAA capture is a JSON summary

        parsed = read_noaa_summary(
            json.loads(source["raw_bytes"].decode("utf-8")),
            station_id=station_id, event_date=event_date,
            settlement_timezone=zone, unit=unit,
        )
    else:
        raise ContractError("settlement_source_family_unsupported")
    basis, value = decide(
        paid_bucket=str(market["winning_bucket"]), unit=unit,
        source_value=parsed["value"],
    )
    window = settlement_day_window(event_date, zone)
    return {
        "schema": EVIDENCE_SCHEMA,
        "charter": 225,
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": zone,
        "settlement_window": {
            "start_utc": _stamp(window["target_day_start_utc"]),
            "end_utc": _stamp(window["target_day_end_utc"]),
        },
        "market": {
            "slug": str(market["slug"]),
            "family": str(market["family"]),
            "unit": unit,
            "resolution_source": str(market["resolution_source"]),
            "winning_bucket": str(market["winning_bucket"]),
        },
        "source": {
            "capture_url": str(source["capture_url"]),
            "object_ref": str(source["object_ref"]),
            "raw_sha256": raw_sha,
            "raw_bytes": len(source["raw_bytes"]),
            "captured_at_utc": _stamp(source["captured_at_utc"]),
            "identity": parsed["identity"],
            "view": parsed.get("view", "daily_observations"),
            "row_count": parsed["row_count"],
            "readings": parsed["readings"][:MAX_SOURCE_ROWS],
            "value": parsed["value"],
            "value_whole_degree": whole_degree(parsed["value"]),
        },
        "decision": {
            "basis": basis,
            "value_market_unit": value,
            "official_tmax_c": to_celsius(value, unit),
        },
    }


def materialize_settlement_source_truth(
    target_conn: sqlite3.Connection,
    *,
    case: Mapping[str, Any],
    as_of_utc: str,
) -> dict[str, Any]:
    """Append one settlement-source revision, or say why not."""

    evidence = build_evidence(case)
    station_id = evidence["station_id"]
    event_date = evidence["event_date"]
    zone = evidence["settlement_timezone"]
    unit = evidence["market"]["unit"]
    as_of = _stamp(as_of_utc)
    day_end = evidence["settlement_window"]["end_utc"]
    if as_of < day_end:
        raise ContractError("settlement_source_truth_day_open")
    revisions = [
        row for row in _revisions(
            target_conn, station_id=station_id, event_date=event_date
        )
        if row["qc_status"] == "ACCEPTED"
    ]
    standing = revisions[0] if revisions else None
    base = {
        "schema": SCHEMA,
        "station_id": station_id,
        "event_date": event_date,
        "basis": evidence["decision"]["basis"],
        "official_tmax_c": evidence["decision"]["official_tmax_c"],
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    if standing is not None and standing["source_lane"] == SETTLEMENT_SOURCE_TRUTH_LANE:
        if standing["official_tmax_c"] == evidence["decision"]["official_tmax_c"]:
            return {**base, "state": "settlement_source_truth_stands", "wrote": False}
        raise ContractError("settlement_source_truth_conflicts_with_standing_repair")
    if standing is not None:
        low, high, _unit = paid_bucket_bounds(evidence["market"]["winning_bucket"])
        standing_value = (
            standing["official_tmax_c"] if unit == "C"
            else standing["official_tmax_c"] * 9.0 / 5.0 + 32.0
        )
        if low <= whole_degree(standing_value) <= high:
            return {**base, "state": "standing_truth_in_paid_bucket", "wrote": False}
        evidence = {
            **evidence,
            "supersedes": {
                "truth_id": standing["truth_id"],
                "revision_no": standing["revision_no"],
                "official_tmax_c": standing["official_tmax_c"],
                "source_lane": standing["source_lane"],
                "source_role": standing["source_role"],
            },
        }
    evidence_sha = content_sha256(evidence)
    revision_no = _next_revision(
        target_conn, station_id=station_id, event_date=event_date
    )
    truth_id = "truth_" + content_sha256({
        "station_id": station_id,
        "event_date": event_date,
        "truth_revision_no": revision_no,
        "truth_content_sha256": evidence_sha,
    })[:40]
    identity = {
        "truth_id": truth_id,
        "station_id": station_id,
        "event_date": event_date,
        "truth_revision_no": revision_no,
        "truth_value": evidence["decision"]["official_tmax_c"],
        "truth_content_sha256": evidence_sha,
        "canonical_committed_at_utc": as_of,
        "station_day_end_utc": day_end,
        "closed_day": True,
        "source_lane": SETTLEMENT_SOURCE_TRUTH_LANE,
        "source_role": SETTLEMENT_SOURCE_TRUTH_ROLE,
        "authority_scope": AUTHORITY_SCOPE,
        "settlement_timezone": zone,
    }
    canonical_truth = {**identity, "content_sha256": content_sha256(identity)}
    # The day's data was complete when the day closed; it was received when
    # the page was captured.
    published_at = day_end
    received_at = evidence["source"]["captured_at_utc"]
    legacy_identity = {
        "truth_id": truth_id,
        "station_id": station_id,
        "local_date": event_date,
        "settlement_timezone": zone,
        "official_tmax_c": canonical_truth["truth_value"],
        "source_lane": SETTLEMENT_SOURCE_TRUTH_LANE,
        "settlement_definition": SETTLEMENT_DEFINITION,
        "revision_no": revision_no,
        "published_at_utc": published_at,
        "received_at_utc": received_at,
        "canonical_committed_at_utc": as_of,
        "timestamps_complete": 1,
        "qc_status": "ACCEPTED",
        "source_role": SETTLEMENT_SOURCE_TRUTH_ROLE,
        "source_payload_sha256": evidence_sha,
        "observation_ids_json": canonical_json(evidence),
        "authority_scope": AUTHORITY_SCOPE,
    }
    projection_row = {
        **canonical_truth,
        "settlement_definition": SETTLEMENT_DEFINITION,
        "published_at_utc": published_at,
        "received_at_utc": received_at,
        "timestamps_complete": 1,
        "qc_status": "ACCEPTED",
        "observation_ids_json": legacy_identity["observation_ids_json"],
        "content_sha256": content_sha256(legacy_identity),
    }
    contract_identity = {
        "schema": CANONICAL_CONTRACT_SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "as_of_utc": as_of,
        "builder_id": BUILDER_ID,
        "model_facts": [],
        "ensemble_member_facts": [],
        "native_arrival_facts": [],
        "ensemble_member_receipt_manifest_facts": [],
        "ensemble_member_arrival_binding_facts": [],
        "truth_facts": [canonical_truth],
        "residual_facts": [],
        "activation_facts": [],
    }
    packet = {**contract_identity, "content_sha256": content_sha256(contract_identity)}
    caller_owns_transaction = target_conn.in_transaction
    ingress = ingest_canonical_contract(
        target_conn,
        packet,
        commit=True,
        finalize_transaction=not caller_owns_transaction,
        awc_daily_truth_projection={
            "schema": AWC_DAILY_TRUTH_PROJECTION_SCHEMA,
            "truth_rows": [projection_row],
        },
    )
    return {
        **base,
        "state": (
            "staged_in_caller_transaction" if caller_owns_transaction
            else "committed"
        ),
        "wrote": bool(
            ingress["inserted_count"]
            or ingress["awc_daily_truth_projection"]["inserted_count"]
        ),
        "truth_id": truth_id,
        "revision_no": revision_no,
        "supersedes": evidence.get("supersedes"),
        "truth_content_sha256": evidence_sha,
    }


__all__ = [
    "BASIS_PAID_DEGREE",
    "BASIS_SOURCE_IN_BUCKET",
    "NOAA_FAMILY",
    "WU_FAMILY",
    "build_evidence",
    "decide",
    "materialize_settlement_source_truth",
    "paid_bucket_bounds",
    "parse_wu_daily_history",
    "read_noaa_summary",
    "to_celsius",
    "whole_degree",
]
