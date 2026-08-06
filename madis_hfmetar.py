"""NOAA MADIS HF-METAR acquisition, ASOS/METAR matching, and P1 OOS basis.

MADIS HF-METAR is a five-minute dissemination product.  It is not a METAR
message.  NCEI/IEM ASOS ``tmpf`` is the ASOS temperature product published at
one-minute intervals; the exact scheduled-minute row is consumed directly and
must never be averaged a second time.  MADIS is the prior five-minute carrier.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from bayes_basket_target.contracts import content_sha256
from station_p1_preobservation_schedule import validate_observation_against_plan


MADIS_BASE = "https://madis-data.ncep.noaa.gov/madisPublic1/data/LDAD/hfmetar/netCDF"
SOURCE_LANE = "MADIS_HFMETAR_ASOS"
SOURCE_CLASS = "official_republisher"
UPSTREAM_AUTHORITY = "NOAA MADIS / ASOS HF-METAR"
MEASUREMENT_SEMANTICS = "ASOS_RUNNING_5_MINUTE_AVERAGE_DISSEMINATED_EVERY_5_MINUTES"
ONE_MINUTE_SEMANTICS = "ASOS_5_MINUTE_TEMPERATURE_PRODUCT_PUBLISHED_EACH_MINUTE_TMPF"
MAX_COMPRESSED_BYTES = 8 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 50_000
P1_ARTIFACT_SCHEMA = "asos_metar_exact_match_p1_oos_artifact_v1"


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _utc(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("madis_timestamp_timezone_required")
    return parsed.astimezone(timezone.utc)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def madis_object_url(hour_utc: datetime) -> str:
    hour = hour_utc.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return f"{MADIS_BASE}/{hour:%Y%m%d_%H00}.gz"


def fetch_madis_hfmetar_object(hour_utc: datetime, *, timeout: float = 30.0) -> dict[str, Any]:
    """Fetch one content-addressed hourly MADIS object under a wire-size cap."""

    url = madis_object_url(hour_utc)
    seconds = max(1, int(math.ceil(timeout)))
    started = datetime.now(timezone.utc)
    result = subprocess.run(
        [
            "/usr/bin/curl", "-fsSL", "--compressed", "--proto", "=https",
            "--tlsv1.2", "--connect-timeout", str(min(8, seconds)), "--max-time",
            str(seconds), "--retry", "1", "--max-filesize", str(MAX_COMPRESSED_BYTES),
            "-H", "Accept: application/octet-stream", "-H",
            "User-Agent: weather-madis-hfmetar/1.0", url,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=seconds + 5,
        check=False,
    )
    completed = datetime.now(timezone.utc)
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError(f"madis_hfmetar_fetch_failed:curl_exit_{result.returncode}")
    payload = bytes(result.stdout)
    if len(payload) > MAX_COMPRESSED_BYTES:
        raise RuntimeError("madis_hfmetar_compressed_limit_exceeded")
    identity = {
        "source_lane": SOURCE_LANE,
        "source_class": SOURCE_CLASS,
        "upstream_authority": UPSTREAM_AUTHORITY,
        "object_url": url,
        "object_hour_utc": _stamp(hour_utc.replace(minute=0, second=0, microsecond=0)),
        "compressed_sha256": _sha(payload),
        "compressed_bytes": len(payload),
        "fetch_started_at_utc": _stamp(started),
        "fetch_completed_at_utc": _stamp(completed),
    }
    return {**identity, "receipt_sha256": content_sha256(identity), "payload": payload}


def _chars(value: Any) -> str:
    try:
        return b"".join(value.tolist()).decode("ascii", "ignore").strip("\x00 ")
    except Exception:
        return ""


def decode_madis_hfmetar_object(
    payload: bytes,
    *,
    wanted_station_ids: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Decode only the exact variables required by the ASOS P1 lane."""

    try:
        from scipy.io import netcdf_file
    except ImportError as exc:
        raise RuntimeError("madis_hfmetar_scipy_required") from exc
    wanted = {str(value).strip().upper() for value in wanted_station_ids if str(value).strip()}
    expanded = 0
    with gzip.GzipFile(fileobj=__import__("io").BytesIO(payload), mode="rb") as source:
        with tempfile.NamedTemporaryFile() as target:
            while True:
                chunk = source.read(64 * 1024)
                if not chunk:
                    break
                expanded += len(chunk)
                if expanded > MAX_DECOMPRESSED_BYTES:
                    raise RuntimeError("madis_hfmetar_decompressed_limit_exceeded")
                target.write(chunk)
            target.flush()
            dataset = netcdf_file(target.name, "r", mmap=False)
            try:
                count = int(dataset.variables["observationTime"].shape[0])
                if count > MAX_RECORDS:
                    raise RuntimeError("madis_hfmetar_record_limit_exceeded")
                rows: list[dict[str, Any]] = []
                for index in range(count):
                    station = _chars(dataset.variables["stationId"][index]).upper()
                    if wanted and station not in wanted:
                        continue
                    provider = _chars(dataset.variables["providerId"][index]).upper()
                    station_type = _chars(dataset.variables["stationType"][index]).upper()
                    data_provider = _chars(dataset.variables["dataProvider"][index]).upper()
                    temperature_k = float(dataset.variables["temperature"][index])
                    observed = datetime.fromtimestamp(
                        float(dataset.variables["observationTime"][index]), timezone.utc
                    )
                    qc_summary = _chars(dataset.variables["temperatureDD"][index]).upper()
                    sensor_status = int(dataset.variables["temperatureStatus"][index])
                    if (
                        not station
                        or not math.isfinite(temperature_k)
                        or not 173.15 <= temperature_k <= 343.15
                    ):
                        continue
                    raw_message = _chars(dataset.variables["rawMessage"][index])
                    identity = {
                        "station_id": station,
                        "provider_station_id": provider,
                        "observed_at_utc": _stamp(observed),
                        "temperature_c": round(temperature_k - 273.15, 6),
                        "temperature_qc_summary": qc_summary,
                        "temperature_sensor_status": sensor_status,
                        "station_type": station_type,
                        "data_provider": data_provider,
                        "measurement_semantics": MEASUREMENT_SEMANTICS,
                        "raw_message_sha256": _sha(raw_message.encode("utf-8")),
                    }
                    rows.append({
                        **identity,
                        "qc_eligible": (
                            station_type == "HF-METAR"
                            and data_provider == "HF-METAR"
                            and sensor_status == 0
                            and qc_summary in {"", "Z"}
                        ),
                        "content_sha256": content_sha256(identity),
                    })
                return rows
            finally:
                dataset.close()


