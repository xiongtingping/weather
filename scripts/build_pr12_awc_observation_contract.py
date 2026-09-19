#!/usr/bin/env python3
"""Build PR12 AWC evidence, canonical facts and terminal gaps without publishing."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.awc_observation_builder import (  # noqa: E402
    build_awc_observation_contract,
    export_awc_ingress_evidence_v4,
)
from bayes_basket_target.awc_observation_contract import (  # noqa: E402
    CanonicalAWCObservationContract,
)
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.station_registry import (  # noqa: E402
    coordinate_registry_manifest,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    write_bounded_json_if_changed,
)
from bayes_basket_target.pr12_authority_scope_projection import (  # noqa: E402
    validate_pr12_authority_scope_projection,
)


def _station_registry(target_db: Path) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    with sqlite3.connect(f"file:{target_db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT station_id,settlement_definition,content_sha256 "
            "FROM calibration_station_registry ORDER BY station_id"
        ).fetchall()
    for raw in rows:
        record = dict(raw)
        station = str(record["station_id"]).strip().upper()
        prior = result.get(station)
        if prior is not None:
            raise ContractError(f"pr12_station_registry_duplicate:{station}")
        record["station_id"] = station
        result[station] = record
    # The guard's claim is "the registry carries the fleet", and the count
    # was a proxy for it that broke the moment the registry carried one
    # row more.  Three onboarding rows for stations not yet in the fleet
    # (LTFM/UUWW/LLBG, written 2026-08-28 07:56Z, append-only so not
    # removable) took the table to 48, and this sweep failed in under a
    # second every ten minutes for the next ten hours -- the observation
    # projection stopped and P1 packets could not close, all while the
    # fleet's own 45 rows sat in the table intact.  Check membership, not
    # count: every fleet station must have exactly one row, and rows for
    # stations outside the fleet are staged onboarding, ignored here.
    fleet = {
        str(value).upper()
        for value in coordinate_registry_manifest()["station_ids"]
    }
    missing = sorted(fleet - set(result))
    if missing:
        raise ContractError(
            "pr12_station_registry_missing_fleet_station:"
            + ",".join(missing[:5])
        )
    return {
        station: record
        for station, record in result.items()
        if station in fleet
    }


def _attach_stored_revision_facts(
    target_db: Path, evidence: dict[str, object], projection_sha: str
) -> dict[str, object]:
    """Carry the stored revision of every report in the window into the packet.

    The builder never opens a database, so what is already on file reaches
    it the same way the archive does: as sealed evidence.  With it the build
    can re-emit an unchanged report as its stored revision and re-issue a
    report a late insertion moved as the next revision of its stored fact
    (see ``_resolve_against_stored_revision``); without it every such day
    was quarantined whole and its later reports never identified -- SBGR
    2026-09-03 lost every P1 revision hash from 12:34Z, when a 10:30Z SPECI
    landed after the day had been identified through 12:00Z.
    """

    stations = {
        str(row.get("station_id") or "").upper() for row in evidence.get("rows", [])
    }
    days = {str(row.get("event_date") or "") for row in evidence.get("rows", [])}
    stored: list[dict[str, object]] = []
    if stations and days:
        # Narrow on the scope key before touching a fact body.  The key is
        # station|observation|revision|observed_at|committed_at|projection
        # (CanonicalAWCObservationRevisionFact.scope_key) and lives in the
        # (fact_type, scope_key) index, so this reads the index, not 50k
        # bodies through json_extract (4.1s of a 19s pass, 2026-09-19).  A
        # local day starts at most 14h before its UTC date, so a fact observed
        # before the day ahead of the earliest day asked for is none of them.
        # A key that does not parse is fetched and judged on its body.
        try:
            floor = (
                min(date.fromisoformat(day) for day in days) - timedelta(days=1)
            ).isoformat()
        except ValueError:
            floor = ""
        sha = str(projection_sha)
        with sqlite3.connect(f"file:{target_db}?mode=ro", uri=True) as conn:
            conn.execute("PRAGMA query_only=ON")
            wanted: list[int] = []
            for rowid, scope_key in conn.execute(
                "SELECT rowid,scope_key FROM canonical_ssot_fact "
                "WHERE fact_type='truth_observation_revision'"
            ):
                parts = str(scope_key).split("|")
                if len(parts) == 6 and (
                    parts[5] != sha
                    or parts[0].upper() not in stations
                    or (floor and parts[3][:10] < floor)
                ):
                    continue
                wanted.append(int(rowid))
            rows = conn.execute(
                "SELECT fact_json FROM canonical_ssot_fact "
                "WHERE rowid IN (SELECT value FROM json_each(?)) "
                "ORDER BY scope_key",
                (json.dumps(wanted),),
            ).fetchall()
        for (raw_json,) in rows:
            raw = json.loads(str(raw_json))
            if (
                str(raw.get("authority_scope_projection_content_sha256") or "")
                == sha
                and str(raw.get("station_id") or "").upper() in stations
                and str(raw.get("event_date") or "") in days
            ):
                stored.append(raw)
    stored.sort(
        key=lambda raw: (
            str(raw.get("station_id")),
            str(raw.get("observation_id")),
            int(raw.get("revision_no") or 0),
        )
    )
    value = dict(evidence)
    value["stored_revision_facts"] = stored
    identity = {key: item for key, item in value.items() if key != "receipt_sha256"}
    return {**identity, "receipt_sha256": content_sha256(identity)}


def _parse_as_of_date(as_of_utc: str) -> date:
    return datetime.fromisoformat(
        str(as_of_utc).replace("Z", "+00:00")
    ).astimezone(timezone.utc).date()


def _materialized_event_dates(target_db: Path) -> list[str]:
    """Event dates already closed out by a committed day-bounds fact."""

    with sqlite3.connect(f"file:{target_db}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT DISTINCT json_extract(fact_json,'$.event_date') "
            "FROM canonical_ssot_fact "
            "WHERE fact_type='truth_observation_day_bounds'"
        ).fetchall()
    return sorted({str(value) for (value,) in rows if value})


def _materialized_day_scopes(
    target_db: Path, projection_sha: str
) -> dict[tuple[str, str], str]:
    """Station-days closed under this authority, each with its closing zone."""

    existing: dict[tuple[str, str], str] = {}
    with sqlite3.connect(f"file:{target_db}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT fact_json FROM canonical_ssot_fact "
            "WHERE fact_type='truth_observation_day_bounds' "
            "  AND json_extract(fact_json,"
            "'$.authority_scope_projection_content_sha256')=?",
            (str(projection_sha),),
        ).fetchall()
    for (raw_json,) in rows:
        raw = json.loads(str(raw_json))
        key = (str(raw["station_id"]).upper(), str(raw["event_date"]))
        zone = str(raw["settlement_timezone"])
        prior = existing.get(key)
        if prior is not None and prior != zone:
            raise ContractError(f"pr12_existing_day_timezone_ambiguous:{key[0]}:{key[1]}")
        existing[key] = zone
    return existing


def _exclude_materialized_day_scopes(
    target_db: Path,
    evidence: dict[str, object],
    projection_sha: str,
    *,
    existing: Mapping[tuple[str, str], str] | None = None,
) -> dict[str, object]:
    """Do not rebuild an append-only station-day already in canonical SSOT.

    "Already" means under the authority this pass is building against, not
    under any authority ever.  The 2026-08-26 doctrine amendment moved the
    settlement day from a fleet-wide Eastern one to the one each station's
    market listing declares, which minted a new projection and therefore a new
    generation of facts. Days closed under the old Eastern authority are
    history: they keep their rows, they do not block the new generation from
    building the same calendar day over the window the market actually
    settles, and comparing the two generations' zones is meaningless -- they
    are answers to different questions. Their scope keys carry the projection
    hash, so the two generations cannot collide.

    The exporter may already have left these days out at read
    (``skip_day_scopes``); what it skipped arrives as a count and joins the
    rows dropped here, so the sealed packet is the one the full read made.
    """

    if existing is None:
        existing = _materialized_day_scopes(target_db, projection_sha)

    value = dict(evidence)
    skipped_at_read = value.pop("read_skipped_day_scopes", None)
    excluded = (
        int(skipped_at_read.get("evidence_row_count") or 0)
        if isinstance(skipped_at_read, Mapping)
        else 0
    )
    kept_rows = []
    for raw in value.get("rows", []):
        key = (str(raw.get("station_id") or "").upper(), str(raw.get("event_date") or ""))
        prior_zone = existing.get(key)
        if prior_zone is None:
            kept_rows.append(raw)
            continue
        if prior_zone != str(raw.get("settlement_timezone") or ""):
            raise ContractError(f"pr12_existing_day_timezone_conflict:{key[0]}:{key[1]}")
        excluded += 1
    value["rows"] = kept_rows
    for field in ("station_day_halts", "station_day_reviews"):
        value[field] = [
            raw for raw in value.get(field, [])
            if (
                str(raw.get("station_id") or "").upper(),
                str(raw.get("event_date") or ""),
            ) not in existing
        ]
    value["materialized_day_scope_exclusion"] = {
        "existing_day_scope_count": len(existing),
        "excluded_evidence_row_count": excluded,
        "uses_canonical_natural_key_only": True,
        "backfills_or_rekeys_existing_fact": False,
    }
    identity = {key: item for key, item in value.items() if key != "receipt_sha256"}
    return {**identity, "receipt_sha256": content_sha256(identity)}


def _conflicting_day_scopes(
    target_db: Path, contract: Mapping[str, object]
) -> dict[tuple[str, str], dict[str, str]]:
    """Station-days whose reports canonical SSOT already identifies differently.

    Ingest refuses a scope that arrives with new content, and the refusal
    takes the whole savepoint: on 2026-08-26 one such day refused all 1,135
    facts of the round, which is why the lane produced nothing for a day.
    That is the correct instinct at the wrong grain -- the store is right to
    refuse the day, and wrong to lose the rest of the fleet with it, exactly
    the per-scope isolation the settlement-truth sweep already applies.

    Such a day is not repaired here.  Its stored ordinals were sealed by an
    earlier partly-visible build (measured: CYYZ 2026-08-21 holds sequences
    1..20 for the reports before 2026-08-22T00:00Z and *restarts* at 1..4 for
    the four that follow, because the day was built twice with the bound
    inside it), and rewriting a committed canonical fact is a supersession
    that wants a review, not a side effect of a build.  So the day is
    quarantined and named, and everything else in the round proceeds.
    """

    parsed = CanonicalAWCObservationContract.from_mapping(dict(contract))
    scopes: dict[str, tuple[str, str, str, str]] = {}
    for fact_type, facts in (
        ("truth_observation_revision", parsed.revision_facts),
        ("truth_observation_day_bounds", parsed.day_bounds_facts),
    ):
        for fact in facts:
            scopes[fact.scope_key] = (
                fact_type,
                str(fact.raw["station_id"]),
                str(fact.raw["event_date"]),
                fact.content_sha256,
            )
    if not scopes:
        return {}
    conflicted: dict[tuple[str, str], dict[str, str]] = {}
    with sqlite3.connect(f"file:{target_db}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        for scope_key, (fact_type, station, event_date, sha) in scopes.items():
            row = conn.execute(
                "SELECT content_sha256 FROM canonical_ssot_fact "
                "WHERE fact_type=? AND scope_key=?",
                (fact_type, scope_key),
            ).fetchone()
            if row is None or str(row[0]) == sha:
                continue
            conflicted.setdefault(
                (station, event_date),
                {
                    "station_id": station,
                    "event_date": event_date,
                    "fact_type": fact_type,
                    "scope_key": scope_key,
                    "stored_content_sha256": str(row[0]),
                    "rebuilt_content_sha256": sha,
                    "failure_code": "PR12_DAY_ALREADY_IDENTIFIED_DIFFERENTLY",
                },
            )
    return conflicted


def _exclude_day_scopes(
    evidence: Mapping[str, object],
    scopes: Mapping[tuple[str, str], Mapping[str, str]],
) -> dict[str, object]:
    """Drop every trace of the quarantined station-days and re-seal the packet.

    Always annotates, including with nothing quarantined, so the packet says
    the check ran rather than leaving its absence to be read as either.
    """

    value = dict(evidence)
    excluded = 0
    for field in ("rows", "station_day_halts", "station_day_reviews"):
        kept = []
        for raw in value.get(field, []):
            key = (
                str(raw.get("station_id") or "").upper(),
                str(raw.get("event_date") or ""),
            )
            if key in scopes:
                excluded += 1
                continue
            kept.append(raw)
        value[field] = kept
    value["conflicting_day_scope_quarantine"] = {
        "quarantined_day_count": len(scopes),
        "excluded_evidence_row_count": excluded,
        "scopes": [dict(scopes[key]) for key in sorted(scopes)],
        "rewrites_committed_fact": False,
    }
    identity = {key: item for key, item in value.items() if key != "receipt_sha256"}
    return {**identity, "receipt_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-database", type=Path, required=True)
    parser.add_argument("--target-database", type=Path, required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--evidence-output", type=Path, required=True)
    parser.add_argument("--contract-output", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    parser.add_argument("--authority-projection", type=Path, required=True)
    parser.add_argument(
        "--window-days",
        type=int,
        default=3,
        help="export station-days holding a report this many days back;"
        " 0 exports the whole archive",
    )
    args = parser.parse_args()
    target = args.target_database.expanduser().resolve(strict=True)
    registry = _station_registry(target)
    projection = validate_pr12_authority_scope_projection(
        json.loads(args.authority_projection.expanduser().resolve(strict=True).read_text())
    )
    # Read a recency window, not the whole backlog.  The pass re-derives every
    # station-day it can see and the archive is append-only, so an unbounded
    # read grows until it trips the builder's row cap and fails the lane
    # closed (measured 2026-08-21: 44,710 rows against a 20,000 cap).
    #
    # Anchoring instead to the oldest un-materialised day drags three weeks of
    # backlog into a single pass and trips the same cap, and it answers a
    # question nobody asked here: the live stage needs today's reports
    # identified, while filling historical gaps is an offline concern with its
    # own window.
    #
    # The bound is a UTC instant and a day is local, so the day it lands inside
    # is only partly visible; the exporter refuses such a day rather than
    # renumbering it (see export_awc_ingress_evidence_v4).  That costs one day
    # of reach at the far edge, so the caller spends one more: three days back
    # leaves every timezone in the fleet -- UTC-12 through UTC+14 -- at least
    # two whole local days inside the window, which is the coverage the two-day
    # bound was reaching for before it started cutting days in half.
    since_valid_at = ""
    if args.window_days > 0:
        since_valid_at = (
            _parse_as_of_date(args.as_of_utc) - timedelta(days=args.window_days)
        ).isoformat() + "T00:00:00.000Z"
    # Closed days are located by key and never hashed (2026-09-19: 87% of
    # the rows read, ~15s of a 19s pass); the packet is unchanged.
    materialized = _materialized_day_scopes(target, projection["content_sha256"])
    evidence = export_awc_ingress_evidence_v4(
        args.archive_database,
        station_registry=registry,
        authority_scope_projection=projection,
        as_of_utc=args.as_of_utc,
        since_valid_at_utc=since_valid_at,
        skip_day_scopes=materialized,
    )
    skipped_at_read = dict(evidence.get("read_skipped_day_scopes") or {})
    evidence = _exclude_materialized_day_scopes(
        target, evidence, projection["content_sha256"], existing=materialized
    )
    evidence = _attach_stored_revision_facts(
        target, evidence, projection["content_sha256"]
    )
    # Build once to learn which days the store already identifies differently,
    # then drop those and build the contract that actually ships.  Quarantining
    # before staging rather than during is what keeps the artifact on disk one
    # the apply step can accept: a conflicting day refuses the whole savepoint
    # at ingest, so a contract carrying one is a contract nothing can ever
    # apply.  The probe build is the same 3k rows and costs nothing worth
    # avoiding.
    probe, _probe_receipt = build_awc_observation_contract(evidence)
    quarantined = _conflicting_day_scopes(target, probe)
    evidence = _exclude_day_scopes(evidence, quarantined)
    contract, receipt = build_awc_observation_contract(evidence)
    for destination, payload in (
        (args.evidence_output, evidence),
        (args.contract_output, contract),
        (args.receipt_output, receipt),
    ):
        write_bounded_json_if_changed(
            destination.expanduser().resolve(),
            payload,
            limit=64 * 1024 * 1024,
            max_nodes=1_000_000,
        )
    summary = {
        "state": receipt["state"],
        "evidence_rows": len(evidence["rows"]),
        "extraction_gaps": len(evidence["extraction_gaps"]),
        "revision_facts": len(contract["revision_facts"]),
        "day_bounds_facts": len(contract["day_bounds_facts"]),
        "terminal_gaps": len(receipt["terminal_gaps"]),
        "truncated_day_scopes": len(evidence.get("truncated_day_scopes") or ()),
        "quarantined_day_scopes": int(
            (evidence.get("conflicting_day_scope_quarantine") or {}).get(
                "quarantined_day_count", 0
            )
        ),
        "evidence_receipt_sha256": evidence["receipt_sha256"],
        "closed_day_rows_located_by_key_only": skipped_at_read.get(
            "rows_located_by_key_only", 0
        ),
        "contract_content_sha256": contract["content_sha256"],
        "builder_receipt_sha256": receipt["receipt_sha256"],
        "writes_database": False,
        "publishes_pr04_pointer": False,
        "starts_services": False,
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
