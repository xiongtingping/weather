#!/usr/bin/env python3
"""Capture one official GEFS+ECMWF d2 forward snapshot for the 45 stations.

This is a bounded no-live operator command.  It fetches each official global
member field once, retains only the minimal content-addressed receipts and
station-member values, and then delegates packet construction to
``build_official_native_forward_inbox.py``.  It neither opens SQLite nor
reads a probability, TAF, METAR, CLOB, or order service.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.ecmwf_ifs_archive import (  # noqa: E402
    capture_ecmwf_station_days,
)
from bayes_basket_target.market_registry import (  # noqa: E402
    load_market_registry_snapshot,
)
from bayes_basket_target.noaa_gefs_archive import (  # noqa: E402
    capture_gefs_station_days,
)
from bayes_basket_target.eccc_geps_archive import (  # noqa: E402
    capture_geps_station_days,
)
from bayes_basket_target.dwd_icon_eps_archive import (  # noqa: E402
    capture_icon_eps_station_days,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json_if_changed,
)
from bayes_basket_target.station_registry import (  # noqa: E402
    coordinate_registry_manifest,
    station_coordinates,
)


DEFAULT_RUNTIME_ROOT = ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
DEFAULT_REGISTRY = DEFAULT_RUNTIME_ROOT / "market_registry_forward.json"
DEFAULT_GEFS_OUTPUT = DEFAULT_RUNTIME_ROOT / "official_noaa_gefs_forward_archive.json"
DEFAULT_ECMWF_OUTPUT = DEFAULT_RUNTIME_ROOT / "official_ecmwf_ifs_forward_archive.json"
DEFAULT_INBOX_OUTPUT = DEFAULT_RUNTIME_ROOT / "official_native_ensemble_forward_inbox.json"
MAX_STATIONS = 45
MIN_TIMEOUT_SECONDS = 5.0
MAX_TIMEOUT_SECONDS = 120.0
# Forward P0 consumes a bounded current snapshot.  A retained archive older
# than this is historical evidence, not a current snapshot, and must be
# refreshed from the official provider (or remain blocked if unavailable).
FORWARD_REUSE_TTL_SECONDS = 6 * 60 * 60


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _parse_utc_timestamp(value: object) -> datetime | None:
    """Parse a receipt timestamp without accepting local/naive time."""

    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _load_retained_archive(
    path: Path,
    *,
    event_date: str,
    station_ids: Sequence[str],
    expected_schema: str,
    as_of_utc: str | None = None,
    max_reuse_age_seconds: float | None = None,
) -> dict[str, Any] | None:
    """Reuse one retained official archive instead of downloading it again.

    Reuse is conservative: malformed, tampered, wrong-date or wrong-scope
    files are treated as absent and the bounded capture path may refresh them.
    This keeps native precedence while preventing duplicate ECMWF/GEFS pulls.
    """

    raw_path = path.expanduser()
    if raw_path.is_symlink():
        return None
    candidate = raw_path.resolve()
    if not candidate.is_file() or candidate.is_symlink():
        return None
    try:
        payload = load_bounded_json(candidate)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    identity = {
        key: value for key, value in payload.items() if key != "content_sha256"
    }
    if (
        payload.get("schema") != expected_schema
        or payload.get("content_sha256") != content_sha256(identity)
        or str(payload.get("event_date") or "") != str(event_date)
        or payload.get("training_only") is not True
        or payload.get("holdout_eligible") is not False
    ):
        return None
    expected = tuple(sorted(str(value).strip().upper() for value in station_ids))
    actual = tuple(sorted(str(value).strip().upper() for value in payload.get("station_ids") or ()))
    captures = payload.get("captures")
    if (
        actual != expected
        or int(payload.get("station_count") or 0) != len(expected)
        or not isinstance(captures, list)
        or len(captures) != len(expected)
    ):
        return None
    # Forward reuse is freshness-gated.  Historical archives remain reusable
    # when callers omit the optional clock/TTL arguments (for example, an
    # offline unit probe), while the production forward path always supplies
    # the six-hour P0 TTL.  Missing or malformed query receipts fail closed.
    if max_reuse_age_seconds is not None:
        try:
            max_age = float(max_reuse_age_seconds)
        except (TypeError, ValueError):
            return None
        if max_age < 0:
            return None
        query_at = _parse_utc_timestamp(payload.get("archive_query_at_utc"))
        as_of = _parse_utc_timestamp(as_of_utc)
        if query_at is None or as_of is None or query_at > as_of:
            return None
        if (as_of - query_at).total_seconds() > max_age:
            return None
    return payload


def _canonical_station_ids() -> tuple[str, ...]:
    station_ids = tuple(
        str(value).strip().upper()
        for value in coordinate_registry_manifest()["station_ids"]
    )
    if len(station_ids) != MAX_STATIONS or len(set(station_ids)) != len(station_ids):
        raise ContractError("official_native_forward_canonical_station_set_invalid")
    return station_ids


def build_station_scopes(
    registry: dict[str, Any],
    *,
    event_date: str,
    station_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Resolve complete, source-eligible registry scopes before any fetch."""

    canonical = _canonical_station_ids()
    requested = canonical if station_ids is None else tuple(
        sorted({str(value).strip().upper() for value in station_ids if str(value).strip()})
    )
    if not requested or len(requested) > MAX_STATIONS:
        raise ContractError("official_native_forward_station_scope_invalid")
    unknown = sorted(set(requested) - set(canonical))
    if unknown:
        raise ContractError(
            "official_native_forward_station_not_canonical:" + ",".join(unknown)
        )
    rows = registry.get("rows")
    if not isinstance(rows, list):
        raise ContractError("official_native_forward_registry_rows_invalid")
    scopes: list[dict[str, Any]] = []
    for station_id in requested:
        matching = [
            dict(row)
            for row in rows
            if isinstance(row, dict)
            and str(row.get("station_id") or "").strip().upper() == station_id
            and str(row.get("event_date") or "") == event_date
            and int(row.get("market_active") or 0) == 1
            and int(row.get("source_eligible") or 0) == 1
        ]
        if not matching:
            raise ContractError(
                "official_native_forward_registry_scope_missing:"
                f"{station_id}:{event_date}"
            )
        timezones = {
            str(row.get("settlement_timezone") or "").strip()
            for row in matching
        }
        if len(timezones) != 1 or "" in timezones:
            raise ContractError(
                "official_native_forward_registry_timezone_invalid:"
                f"{station_id}:{event_date}"
            )
        outcomes = [str(row.get("outcome") or "").strip() for row in matching]
        if not outcomes or any(not outcome for outcome in outcomes) or len(outcomes) != len(set(outcomes)):
            raise ContractError(
                "official_native_forward_registry_outcome_invalid:"
                f"{station_id}:{event_date}"
            )
        coordinates = station_coordinates(station_id)
        scopes.append({
            "station_id": station_id,
            "settlement_timezone": next(iter(timezones)),
            "latitude": float(coordinates["latitude"]),
            "longitude": float(coordinates["longitude"]),
            "registry_rows": matching,
        })
    return scopes