@dataclass(frozen=True)
class AsosTemperatureProduct:
    observed_at_utc: datetime
    temperature_c: float
    observation_sha256: str


def exact_asos_temperature_product(
    minute_rows: Sequence[AsosTemperatureProduct],
    *,
    at_utc: datetime,
) -> dict[str, Any] | None:
    """Return only the ASOS product published at the frozen exact minute."""

    rows = {
        row.observed_at_utc.astimezone(timezone.utc).replace(second=0, microsecond=0): row
        for row in minute_rows
    }
    target = at_utc.astimezone(timezone.utc).replace(second=0, microsecond=0)
    row = rows.get(target)
    if row is None:
        return None
    identity = {
        "published_at_utc": _stamp(target),
        "temperature_c": row.temperature_c,
        "observation_sha256": row.observation_sha256,
        "measurement_semantics": ONE_MINUTE_SEMANTICS,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def match_metar_to_asos(
    metar: Mapping[str, Any],
    minute_rows: Sequence[AsosTemperatureProduct],
    hf_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Bind METAR obsTime to exact historical ASOS and nearest prior MADIS cycle."""

    station = str(metar.get("station_id") or "").upper()
    observed = _utc(metar["observed_at_utc"])
    exact = exact_asos_temperature_product(minute_rows, at_utc=observed)
    eligible_hf = [
        row for row in hf_rows
        if str(row.get("station_id") or "").upper() == station
        and row.get("qc_eligible") is True
        and _utc(row["observed_at_utc"]) <= observed
        and observed - _utc(row["observed_at_utc"]) <= timedelta(minutes=5)
    ]
    hf = max(eligible_hf, key=lambda row: str(row["observed_at_utc"]), default=None)
    identity = {
        "station_id": station,
        "metar_observation_id": str(metar["observation_id"]),
        "metar_observed_at_utc": _stamp(observed),
        "metar_temperature_c": float(metar["reported_temperature_c"]),
        "metar_raw_sha256": str(metar["raw_sha256"]),
        "exact_asos_temperature_product": exact,
        "prior_madis_hf_cycle": None if hf is None else {
            "observed_at_utc": hf["observed_at_utc"],
            "temperature_c": hf["temperature_c"],
            "content_sha256": hf["content_sha256"],
            "age_seconds": int((observed - _utc(hf["observed_at_utc"])).total_seconds()),
        },
        "match_state": "EXACT_ONE_MINUTE_MATCH" if exact else (
            "MADIS_PRIOR_5MIN_APPROXIMATION" if hf else "ASOS_MATCH_MISSING"
        ),
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def build_asos_p1_oos_artifact(
    matches: Sequence[Mapping[str, Any]],
    *,
    train_count: int = 20,
    holdout_count: int = 8,
) -> dict[str, Any]:
    """Freeze chronological exact matches into an independent P1 adapter artifact."""

    stations = {str(row.get("station_id") or "").upper() for row in matches}
    stations.discard("")
    if len(stations) != 1:
        raise ValueError("asos_p1_artifact_single_station_required")
    station_id = next(iter(stations))
    exact = sorted(
        [row for row in matches if row.get("match_state") == "EXACT_ONE_MINUTE_MATCH"],
        key=lambda row: (str(row["metar_observed_at_utc"]), str(row["content_sha256"])),
    )
    required = int(train_count) + int(holdout_count)
    if len(exact) < required:
        identity = {
            "schema": P1_ARTIFACT_SCHEMA,
            "station_id": station_id,
            "state": "BLOCKED",
            "failure_code": "OOS_SAMPLE_SHORT",
            "exact_match_count": len(exact),
            "required_exact_match_count": required,
            "train_count": int(train_count),
            "holdout_count": int(holdout_count),
            "enters_p1": False,
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        }
        return {**identity, "content_sha256": content_sha256(identity)}
    chosen = exact[-required:]
    train, holdout = chosen[:train_count], chosen[train_count:]

    def residual(row: Mapping[str, Any]) -> float:
        derived = float(row["exact_asos_temperature_product"]["temperature_c"])
        return float(row["metar_temperature_c"]) - derived

    train_bias = sum(residual(row) for row in train) / len(train)
    holdout_errors = [residual(row) - train_bias for row in holdout]
    mae = sum(abs(value) for value in holdout_errors) / len(holdout_errors)
    identity = {
        "schema": P1_ARTIFACT_SCHEMA,
        "station_id": station_id,
        "state": "OOS_READY",
        "source_lane": SOURCE_LANE,
        "measurement_semantics": "ASOS_EXACT_5MIN_TO_METAR_TEMPERATURE_ADAPTER",
        "train_match_sha256s": [str(row["content_sha256"]) for row in train],
        "holdout_match_sha256s": [str(row["content_sha256"]) for row in holdout],
        "train_bias_c": train_bias,
        "holdout_mae_c": mae,
        "train_count": len(train),
        "holdout_count": len(holdout),
        "enters_p1": True,
        "formal_live_authority": False,
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def ingest_madis_hfmetar_object(
    conn: sqlite3.Connection,
    receipt: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    retained_path: str,
    committed_at_utc: str,
) -> dict[str, Any]:
    """Append one retained MADIS object and decoded observations."""

    compressed_sha = str(receipt.get("compressed_sha256") or "")
    if len(compressed_sha) != 64 or receipt.get("source_lane") != SOURCE_LANE:
        raise ValueError("madis_hfmetar_receipt_invalid")
    conn.execute(
        """INSERT OR IGNORE INTO reported_trajectory_madis_hfmetar_objects(
             compressed_sha256,object_hour_utc,object_url,compressed_bytes,retained_path,
             source_lane,source_class,receipt_sha256,fetch_started_at_utc,fetch_completed_at_utc
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            compressed_sha, receipt["object_hour_utc"], receipt["object_url"],
            int(receipt["compressed_bytes"]), retained_path, SOURCE_LANE, SOURCE_CLASS,
            receipt["receipt_sha256"], receipt["fetch_started_at_utc"],
            receipt["fetch_completed_at_utc"],
        ),
    )
    inserted = 0
    for row in rows:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO reported_trajectory_madis_hfmetar_observations(
                 content_sha256,compressed_sha256,station_id,provider_station_id,
                 observed_at_utc,temperature_c,temperature_qc_summary,
                 temperature_sensor_status,station_type,data_provider,measurement_semantics,
                 raw_message_sha256,qc_eligible,canonical_committed_at_utc
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["content_sha256"], compressed_sha, row["station_id"],
                row["provider_station_id"], row["observed_at_utc"], row["temperature_c"],
                row["temperature_qc_summary"], row["temperature_sensor_status"],
                row["station_type"], row["data_provider"], row["measurement_semantics"],
                row["raw_message_sha256"], int(bool(row["qc_eligible"])), committed_at_utc,
            ),
        )
        inserted += int(cursor.rowcount == 1)
    return {"compressed_sha256": compressed_sha, "inserted_observation_count": inserted}


