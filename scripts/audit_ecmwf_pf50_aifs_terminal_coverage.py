#!/usr/bin/env python3
"""Adapt retained ECMWF PF50/AIFS audits into the shared coverage matrix.

This is a read-only adapter.  It only parses retained raw/audit evidence and
then emits rows exclusively through ``make_coverage_row`` and
``build_coverage_matrix``.  It never selects cross-model systems, creates an
activation, compiles P0, opens the SSOT, or contacts a network endpoint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.model_system_coverage import (  # noqa: E402
    LEADS,
    build_coverage_matrix,
    canonical_station_ids,
    make_coverage_row,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json_if_changed,
)


MAX_INPUT_BYTES = 64 * 1024 * 1024
EXPECTED_STATION_COUNT = expected_station_count()

PF50_PRODUCT = "ECMWF_IFS_ENS_OPEN_PF50"
AIFS_SINGLE_PRODUCT = "ECMWF_AIFS_SINGLE_0P25"
AIFS_REPUBLISHER_PRODUCT = "ECMWF_AIFS_OPEN_METEO_REPUBLISHER"
PF50_SCHEMA = "ecmwf_ifs_open_pf50_historical_multistation_members_v1"
PF50_MEMBER_CONTRACT = "OFFICIAL_OPEN_DATA_PERTURBED_1_TO_50_NO_CONTROL"

DEFAULT_PF50_ARCHIVE = (
    ROOT
    / "output/audits/native_ensemble_history/"
    "ECMWF_PF50_45station_20260731_official_18z_d2.json"
)
DEFAULT_PF50_OOS_LINKAGE = (
    ROOT
    / "output/audits/"
    "native_ensemble_activation_oos_linkage_20260803_ecmwf_noaa_1755.json"
)
DEFAULT_AIFS_READINESS = (
    ROOT
    / "output/audits/"
    "aifs_official_strict_oos_readiness_45station_20260802_lineage.json"
)
DEFAULT_AIFS_OOS = (
    ROOT / "output/audits/aifs_deterministic_strict_oos_20260802_latest.json"
)
DEFAULT_AIFS_REPUBLISHER_OOS = (
    ROOT
    / "output/audits/"
    "open_meteo_aifs_deterministic_historical_oos_20260802.json"
)


class TerminalCoverageError(ValueError):
    """The adapter input cannot represent an exact canonical coverage scope."""


def _text(value: object) -> str:
    return str(value or "").strip()


def _is_sha256(value: object) -> bool:
    text = _text(value).lower()
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _artifact_sha(value: Mapping[str, Any]) -> str:
    """Return explicit content identity, deriving only a local audit hash."""

    supplied = _text(value.get("content_sha256")).lower()
    return supplied if _is_sha256(supplied) else content_sha256(dict(value))


def _normalized_stations(station_ids: Iterable[object]) -> tuple[str, ...]:
    stations = tuple(sorted({_text(value).upper() for value in station_ids if _text(value)}))
    if len(stations) != EXPECTED_STATION_COUNT or set(stations) != set(canonical_station_ids()):
        raise TerminalCoverageError("terminal_coverage_exact_canonical_45_stations_required")
    return stations


def _run_cycle_from_init(value: object) -> str | None:
    text = _text(value)
    if len(text) >= 13 and text[10:11] == "T" and text[11:13].isdigit():
        return text[11:13] + "Z"
    return None


def _pf50_capture_index(
    archives: Iterable[Mapping[str, Any]],
    *,
    stations: Sequence[str],
) -> tuple[dict[tuple[str, str], dict[str, Any]], str]:
    """Parse exact PF50 raw/member facts without making any arrival claim."""

    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    archive_hashes: list[str] = []
    expected = set(stations)
    for archive in archives:
        archive_hashes.append(_artifact_sha(archive))
        if (
            archive.get("schema") != PF50_SCHEMA
            or archive.get("ensemble_system_id") != PF50_PRODUCT
            or _text(archive.get("archive_lead_bucket")) not in LEADS
        ):
            continue
        lead = _text(archive.get("archive_lead_bucket"))
        captures = archive.get("captures")
        if not isinstance(captures, list):
            continue
        archive_station_ids = {
            _text(item.get("station_id")).upper()
            for item in captures
            if isinstance(item, Mapping) and _text(item.get("station_id"))
        }
        scope_exact = (
            int(archive.get("station_count") or 0) == EXPECTED_STATION_COUNT
            and archive_station_ids == expected
            and int(archive.get("member_count") or 0) == 50
            and archive.get("member_availability_contract") == PF50_MEMBER_CONTRACT
        )
        for raw_capture in captures:
            capture = _mapping(raw_capture)
            station = _text(capture.get("station_id")).upper()
            if station not in expected:
                continue
            receipts = capture.get("field_receipts")
            members = capture.get("members")
            receipt_complete = (
                isinstance(receipts, list)
                and bool(receipts)
                and all(
                    isinstance(item, Mapping)
                    and _is_sha256(item.get("message_sha256"))
                    and _is_sha256(item.get("index_sha256"))
                    for item in receipts
                )
            )
            member_complete = (
                isinstance(members, list)
                and len(members) == 50
                and int(capture.get("member_count") or 0) == 50
                and capture.get("member_availability_contract") == PF50_MEMBER_CONTRACT
                and {
                    _text(item.get("member_id"))
                    for item in members
                    if isinstance(item, Mapping)
                }
                == {f"perturbed_{number:02d}" for number in range(1, 51)}
                and all(
                    isinstance(item, Mapping)
                    and item.get("member_role") == "PERTURBED"
                    for item in members
                )
            )
            receipt_sha = _text(capture.get("field_receipt_set_sha256")).lower()
            if not _is_sha256(receipt_sha):
                receipt_sha = content_sha256(list(receipts)) if isinstance(receipts, list) else ""
            fact = {
                "raw_receipt_complete": bool(scope_exact and receipt_complete),
                "member_contract_verified": bool(scope_exact and member_complete),
                "native_arrival_verified": bool(
                    capture.get("native_publication_at_known") is True
                    and capture.get("native_run_init_known") is True
                ),
                "raw_receipt_sha256": receipt_sha if receipt_complete else None,
            }
            key = (station, lead)
            previous = indexed.get(key)
            if previous is None:
                indexed[key] = fact
            else:
                # Additional retained input may only add directly evidenced
                # facts.  It cannot overwrite a receipt identity.
                indexed[key] = {
                    "raw_receipt_complete": bool(previous["raw_receipt_complete"])
                    or bool(fact["raw_receipt_complete"]),
                    "member_contract_verified": bool(previous["member_contract_verified"])
                    or bool(fact["member_contract_verified"]),
                    "native_arrival_verified": bool(previous["native_arrival_verified"])
                    or bool(fact["native_arrival_verified"]),
                    "raw_receipt_sha256": (
                        previous["raw_receipt_sha256"]
                        if previous["raw_receipt_sha256"] == fact["raw_receipt_sha256"]
                        else None
                    ),
                }
    return indexed, content_sha256(sorted(archive_hashes))


def _pf50_existing_oos_stations(audit: Mapping[str, Any]) -> set[str]:
    """Observe the existing pointer; never construct a cross-system choice."""

    pointer = _mapping(audit.get("current_pointer"))
    selected = {_text(value) for value in pointer.get("selected_system_ids_filter") or ()}
    if (
        pointer.get("state") != "BOUND"
        or PF50_PRODUCT not in selected
        or int(pointer.get("station_count") or 0) <= 0
    ):
        return set()
    return {
        _text(value).upper()
        for value in pointer.get("station_ids") or ()
        if _text(value)
    }


def _aifs_forward_ready(readiness: Mapping[str, Any], *, stations: Sequence[str]) -> bool:
    capture = _mapping(readiness.get("official_native_forward_capture"))
    decode = _mapping(readiness.get("decode_evidence"))
    decoded = {
        _text(value).upper()
        for value in decode.get("decoded_station_ids") or ()
        if _text(value)
    }
    return (
        capture.get("capture_state") == "OFFICIAL_NATIVE_FORWARD_COMPLETE"
        and int(capture.get("station_count") or 0) == EXPECTED_STATION_COUNT
        and int(capture.get("station_valid_arrival_count") or 0) > 0
        and int(capture.get("unique_raw_message_count") or 0) > 0
        and int(decode.get("complete_curve_count") or 0) == EXPECTED_STATION_COUNT
        and decoded == set(stations)
        and decode.get("coverage_scope")
        == "FULL_45_STATION_FORWARD_NOT_CLOSED_DAY_STRICT_OOS"
    )


def _aifs_forward_raw_receipt_sha(readiness: Mapping[str, Any]) -> str | None:
    """Bind native forward readiness to its retained official raw receipts."""

    capture = _mapping(readiness.get("official_native_forward_capture"))
    raw_message_sha256s = capture.get("raw_message_sha256s")
    if not isinstance(raw_message_sha256s, list) or not raw_message_sha256s:
        return None
    hashes = [_text(value).lower() for value in raw_message_sha256s]
    if (
        any(not _is_sha256(value) for value in hashes)
        or len(set(hashes)) != len(hashes)
        or int(capture.get("unique_raw_message_count") or 0) != len(hashes)
    ):
        return None
    return content_sha256(sorted(hashes))


def _aifs_scope_index(report: Mapping[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Parse exact historical scopes, never inferring them from a forward run."""

    result: dict[tuple[str, str], dict[str, Any]] = {}

    def record(item: Mapping[str, Any], *, candidate: bool) -> None:
        station = _text(item.get("station_id")).upper()
        lead = _text(item.get("lead_bucket"))
        if not station or lead not in LEADS:
            return
        current = result.setdefault(key := (station, lead), {
            "exact_lead_scope_observed": False,
            "strict_oos_pass": False,
            "strict_oos_shortfall": False,
            "run_cycle": None,
        })
        current["exact_lead_scope_observed"] = True
        current["run_cycle"] = current["run_cycle"] or _text(item.get("run_cycle")) or None
        if candidate:
            current["strict_oos_pass"] = current["strict_oos_pass"] or (
                _text(item.get("oos_state") or item.get("state")) == "STRICT_OOS_PASS"
            )
        else:
            current["strict_oos_shortfall"] = True

    strict_report = _mapping(report.get("strict_oos_report"))
    for raw in strict_report.get("candidates") or ():
        if isinstance(raw, Mapping):
            record(raw, candidate=True)
    for raw in report.get("scope_shortfalls") or ():
        if isinstance(raw, Mapping):
            record(raw, candidate=False)
    return result