def _verify_official_inbox(
    path: Path,
    *,
    event_date: str,
    station_ids: Sequence[str],
    require_eccc: bool = False,
) -> dict[str, Any]:
    inbox = load_bounded_json(path)
    identity = {key: value for key, value in inbox.items() if key != "content_sha256"}
    packets = inbox.get("packets")
    if (
        inbox.get("schema") != "native_ensemble_forward_inbox_v1"
        or not isinstance(packets, list)
        or str(inbox.get("content_sha256") or "") != content_sha256(identity)
    ):
        raise ContractError("official_native_forward_inbox_invalid")
    scopes = {
        (str(packet.get("station_id") or "").strip().upper(), str(packet.get("event_date") or ""))
        for packet in packets
        if isinstance(packet, dict)
    }
    expected = {(station, event_date) for station in station_ids}
    if scopes != expected:
        raise ContractError("official_native_forward_inbox_scope_mismatch")
    for packet in packets:
        if not isinstance(packet, dict) or packet.get("schema") != "official_native_ensemble_forward_packet_v1":
            raise ContractError("official_native_forward_inbox_packet_schema_invalid")
        families = {
            str(system.get("independence_family_id") or "")
            for system in packet.get("systems") or []
            if isinstance(system, dict)
        }
        if not {"ECMWF", "NOAA_GEFS"}.issubset(families):
            raise ContractError("official_native_forward_inbox_cross_family_missing")
        if require_eccc and "ECCC_GEM" not in families:
            raise ContractError("official_native_forward_inbox_eccc_missing")
    return inbox