def ingest_asos_metar_matches_and_artifact(
    conn: sqlite3.Connection,
    matches: Sequence[Mapping[str, Any]],
    artifact: Mapping[str, Any],
    *,
    committed_at_utc: str,
) -> dict[str, Any]:
    inserted = 0
    for match in matches:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO reported_trajectory_asos_metar_match_facts(
                 content_sha256,station_id,metar_observation_id,metar_observed_at_utc,
                 match_state,fact_json,canonical_committed_at_utc
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                match["content_sha256"], match["station_id"], match["metar_observation_id"],
                match["metar_observed_at_utc"], match["match_state"],
                json.dumps(match, sort_keys=True, separators=(",", ":")), committed_at_utc,
            ),
        )
        inserted += int(cursor.rowcount == 1)
    conn.execute(
        """INSERT OR IGNORE INTO reported_trajectory_asos_p1_oos_artifacts(
             content_sha256,station_id,state,authority_scope,train_count,holdout_count,
             artifact_json,canonical_committed_at_utc
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            artifact["content_sha256"], artifact["station_id"], artifact["state"],
            artifact["authority_scope"], artifact["train_count"], artifact["holdout_count"],
            json.dumps(artifact, sort_keys=True, separators=(",", ":")), committed_at_utc,
        ),
    )
    return {"inserted_match_count": inserted, "artifact_content_sha256": artifact["content_sha256"]}


def build_asos_p1_observation(
    source_row: Mapping[str, Any],
    artifact: Mapping[str, Any],
    *,
    source_product: str,
    schedule: Mapping[str, Any],
    planned_metar_observation_at_utc: str,
    event_date: str,
    settlement_timezone: str,
    canonical_committed_at_utc: str,
) -> dict[str, Any]:
    """Adapt an OOS-governed ASOS row to the existing canonical P1 compiler."""

    if (
        artifact.get("schema") != P1_ARTIFACT_SCHEMA
        or artifact.get("state") != "OOS_READY"
        or artifact.get("enters_p1") is not True
        or artifact.get("authority_scope") != "PRODUCTION_NO_LIVE_ONLY"
        or str(artifact.get("station_id") or "").upper()
        != str(source_row.get("station_id") or "").upper()
    ):
        raise ValueError("asos_p1_oos_artifact_not_eligible")
    if source_row.get("qc_eligible") is not True:
        raise ValueError("asos_p1_observation_qc_ineligible")
    if source_product not in {"EXACT_ASOS_1MIN", "MADIS_HFMETAR_5MIN"}:
        raise ValueError("asos_p1_source_product_invalid")
    observed = _utc(source_row["observed_at_utc"])
    committed = _utc(canonical_committed_at_utc)
    if committed < observed:
        raise ValueError("asos_p1_observation_commit_before_observation")
    schedule_binding = validate_observation_against_plan(
        schedule,
        station_id=str(source_row["station_id"]),
        source_product=source_product,
        planned_metar_observation_at_utc=planned_metar_observation_at_utc,
        observed_at_utc=_stamp(observed),
    )
    lane, report_type, semantics = (
        ("asos_1min", "ASOS_1MIN", ONE_MINUTE_SEMANTICS)
        if source_product == "EXACT_ASOS_1MIN"
        else ("madis_hfmetar", "ASOS_HF", MEASUREMENT_SEMANTICS)
    )
    raw_sha256 = str(source_row.get("raw_sha256") or "")
    if len(raw_sha256) != 64:
        raise ValueError("asos_p1_raw_sha256_invalid")
    identity = {
        "station_id": str(source_row["station_id"]).upper(),
        "event_date_local": event_date,
        "settlement_timezone": settlement_timezone,
        "source_lane": lane,
        "report_type": report_type,
        "valid_at_utc": _stamp(observed),
        "canonical_committed_at_utc": _stamp(committed),
        "reported_temperature_c": float(source_row["temperature_c"]),
        "raw_sha256": raw_sha256,
        "qc_status": "ACCEPTED",
        "evidence_role": "FORMAL",
        "correction_suspected": False,
        "raw_text": "",
        "measurement_semantics": semantics,
        "asos_p1_oos_artifact_content_sha256": str(artifact["content_sha256"]),
        "planned_metar_observation_at_utc": schedule_binding["planned_metar_observation_at_utc"],
        "schedule_content_sha256": schedule_binding["schedule_content_sha256"],
    }
    return {
        **identity,
        "observation_id": "asos_p1_" + content_sha256(identity)[:32],
    }


__all__ = [
    "AsosTemperatureProduct", "build_asos_p1_oos_artifact", "decode_madis_hfmetar_object",
    "fetch_madis_hfmetar_object", "madis_object_url", "match_metar_to_asos",
    "exact_asos_temperature_product", "ingest_madis_hfmetar_object",
    "ingest_asos_metar_matches_and_artifact",
    "build_asos_p1_observation",
]