def _aifs_formal_authority_present(readiness: Mapping[str, Any]) -> bool:
    closed = _mapping(readiness.get("closed_day_strict_oos"))
    artifacts = _mapping(closed.get("deterministic_artifacts"))
    binding = _mapping(readiness.get("provider_binding"))
    return (
        artifacts.get("state") == "FORMAL_DETERMINISTIC_PROVIDER_READY"
        and binding.get("constructible") is True
        and binding.get("formal_provider_eligible") is True
        and binding.get("formal_p0_role_eligible") is False
    )


def _republisher_candidate_index(report: Mapping[str, Any]) -> dict[tuple[str, str], dict[str, bool]]:
    result: dict[tuple[str, str], dict[str, bool]] = {}
    if report.get("source_role") != "aggregator_republisher":
        return result
    for raw in report.get("candidates") or ():
        candidate = _mapping(raw)
        station = _text(candidate.get("station_id")).upper()
        lead = _text(candidate.get("lead_bucket"))
        if not station or lead not in LEADS:
            continue
        closed_pass = (
            candidate.get("oos_event_temporal_scope") == "CLOSED_HISTORICAL_AWC_TRUTH"
            and int(candidate.get("train_n") or 0) >= 20
            and int(candidate.get("holdout_n") or 0) >= 8
            and _text(candidate.get("state")) == "PRODUCTION_NO_LIVE_OOS_PASS"
        )
        result[(station, lead)] = {
            "historical_candidate_observed": True,
            "strict_oos_pass": closed_pass,
        }
    return result