def capture_official_native_forward(
    *,
    registry_path: Path,
    event_date: str,
    gefs_output: Path,
    ecmwf_output: Path,
    inbox_output: Path,
    timeout_seconds: float,
    station_ids: Sequence[str] | None = None,
    eccc_geps_archive: Path | None = None,
    eccc_station_ids: Sequence[str] | None = None,
    eccc_output: Path | None = None,
    icon_output: Path | None = None,
    # P0 follows the freshest run issued for the event date, so the caller
    # states which lead this pass is for.  Two is the historical default.
    lead_days: int = 2,
) -> dict[str, Any]:
    """Capture and materialize a formally admissible official-native inbox."""

    if not MIN_TIMEOUT_SECONDS <= float(timeout_seconds) <= MAX_TIMEOUT_SECONDS:
        raise ContractError("official_native_forward_timeout_invalid")
    registry = load_market_registry_snapshot(registry_path.expanduser().resolve(strict=True))
    scopes = build_station_scopes(registry, event_date=event_date, station_ids=station_ids)
    capture_at = _utc_now()
    requested_station_ids = [str(scope["station_id"]) for scope in scopes]
    gefs_path = gefs_output.expanduser().resolve()
    ecmwf_path = ecmwf_output.expanduser().resolve()
    gefs = _load_retained_archive(
        gefs_path,
        event_date=event_date,
        station_ids=requested_station_ids,
        expected_schema="noaa_gefs_official_historical_multistation_members_v2",
        as_of_utc=capture_at,
        max_reuse_age_seconds=FORWARD_REUSE_TTL_SECONDS,
    )
    ecmwf = _load_retained_archive(
        ecmwf_path,
        event_date=event_date,
        station_ids=requested_station_ids,
        expected_schema="ecmwf_ifs_open_pf50_historical_multistation_members_v1",
        as_of_utc=capture_at,
        max_reuse_age_seconds=FORWARD_REUSE_TTL_SECONDS,
    )
    # ECCC and ICON are captured actively here, not merely reused.  Leaving
    # them to a "supply an archive if you happen to have one" parameter meant
    # their history never grew: ECCC stalled at 2026-07-30 and ICON had none at
    # all, so neither could ever satisfy the exact holdout cohort that admits a
    # system to the cross-ensemble candidate set.  With only ECMWF and GEFS
    # qualifying, per-station model selection had nothing to choose between.
    eccc = None
    if eccc_output is not None:
        eccc = _load_retained_archive(
            eccc_output.expanduser().resolve(),
            event_date=event_date,
            station_ids=requested_station_ids,
            expected_schema=(
                "eccc_geps_official_historical_multistation_members_v1"
            ),
            as_of_utc=capture_at,
            max_reuse_age_seconds=FORWARD_REUSE_TTL_SECONDS,
        )
    icon = None
    if icon_output is not None:
        icon = _load_retained_archive(
            icon_output.expanduser().resolve(),
            event_date=event_date,
            station_ids=requested_station_ids,
            expected_schema=(
                "dwd_icon_eps_official_historical_multistation_members_v1"
            ),
            as_of_utc=capture_at,
            max_reuse_age_seconds=FORWARD_REUSE_TTL_SECONDS,
        )
    reused_gefs = gefs is not None
    reused_ecmwf = ecmwf is not None
    reused_eccc = eccc is not None
    reused_icon = icon is not None
    if gefs is None:
        assert_process_memory_budget()
        gefs = capture_gefs_station_days(
            event_date=event_date,
            station_scopes=scopes,
            archive_query_at_utc=capture_at,
            timeout_seconds=float(timeout_seconds),
            lead_days=int(lead_days),
        )
    if ecmwf is None:
        assert_process_memory_budget()
        ecmwf = capture_ecmwf_station_days(
            event_date=event_date,
            station_scopes=scopes,
            archive_query_at_utc=capture_at,
            timeout_seconds=float(timeout_seconds),
            lead_days=int(lead_days),
        )
    if eccc_output is not None and eccc is None:
        assert_process_memory_budget()
        eccc = capture_geps_station_days(
            event_date=event_date,
            station_scopes=scopes,
            archive_query_at_utc=capture_at,
            timeout_seconds=float(timeout_seconds),
            lead_days=int(lead_days),
        )
    if icon_output is not None and icon is None:
        assert_process_memory_budget()
        icon = capture_icon_eps_station_days(
            event_date=event_date,
            station_scopes=scopes,
            archive_query_at_utc=capture_at,
            timeout_seconds=float(timeout_seconds),
            lead_days=int(lead_days),
        )
    assert_process_memory_budget()
    inbox_path = inbox_output.expanduser().resolve()
    wrote_gefs = write_bounded_json_if_changed(gefs_path, gefs)
    wrote_ecmwf = write_bounded_json_if_changed(ecmwf_path, ecmwf)
    wrote_eccc = (
        write_bounded_json_if_changed(
            eccc_output.expanduser().resolve(), eccc
        )
        if eccc_output is not None and eccc is not None
        else False
    )
    wrote_icon = (
        write_bounded_json_if_changed(
            icon_output.expanduser().resolve(), icon
        )
        if icon_output is not None and icon is not None
        else False
    )
    command = [
        sys.executable,
        str(ROOT / "scripts" / "build_official_native_forward_inbox.py"),
        "--archive", str(gefs_path),
        "--archive", str(ecmwf_path),
        "--market-registry", str(registry_path.expanduser().resolve()),
        "--output", str(inbox_path),
    ]
    # An actively captured ECCC archive is preferred over a supplied one; both
    # carry the same GEPS native 00Z lineage, so the builder treats them alike.
    resolved_eccc = (
        eccc_output.expanduser().resolve()
        if eccc_output is not None and eccc is not None
        else (
            eccc_geps_archive.expanduser().resolve(strict=True)
            if eccc_geps_archive is not None
            else None
        )
    )
    if resolved_eccc is not None:
        command.extend(["--eccc-geps-archive", str(resolved_eccc)])
        for station_id in eccc_station_ids or ():
            command.extend(["--eccc-station-id", str(station_id)])
    if icon_output is not None and icon is not None:
        command.extend(["--archive", str(icon_output.expanduser().resolve())])
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    inbox = _verify_official_inbox(
        inbox_path,
        event_date=event_date,
        station_ids=[str(scope["station_id"]) for scope in scopes],
        require_eccc=eccc_geps_archive is not None,
    )
    return {
        "schema": "official_native_ensemble_forward_capture_v1",
        "state": "COMMITTED_NO_LIVE",
        "event_date": event_date,
        "captured_at_utc": capture_at,
        "station_count": len(scopes),
        "archive_paths": {
            "NOAA_GEFS_PDS_S3": str(gefs_path),
            "ECMWF_OPEN_DATA_S3": str(ecmwf_path),
            **({
                "ECCC_DD_WEATHER": str(
                    eccc_geps_archive.expanduser().resolve(strict=True)
                )
            } if eccc_geps_archive is not None else {}),
        },
        "archive_wrote": {
            "NOAA_GEFS_PDS_S3": wrote_gefs,
            "ECMWF_OPEN_DATA_S3": wrote_ecmwf,
            **({"ECCC_DD_WEATHER": False}
               if eccc_geps_archive is not None else {}),
        },
        "archive_reused_retained": {
            "NOAA_GEFS_PDS_S3": reused_gefs,
            "ECMWF_OPEN_DATA_S3": reused_ecmwf,
        },
        "inbox_path": str(inbox_path),
        "inbox_content_sha256": str(inbox["content_sha256"]),
        "inbox_packet_count": len(inbox["packets"]),
        "inbox_builder_stdout": completed.stdout.strip(),
        "production_no_live": True,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--lead-days", type=int, default=2, choices=[0, 1, 2])
    parser.add_argument("--market-registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--gefs-output", type=Path, default=DEFAULT_GEFS_OUTPUT)
    parser.add_argument("--ecmwf-output", type=Path, default=DEFAULT_ECMWF_OUTPUT)
    parser.add_argument("--inbox-output", type=Path, default=DEFAULT_INBOX_OUTPUT)
    parser.add_argument(
        "--eccc-output",
        type=Path,
        help=(
            "Actively capture official ECCC GEPS into this path instead of "
            "only reusing a supplied archive."
        ),
    )
    parser.add_argument(
        "--icon-output",
        type=Path,
        help=(
            "Actively capture official DWD ICON Global EPS into this path. "
            "DWD retains only the newest run, so this is forward-only: the "
            "run captured today becomes a d2 candidate two days later."
        ),
    )
    parser.add_argument("--station-id", action="append", default=[])
    parser.add_argument("--timeout-seconds", type=float, default=45.0)
    parser.add_argument(
        "--eccc-geps-archive",
        type=Path,
        help=(
            "Optional existing official ECCC GEPS forward archive; reuse it "
            "without a second capture."
        ),
    )
    parser.add_argument(
        "--eccc-station-id",
        action="append",
        default=[],
        help=(
            "Restrict reused ECCC GEPS to these stations; other stations "
            "remain ECMWF+GEFS pair packets."
        ),
    )
    args = parser.parse_args(argv)
    try:
        result = capture_official_native_forward(
            registry_path=args.market_registry,
            event_date=str(args.event_date),
            gefs_output=args.gefs_output,
            ecmwf_output=args.ecmwf_output,
            inbox_output=args.inbox_output,
            timeout_seconds=float(args.timeout_seconds),
            station_ids=args.station_id or None,
            eccc_geps_archive=args.eccc_geps_archive,
            eccc_output=args.eccc_output,
            icon_output=args.icon_output,
        lead_days=args.lead_days,
            eccc_station_ids=args.eccc_station_id,
        )
    except (ContractError, OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        print(json.dumps({
            "schema": "official_native_ensemble_forward_capture_v1",
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
            "production_no_live": True,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
        }, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
