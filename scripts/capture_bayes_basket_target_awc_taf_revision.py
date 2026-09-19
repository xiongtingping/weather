#!/usr/bin/env python3
"""Capture one bounded official AWC TAF revision for P0-M materialization."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402
from scripts.capture_bayes_basket_target_awc_taf_calibration import (  # noqa: E402
    TAF_ENDPOINT,
    _event_window,
    _fetch_json,
    _iso,
    _parse_utc,
    _taf_row,
)


SCHEMA = "bayes_basket_awc_taf_revision_capture_v1"
FORWARD_SCHEMA = "bayes_basket_target_forward_taf_capture_v1"
INBOX_SCHEMA = "bayes_basket_target_taf_revision_inbox_v1"
# 45 stations x ~3 active event dates x ~6 TAF revisions each fits with slack;
# the old 256-slot global window held roughly 17 hours across 86 scopes, so
# every chain root scrolled out within its own event day.
MAX_INBOX_CAPTURES = 2048
# How far ahead of its nominal issue time a bulletin may reach AWC and still
# be captured (see ``_taf_row``).  Measured case KBKF 2026-09-18: 1m47s, and
# the strict order refused that bulletin on every round for seven hours.
PRE_ISSUE_TOLERANCE_SEC = 900.0


def capture_forward_awc_taf(
    *,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    timeout_sec: int = 12,
    response_cache: dict[str, object] | None = None,
) -> dict[str, object]:
    """Capture the currently visible official TAF with a local receipt clock.

    ``response_cache`` is one poll round's memory of what each station's
    request returned.  The request names the station, not the event day --
    every scope of one station is parsed out of the same response -- so a
    round that holds two or three of a station's days used to fetch the same
    bulletin two or three times, each a curl subprocess with retries.  With
    the cache the station is fetched once per round and its other scopes are
    parsed from those bytes, received at the moment they arrived: the
    capture's identity (URL, response sha, receipt clock) is the one a
    separate fetch a second later would have produced.  A station whose
    fetch failed fails its other scopes with the same error rather than
    asking again inside the round.
    """

    station = station_id.strip().upper()
    start, end = _event_window(event_date, settlement_timezone)
    url = TAF_ENDPOINT + "?" + urllib.parse.urlencode({
        "ids": station,
        "format": "json",
    })
    cached = None if response_cache is None else response_cache.get(url)
    if isinstance(cached, BaseException):
        raise cached
    if cached is None:
        try:
            rows, response_sha, response_bytes = _fetch_json(
                url, timeout_sec=timeout_sec
            )
        except Exception as exc:
            if response_cache is not None:
                response_cache[url] = exc
            raise
        received_at = datetime.now(timezone.utc)
        if response_cache is not None:
            response_cache[url] = (
                rows, response_sha, response_bytes, received_at
            )
    else:
        rows, response_sha, response_bytes, received_at = cached  # type: ignore[misc]
    try:
        taf, raw = _taf_row(
            rows,
            station_id=station,
            start=start,
            end=end,
            event_date=event_date,
            timezone_name=settlement_timezone,
            require_tx=False,
            pre_issue_tolerance_sec=PRE_ISSUE_TOLERANCE_SEC,
        )
    except ContractError as exc:
        # ``_taf_row`` is also the historical replay parser, where this
        # condition is intentionally named for historical availability.  The
        # forward capture has a distinct public contract: callers need one
        # stable reason code for any provider clock that cannot establish a
        # native forward arrival.  Do the mapping only at this boundary so the
        # historical parser's diagnostic remains precise.
        if str(exc) == "awc_taf_historical_available_before_issue":
            raise ContractError(
                "awc_forward_taf_provider_timestamp_order_invalid"
            ) from exc
        raise
    issued_at = _parse_utc(taf["issue_time_utc"])
    if issued_at > received_at:
        raise ContractError("awc_forward_taf_issue_after_local_receipt")
    provider_available_at = _parse_utc(taf["available_time_utc"])
    if not issued_at <= provider_available_at <= received_at:
        # AWC occasionally returns a current bulletin with a stale dbPopTime
        # from the preceding revision.  It is not safe to invent an earlier
        # native availability timestamp from that response; reject this
        # capture and let the station scope retry on the next rotation.
        raise ContractError(
            "awc_forward_taf_provider_timestamp_order_invalid"
        )
    valid_from = _parse_utc(raw.get("validTimeFrom"))
    valid_until = _parse_utc(raw.get("validTimeTo"))
    local_received = _iso(received_at)
    raw_sha = taf["raw_taf_sha256"]
    revision_identity = {
        "schema": "bayes_basket_forecast_revision_v1",
        "station_id": station,
        "event_date": event_date,
        "revision_kind": "TAF",
        "sequence": 1,
        "issued_at_utc": taf["issue_time_utc"],
        "received_at_utc": local_received,
        "valid_from_utc": _iso(valid_from),
        "valid_until_utc": _iso(valid_until),
        "source_object_sha256": raw_sha,
        "supersedes_revision_id": None,
    }
    revision_id = "taf-revision-" + content_sha256(revision_identity)[:32]
    identity = {
        "schema": FORWARD_SCHEMA,
        "source_class": "official_forward_native_receipt",
        "evidence_role": "observational_candidate_not_oos",
        "provider": "NOAA Aviation Weather Center",
        "station_id": station,
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "source_url": url,
        "response_sha256": response_sha,
        "response_bytes": response_bytes,
        "provider_available_at_utc": _iso(provider_available_at),
        "local_received_at_utc": local_received,
        "forecast_revision": {
            "revision_id": revision_id,
            **revision_identity,
        },
        "taf_fields": {
            "tx_c": taf["tx_c"],
            "cloud_cover_fraction": taf["cloud_cover_fraction"],
            "precipitation_probability": taf["precipitation_probability"],
            "wind_speed_mps": taf["wind_speed_mps"],
        },
        "raw_taf": taf["raw_taf"],
        "raw_taf_sha256": raw_sha,
        "boundary": {
            "native_local_receipt": True,
            "retrospective_replay_only": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def awc_newer_taf_stations(
    station_ids,
    *,
    held_issued_at: dict[str, str],
    already_forced: dict[str, str] | None = None,
    timeout_sec: int = 12,
    fetch=None,
) -> tuple[dict[str, str], dict[str, object]]:
    """Which stations AWC holds a bulletin for that is newer than ours.

    User, 2026-09-19 (「taf更新了，没有抓取吗？要提高轮询抓取，快速反应」): the
    rotation asks one station per request and reaches a station outside its
    priority classes only when the lap comes round -- LTAC's 22:40Z bulletin
    sat at AWC for over an hour.  One request for every station answers the
    only question that matters each round: who has something new.  Returns
    ``{station: newest AWC issue time}`` for those stations, skipping any
    issue time already forced once (a bulletin that covers no open scope --
    EGLC's three-hour evening TAF -- would otherwise be asked for every
    round), and a small receipt.  Never raises: a failed probe leaves the
    rotation exactly as it was.
    """

    stations = sorted({str(s).strip().upper() for s in station_ids if str(s).strip()})
    receipt: dict[str, object] = {"stations": len(stations)}
    if not stations:
        return {}, receipt
    url = TAF_ENDPOINT + "?" + urllib.parse.urlencode({
        "ids": ",".join(stations),
        "format": "json",
    })
    try:
        rows, _sha, response_bytes = (fetch or _fetch_json)(url, timeout_sec=timeout_sec)
    except Exception as exc:  # noqa: BLE001 -- the rotation proceeds without it
        receipt["state"] = f"probe_failed:{type(exc).__name__}"
        return {}, receipt
    newest: dict[str, datetime] = {}
    for row in rows or []:
        station = str((row or {}).get("icaoId") or "").strip().upper()
        try:
            issued = _parse_utc((row or {}).get("issueTime"))
        except (TypeError, ValueError, OverflowError, ContractError):
            continue
        if station in stations and (station not in newest or issued > newest[station]):
            newest[station] = issued
    forced = dict(already_forced or {})
    newer: dict[str, str] = {}
    for station, issued in newest.items():
        held = held_issued_at.get(station)
        try:
            held_at = _parse_utc(held) if held else None
        except (TypeError, ValueError, OverflowError, ContractError):
            held_at = None
        if held_at is not None and issued <= held_at:
            continue
        stamp = _iso(issued)
        if forced.get(station) == stamp:
            continue
        newer[station] = stamp
    receipt.update({"state": "ok", "response_bytes": response_bytes,
                    "awc_stations": len(newest), "newer": sorted(newer)})
    return newer, receipt


def refresh_forward_taf_inbox(
    *,
    captures: list[dict[str, object]],
    inbox_path: Path,
) -> dict[str, object]:
    """Append unique forward captures and retain only the bounded active tail."""

    existing: list[dict[str, object]] = []
    path = inbox_path.expanduser().resolve()
    if path.is_file():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema") != INBOX_SCHEMA:
            raise ContractError("forward_taf_inbox_schema_invalid")
        existing = [
            dict(item)
            for item in raw.get("captures", [])
            if isinstance(item, dict)
        ]
    # A resident may first observe an upstream AMD/COR after the original TAF
    # has already scrolled out of the local capture window.  The immutable
    # ForecastRevision relation must still start at a local TAF root; preserve
    # the bulletin's native marker separately and re-key the observed chain.
    by_scope: dict[tuple[str, str], list[dict[str, object]]] = {}
    for item in existing:
        revision = item.get("forecast_revision")
        if not isinstance(revision, dict):
            continue
        by_scope.setdefault(
            (
                str(item.get("station_id") or ""),
                str(item.get("event_date") or ""),
            ),
            [],
        ).append(item)
    repaired: dict[int, dict[str, object]] = {}
    for scope, group in by_scope.items():
        # Received order is the only ordering the chain contract can honour:
        # sequence numbers assigned before an eviction may repeat after one,
        # and sorting by them interleaves arrivals (FACT 2026-08-20 held
        # 1,2,3,3,3,3,4 with the newest arrival numbered 3).
        group.sort(
            key=lambda item: str(item.get("local_received_at_utc") or "")
        )
        sequences = [
            int((item.get("forecast_revision") or {}).get("sequence") or 0)
            for item in group
        ]
        first_revision = group[0].get("forecast_revision")
        # Re-key whenever the retained group is not already the valid chain
        # the validator demands: contiguous 1..N in received order with a TAF
        # root.  The previous guard skipped exactly the scrolled-root case
        # (first retained sequence != 1) its own comment said it repaired.
        if (
            isinstance(first_revision, dict)
            and sequences == list(range(1, len(group) + 1))
            and str(first_revision.get("revision_kind") or "") == "TAF"
        ):
            continue
        previous_id = None
        for index, item in enumerate(group, start=1):
            revision = dict(item["forecast_revision"])
            native_kind = str(
                item.get("native_revision_kind")
                or revision.get("revision_kind")
                or "TAF"
            ).upper()
            source_sha = str(
                revision.get("source_object_sha256") or ""
            )
            revision_id = "taf-revision-" + content_sha256({
                "station_id": scope[0],
                "event_date": scope[1],
                "source_object_sha256": source_sha,
                "observed_chain_root_normalized": True,
                "sequence": index,
            })[:32]
            revision.update({
                "revision_id": revision_id,
                "revision_kind": (
                    "TAF" if index == 1 else native_kind
                ),
                "sequence": index,
                "supersedes_revision_id": previous_id,
            })
            normalized = {
                key: value
                for key, value in item.items()
                if key != "content_sha256"
            }
            normalized["native_revision_kind"] = native_kind
            normalized["forecast_revision"] = revision
            repaired[id(item)] = {
                **normalized,
                "content_sha256": content_sha256(normalized),
            }
            previous_id = revision_id
    existing = [repaired.get(id(item), item) for item in existing]
    retained = list(existing)
    source_keys = {
        (
            str(item.get("station_id") or ""),
            str(item.get("event_date") or ""),
            str((item.get("forecast_revision") or {}).get(
                "source_object_sha256"
            ) or ""),
        )
        for item in existing
        if isinstance(item.get("forecast_revision"), dict)
    }
    for capture in sorted(
        captures,
        key=lambda item: str(item.get("local_received_at_utc") or ""),
    ):
        revision = capture.get("forecast_revision")
        if not isinstance(revision, dict):
            raise ContractError("forward_taf_capture_revision_missing")
        scope = (
            str(capture.get("station_id") or ""),
            str(capture.get("event_date") or ""),
        )
        source_sha = str(revision.get("source_object_sha256") or "")
        if (*scope, source_sha) in source_keys:
            continue
        predecessors = [
            item
            for item in retained
            if (
                str(item.get("station_id") or ""),
                str(item.get("event_date") or ""),
            ) == scope
            and isinstance(item.get("forecast_revision"), dict)
        ]
        predecessors.sort(
            key=lambda item: int(
                item["forecast_revision"].get("sequence") or 0
            )
        )
        sequence = len(predecessors) + 1
        previous_id = (
            str(predecessors[-1]["forecast_revision"]["revision_id"])
            if predecessors else None
        )
        raw_taf = str(capture.get("raw_taf") or "").upper()
        native_kind = (
            "COR" if " TAF COR " in f" {raw_taf} "
            else "AMD" if " TAF AMD " in f" {raw_taf} "
            else "TAF"
        )
        kind = "TAF" if sequence == 1 else native_kind
        normalized_revision = {
            **revision,
            "revision_id": "taf-revision-" + content_sha256({
                "station_id": scope[0],
                "event_date": scope[1],
                "source_object_sha256": source_sha,
            })[:32],
            "revision_kind": kind,
            "sequence": sequence,
            "supersedes_revision_id": previous_id,
        }
        normalized_identity = {
            key: value
            for key, value in capture.items()
            if key != "content_sha256"
        }
        normalized_identity["forecast_revision"] = normalized_revision
        normalized_identity["native_revision_kind"] = native_kind
        retained.append({
            **normalized_identity,
            "content_sha256": content_sha256(normalized_identity),
        })
        source_keys.add((*scope, source_sha))
    # Eviction granularity is the whole (station, event_date) scope, never a
    # single capture.  Trimming captures individually let a chain's root
    # scroll out mid-day, after which ``len(predecessors)+1`` renumbered new
    # arrivals backwards into already-persisted sequences.  A chain is either
    # fully present or fully absent; under pressure the scopes with the
    # oldest newest-arrival leave first.
    retained = sorted(
        retained,
        key=lambda item: str(item.get("local_received_at_utc") or ""),
    )
    if len(retained) > MAX_INBOX_CAPTURES:
        newest_by_scope: dict[tuple[str, str], str] = {}
        for item in retained:
            item_scope = (
                str(item.get("station_id") or ""),
                str(item.get("event_date") or ""),
            )
            received = str(item.get("local_received_at_utc") or "")
            if received > newest_by_scope.get(item_scope, ""):
                newest_by_scope[item_scope] = received
        for evict_scope, _newest in sorted(
            newest_by_scope.items(), key=lambda entry: entry[1]
        ):
            if len(retained) <= MAX_INBOX_CAPTURES:
                break
            retained = [
                item
                for item in retained
                if (
                    str(item.get("station_id") or ""),
                    str(item.get("event_date") or ""),
                ) != evict_scope
            ]
    identity = {
        "schema": INBOX_SCHEMA,
        "capture_count": len(retained),
        "captures": retained,
        "evidence_role": "observational_candidate_not_oos",
        "boundary": {
            "forward_only": True,
            "max_captures": MAX_INBOX_CAPTURES,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    result = {**identity, "content_sha256": content_sha256(identity)}
    write_bounded_json(path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station-id", required=True)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--settlement-timezone", required=True)
    parser.add_argument("--decision-at-utc", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    station = args.station_id.strip().upper()
    decision_at = _parse_utc(args.decision_at_utc)
    start, end = _event_window(args.event_date, args.settlement_timezone)
    url = TAF_ENDPOINT + "?" + urllib.parse.urlencode({
        "ids": station,
        "format": "json",
        "time": "issue",
        "date": _iso(decision_at),
    })
    rows, response_sha, response_bytes = _fetch_json(url, timeout_sec=30)
    taf, raw = _taf_row(
        rows,
        station_id=station,
        start=start,
        end=end,
        event_date=args.event_date,
        timezone_name=args.settlement_timezone,
    )
    if _parse_utc(taf["available_time_utc"]) > decision_at:
        raise ContractError("awc_taf_revision_not_available_asof")
    valid_from = _parse_utc(raw.get("validTimeFrom"))
    valid_until = _parse_utc(raw.get("validTimeTo"))
    raw_sha = taf["raw_taf_sha256"]
    revision_identity = {
        "schema": "bayes_basket_forecast_revision_v1",
        "station_id": station,
        "event_date": args.event_date,
        "revision_kind": "TAF",
        "sequence": 1,
        "issued_at_utc": taf["issue_time_utc"],
        "received_at_utc": taf["available_time_utc"],
        "valid_from_utc": _iso(valid_from),
        "valid_until_utc": _iso(valid_until),
        "source_object_sha256": raw_sha,
        "supersedes_revision_id": None,
    }
    revision_id = "taf-revision-" + content_sha256(revision_identity)[:32]
    revision = {"revision_id": revision_id, **revision_identity}
    identity = {
        "schema": SCHEMA,
        "provider": "NOAA Aviation Weather Center",
        "source_class": "official_date_addressed_retrospective_capture",
        "station_id": station,
        "event_date": args.event_date,
        "settlement_timezone": args.settlement_timezone,
        "decision_at_utc": _iso(decision_at),
        "source_url": url,
        "response_sha256": response_sha,
        "response_bytes": response_bytes,
        "forecast_revision": revision,
        "taf_fields": {
            "tx_c": taf["tx_c"],
            "cloud_cover_fraction": taf["cloud_cover_fraction"],
            "precipitation_probability": taf["precipitation_probability"],
            "wind_speed_mps": taf["wind_speed_mps"],
        },
        "raw_taf": taf["raw_taf"],
        "raw_taf_sha256": hashlib.sha256(
            taf["raw_taf"].encode("utf-8")
        ).hexdigest(),
        "boundary": {
            "native_local_receipt": False,
            "official_awc_db_population_time_preserved": True,
            "retrospective_replay_only": True,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    result = {**identity, "content_sha256": content_sha256(identity)}
    write_bounded_json(args.output.expanduser().resolve(), result)
    print(json.dumps({
        "revision_id": revision_id,
        "issued_at_utc": taf["issue_time_utc"],
        "available_time_utc": taf["available_time_utc"],
        "taf_fields": identity["taf_fields"],
        "output": str(args.output.expanduser().resolve()),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