def _pf50_row(
    *,
    station: str,
    lead: str,
    as_of_utc: str,
    source_sha: str,
    oos_sha: str,
    fact: Mapping[str, Any],
    existing_oos: bool,
) -> dict[str, Any]:
    raw_complete = bool(fact.get("raw_receipt_complete"))
    member_complete = bool(fact.get("member_contract_verified"))
    arrival_verified = bool(fact.get("native_arrival_verified"))
    if not raw_complete:
        state, blocker = "BLOCKED", "DATA_MISSING"
    elif not member_complete or not arrival_verified:
        state, blocker = "DATA_READY", "LINEAGE_MISMATCH"
    elif not existing_oos:
        state, blocker = "DATA_READY", "OOS_SAMPLE_SHORT"
    else:
        # The linkage audit is a cross-system pointer and does not expose a
        # per-row 20+8 sample vector.  It remains observed evidence, never a
        # fabricated OOS_READY declaration in this single-product adapter.
        state, blocker = "DATA_READY", "OOS_SAMPLE_SHORT"
    return make_coverage_row(
        station_id=station,
        product_id=PF50_PRODUCT,
        provider_id="ECMWF_OPEN_DATA_S3",
        model_family="global_ensemble",
        independence_family_id="ECMWF",
        source_class="native_official",
        lead_bucket=lead,
        run_cycle="18Z",
        as_of_utc=as_of_utc,
        state=state,
        blockers=[blocker],
        evidence_role="historical_strict_oos",
        strict_asof=bool(existing_oos),
        closed_awc_truth=bool(existing_oos),
        train_count=0,
        holdout_count=0,
        source_evidence_sha256=source_sha,
        raw_receipt_sha256=fact.get("raw_receipt_sha256") if raw_complete else None,
        oos_evidence_sha256=oos_sha if existing_oos else None,
        single_system_artifact_sha256=None,
        native_arrival_indexed=arrival_verified,
        formal_live_authority=False,
        cross_selection_count=0,
        cross_activation_count=0,
        p0_count=0,
    )


def _aifs_single_row(
    *,
    station: str,
    lead: str,
    as_of_utc: str,
    readiness_sha: str,
    oos_sha: str,
    forward_ready: bool,
    formal_authority: bool,
    default_run_cycle: str | None,
    forward_raw_receipt_sha: str | None,
    scope: Mapping[str, Any],
) -> dict[str, Any]:
    exact_scope = bool(scope.get("exact_lead_scope_observed"))
    strict_pass = bool(scope.get("strict_oos_pass"))
    shortfall = bool(scope.get("strict_oos_shortfall"))
    run_cycle = scope.get("run_cycle") or default_run_cycle
    if not forward_ready:
        state, blocker, role = "BLOCKED", "DATA_MISSING", "observational_candidate_not_oos"
    elif not exact_scope:
        state, blocker, role = "CANDIDATE_ONLY", "DATA_MISSING", "observational_candidate_not_oos"
    elif strict_pass and formal_authority:
        state, blocker, role = "OOS_READY", None, "historical_strict_oos"
    elif strict_pass:
        state, blocker, role = "BLOCKED", "ACTIVATION_ROOT_MISSING", "historical_strict_oos"
    else:
        # A formal shortfall proves an exact historical scope exists, but not
        # enough closed strict-as-of cases to promote it.
        state, blocker, role = "DATA_READY", "OOS_SAMPLE_SHORT", "historical_strict_oos"
    return make_coverage_row(
        station_id=station,
        product_id=AIFS_SINGLE_PRODUCT,
        provider_id="ecmwf_aifs025_official",
        model_family="global_deterministic",
        independence_family_id="ECMWF",
        source_class="native_official",
        lead_bucket=lead,
        run_cycle=run_cycle,
        as_of_utc=as_of_utc,
        state=state,
        blockers=[] if blocker is None else [blocker],
        evidence_role=role,
        strict_asof=role == "historical_strict_oos",
        closed_awc_truth=role == "historical_strict_oos",
        train_count=20 if state == "OOS_READY" else 0,
        holdout_count=8 if state == "OOS_READY" else 0,
        source_evidence_sha256=oos_sha if exact_scope else readiness_sha,
        raw_receipt_sha256=forward_raw_receipt_sha if forward_ready else None,
        oos_evidence_sha256=oos_sha if exact_scope else None,
        single_system_artifact_sha256=readiness_sha if formal_authority else None,
        native_arrival_indexed=forward_ready,
        formal_live_authority=False,
        cross_selection_count=0,
        cross_activation_count=0,
        p0_count=0,
    )


def _republisher_row(
    *,
    station: str,
    lead: str,
    as_of_utc: str,
    source_sha: str,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    historical = bool(candidate.get("historical_candidate_observed"))
    strict_pass = bool(candidate.get("strict_oos_pass"))
    # ``run_cycle=None`` is intentional: Open-Meteo has no native cycle proof.
    return make_coverage_row(
        station_id=station,
        product_id=AIFS_REPUBLISHER_PRODUCT,
        provider_id="ecmwf_aifs_single_open_meteo",
        model_family="global_deterministic",
        independence_family_id="ECMWF",
        source_class="aggregator_republisher",
        lead_bucket=lead,
        run_cycle=None,
        as_of_utc=as_of_utc,
        state="CANDIDATE_ONLY",
        blockers=["DATA_MISSING"],
        evidence_role="historical_strict_oos" if historical else "observational_candidate_not_oos",
        strict_asof=historical and strict_pass,
        closed_awc_truth=historical and strict_pass,
        train_count=20 if historical and strict_pass else 0,
        holdout_count=8 if historical and strict_pass else 0,
        source_evidence_sha256=source_sha,
        raw_receipt_sha256=None,
        oos_evidence_sha256=source_sha if historical else None,
        single_system_artifact_sha256=None,
        native_arrival_indexed=False,
        formal_live_authority=False,
        cross_selection_count=0,
        cross_activation_count=0,
        p0_count=0,
    )


def build_terminal_matrix(
    *,
    pf50_archives: Iterable[Mapping[str, Any]],
    pf50_oos_linkage: Mapping[str, Any],
    aifs_readiness: Mapping[str, Any],
    aifs_oos: Mapping[str, Any],
    aifs_republisher_oos: Mapping[str, Any],
    as_of_utc: str,
    station_ids: Iterable[object] | None = None,
) -> dict[str, Any]:
    """Return only the shared canonical 3-product x 45 x 3 coverage matrix."""

    stations = _normalized_stations(canonical_station_ids() if station_ids is None else station_ids)
    pf50_facts, pf50_source_sha = _pf50_capture_index(pf50_archives, stations=stations)
    pf50_oos_stations = _pf50_existing_oos_stations(pf50_oos_linkage)
    pf50_oos_sha = _artifact_sha(pf50_oos_linkage)
    forward_raw_receipt_sha = _aifs_forward_raw_receipt_sha(aifs_readiness)
    forward_ready = (
        _aifs_forward_ready(aifs_readiness, stations=stations)
        and forward_raw_receipt_sha is not None
    )
    aifs_scopes = _aifs_scope_index(aifs_oos)
    aifs_readiness_sha = _artifact_sha(aifs_readiness)
    aifs_oos_sha = _artifact_sha(aifs_oos)
    aifs_authority = _aifs_formal_authority_present(aifs_readiness)
    aifs_default_cycle = _run_cycle_from_init(
        _mapping(aifs_readiness.get("official_native_forward_capture")).get(
            "run_initialization_utc"
        )
    )
    republisher_candidates = _republisher_candidate_index(aifs_republisher_oos)
    republisher_sha = _artifact_sha(aifs_republisher_oos)

    rows = []
    for station in stations:
        for lead in LEADS:
            rows.append(_pf50_row(
                station=station,
                lead=lead,
                as_of_utc=as_of_utc,
                source_sha=pf50_source_sha,
                oos_sha=pf50_oos_sha,
                fact=pf50_facts.get((station, lead), {}),
                existing_oos=lead == "d2" and station in pf50_oos_stations,
            ))
            rows.append(_aifs_single_row(
                station=station,
                lead=lead,
                as_of_utc=as_of_utc,
                readiness_sha=aifs_readiness_sha,
                oos_sha=aifs_oos_sha,
                forward_ready=forward_ready,
                formal_authority=aifs_authority,
                default_run_cycle=aifs_default_cycle,
                forward_raw_receipt_sha=forward_raw_receipt_sha,
                scope=aifs_scopes.get((station, lead), {}),
            ))
            rows.append(_republisher_row(
                station=station,
                lead=lead,
                as_of_utc=as_of_utc,
                source_sha=republisher_sha,
                candidate=republisher_candidates.get((station, lead), {}),
            ))
    return build_coverage_matrix(
        rows,
        expected_product_ids=(PF50_PRODUCT, AIFS_SINGLE_PRODUCT, AIFS_REPUBLISHER_PRODUCT),
        as_of_utc=as_of_utc,
    )


def _load(path: Path) -> Mapping[str, Any]:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file() or resolved.is_symlink() or resolved.stat().st_size > MAX_INPUT_BYTES:
        raise TerminalCoverageError(f"terminal_coverage_input_invalid:{resolved}")
    value = load_bounded_json(resolved, limit=MAX_INPUT_BYTES)
    if not isinstance(value, Mapping):
        raise TerminalCoverageError(f"terminal_coverage_mapping_required:{resolved}")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pf50-archive", type=Path, action="append", default=[])
    parser.add_argument("--pf50-oos-linkage", type=Path, default=DEFAULT_PF50_OOS_LINKAGE)
    parser.add_argument("--aifs-readiness", type=Path, default=DEFAULT_AIFS_READINESS)
    parser.add_argument("--aifs-oos", type=Path, default=DEFAULT_AIFS_OOS)
    parser.add_argument("--aifs-republisher-oos", type=Path, default=DEFAULT_AIFS_REPUBLISHER_OOS)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    archive_paths = list(args.pf50_archive) or [DEFAULT_PF50_ARCHIVE]
    resolved_archives = [path.expanduser().resolve(strict=True) for path in archive_paths]
    matrix = build_terminal_matrix(
        pf50_archives=[_load(path) for path in resolved_archives],
        pf50_oos_linkage=_load(args.pf50_oos_linkage),
        aifs_readiness=_load(args.aifs_readiness),
        aifs_oos=_load(args.aifs_oos),
        aifs_republisher_oos=_load(args.aifs_republisher_oos),
        as_of_utc=args.as_of_utc,
    )
    output = args.output.expanduser().resolve()
    wrote = write_bounded_json_if_changed(output, matrix)
    print(json.dumps({
        "output": str(output),
        "output_wrote": wrote,
        "scope_count": matrix["scope_count"],
        "state_counts": matrix["state_counts"],
        "cross_selection_count": matrix["cross_selection_count"],
        "cross_activation_count": matrix["cross_activation_count"],
        "p0_count": matrix["p0_count"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
