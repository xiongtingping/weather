"""Native-ensemble certificates never simply end.

User ruling 2026-09-23 (熊先生: 「到期前端预警，不过期」).  A P0 calibration
certificate -- a system artifact, or a cross artifact with its member system
artifacts, each behind an activation -- is not allowed to run out.  Its
thirty-day window stays what it always was, a freshness window: a lane that
fits daily re-mints inside it and nothing here ever fires.  What changes is
the end.  A scope whose newest certificate is inside ``RENEW_WITHIN_DAYS`` of
its end, with nothing fresher behind it, is renewed: the same evidence under
a new window, generation advanced, and a receipt row the board reads to say
which scopes serve on evidence the lane has not refreshed, and how old that
evidence is.

Why the end existed, and what keeps this from bringing that back.  The window
was the only thing that retired a fossil: ECCC's 00Z h24_48/h48_plus lanes
stopped supplying cases on 2026-08-11 and WMKK was still served a
``trained_through`` 08-11 certificate two weeks later, because the OOS lane
kept re-activating a frozen case set.  Emission was fixed at the source
(``native_ensemble_oos``, supply freshness), and the rows already written
aged out on their windows.  Two rules keep that repair intact:

1. **Fresh beats stale.**  A renewal is minted only when nothing fresher
   exists for the scope.  A cross scope (station x season x lead) with any
   activation ending past the horizon is left alone; so is a system scope
   (station x system x season x lead).  The moment a lane recovers, its
   fresh rows end past the horizon, renewals stop, and the renewed rows age
   out on their own thirty days -- exactly the old behaviour.
2. **Stale beats nothing.**  A renewed certificate serves through the
   reader's unchanged window predicate (``valid_from <= at < valid_until``,
   newest generation first), so a compile never finds a scope dark because
   nobody was watching a date.  The board is where the staleness is said.

Renewal copies rows, it never edits them: artifact rows are content-addressed
with their clocks inside the address, so a renewed artifact is a new row with
identical evidence columns, and its activation is a new generation of the
same activation key.  Nothing a replay reads as-of a past moment changes.

The lane is the daily ``calibration_refit`` sweep (``scripts/
calibration_refit_sweep.sh`` -> ``scripts/renew_native_ensemble_certificates
.py``).  ``RENEW_WITHIN_DAYS`` is sized against that cadence: ten days of
slack covers a hosted sweep starved for a deploy storm (72 h measured
2026-09) three times over.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .contracts import ContractError, content_sha256
from .model_product_identity import PRODUCT_BY_ID
from .native_ensemble_oos import DEFAULT_VALID_DAYS
from .station_season import season_bins_for_window

#: A renewed certificate's window.  The same thirty days a fresh mint gets:
#: short on purpose, so a lane that recovers pushes its renewals out of the
#: reader's candidate set within a month instead of a year.
VALID_DAYS = DEFAULT_VALID_DAYS
#: Renew a scope whose newest certificate ends inside this many days.
RENEW_WITHIN_DAYS = 10.0
#: Board thresholds.  A scope inside the warn window with no renewal means
#: the daily lane has not run for a while; inside the red window it is about
#: to go dark.  A renewed scope is amber on sight (it serves on evidence the
#: lane stopped refreshing) and red once that evidence is a month old.
EXPIRY_WARN_DAYS = RENEW_WITHIN_DAYS
EXPIRY_RED_DAYS = 3.0
STALE_EVIDENCE_RED_DAYS = 30.0
RENEWAL_REASON = "no_fresher_certificate_in_scope_v1"
#: Only the active model-product registry is kept alive: a retired product's
#: last certificate ends and stays ended (AGENTS.md: retired products keep
#: their lineage as history and never re-enter formal authority).
ACTIVE_SYSTEM_IDS = frozenset(PRODUCT_BY_ID)
#: Charter 47's lead vocabulary; the calendar tokens (d0/d1/d2) are history.
LEAD_BUCKETS = frozenset({"h00_12", "h12_24", "h24_48", "h48_plus"})
RENEWAL_ID_PREFIX = "native-certificate-renewal-"
RENEWAL_TABLE = "calibration_native_ensemble_certificate_renewal"
ACTIVATION_TABLE = "calibration_native_ensemble_activation"
SYSTEM_ARTIFACT_TABLE = "calibration_native_ensemble_system_artifact"
CROSS_ARTIFACT_TABLE = "calibration_cross_native_ensemble_artifact"
SELECTION_EVIDENCE_TABLE = (
    "calibration_cross_native_ensemble_selection_evidence"
)
SYSTEM_KIND = "NATIVE_ENSEMBLE_SYSTEM_CALIBRATION"
CROSS_KIND = "CROSS_NATIVE_ENSEMBLE"
AUTHORITY = "PRODUCTION_NO_LIVE_ONLY"
_SYSTEM_ARTIFACT_ADDRESS_COLUMNS = ("system_artifact_id", "content_sha256")
_CROSS_ARTIFACT_ADDRESS_COLUMNS = (
    "cross_ensemble_artifact_id", "content_sha256",
)
_CLOCK_COLUMNS = ("valid_from_utc", "valid_until_utc")


def utc_text(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _moment(text: Any) -> datetime:
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _row_dict(cursor: sqlite3.Cursor, row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        str(description[0]): value
        for description, value in zip(cursor.description, row)
    }


def _fetch_dict(
    conn: sqlite3.Connection, sql: str, params: tuple[Any, ...],
) -> dict[str, Any] | None:
    cursor = conn.execute(sql, params)
    row = cursor.fetchone()
    return None if row is None else _row_dict(cursor, row)


def _fetch_dicts(
    conn: sqlite3.Connection, sql: str, params: tuple[Any, ...],
) -> list[dict[str, Any]]:
    cursor = conn.execute(sql, params)
    return [_row_dict(cursor, row) for row in cursor.fetchall()]


def _insert_content_row(
    conn: sqlite3.Connection,
    *,
    table: str,
    primary_key: str,
    row: Mapping[str, Any],
) -> int:
    """Append-only insert, no-op on the same content, conflict on different.

    The same rule ``native_ensemble_oos._insert_content_row`` applies; kept
    here rather than imported so this module's writes do not depend on a
    private name of a 7,000-line neighbour.
    """

    existing = conn.execute(
        f"SELECT content_sha256 FROM {table} WHERE {primary_key}=?",
        (row[primary_key],),
    ).fetchone()
    if existing is not None:
        if str(existing[0]) != str(row["content_sha256"]):
            raise ContractError(
                f"native_certificate_renewal_{table}_content_conflict"
            )
        return 0
    columns = tuple(row)
    conn.execute(
        f"INSERT INTO {table}({','.join(columns)}) "
        f"VALUES({','.join('?' for _ in columns)})",
        tuple(row[column] for column in columns),
    )
    return 1


# --------------------------------------------------------------------------
# Which scopes the clock keeps alive at all
# --------------------------------------------------------------------------
#
# Measured on the production store 2026-09-23 before these filters: 1,683
# renewals planned, 855 of them under the retired ``JJA`` label, 726 under
# ``PEAK`` at stations that had already moved to ``DESCENT``, and 143 for
# point systems charter 133/201 retired from the member pool.  None of those
# scopes is read again; renewing them would be minting fossils by the
# thousand.  So a scope is kept alive only when the station will serve its
# label inside the renewed window (charter 62's own rule at mint time), its
# lead is charter 47 vocabulary, and every system is in the active registry.


def served_labels(
    station_id: str, *, now: datetime, valid_days: int,
) -> tuple[str, ...] | None:
    """Labels the station serves in the renewed window, None if unmapped."""

    start = (now.astimezone(timezone.utc).date() - timedelta(days=1))
    end = start + timedelta(days=valid_days + 2)
    try:
        return season_bins_for_window(
            station_id, start.isoformat(), end.isoformat(),
        )
    except ContractError:
        return None


def _scope_skip_reason(
    *,
    station_id: str,
    season_bin: str,
    lead_bucket: str,
    system_ids: "tuple[str, ...]",
    labels: "tuple[str, ...] | None",
) -> str | None:
    if labels is None:
        return "unmapped_station"
    if season_bin not in labels:
        return "label_not_served"
    if lead_bucket not in LEAD_BUCKETS:
        return "lead_vocabulary"
    if any(system_id not in ACTIVE_SYSTEM_IDS for system_id in system_ids):
        return "retired_system"
    return None


# --------------------------------------------------------------------------
# What is about to end with nothing fresher behind it
# --------------------------------------------------------------------------


def stalled_cross_scopes(
    conn: sqlite3.Connection, *, now_text: str, horizon_text: str,
) -> list[dict[str, Any]]:
    """Cross scopes whose every live activation ends by ``horizon_text``."""

    rows = conn.execute(
        f"SELECT station_id,season_bin,lead_bucket,"
        f"MAX(valid_until_utc),COUNT(*) FROM {ACTIVATION_TABLE} "
        f"WHERE artifact_kind=? AND authority_scope=? "
        f"AND valid_from_utc<=? AND valid_until_utc>? "
        f"GROUP BY station_id,season_bin,lead_bucket "
        f"HAVING MAX(valid_until_utc)<=? "
        f"ORDER BY station_id,season_bin,lead_bucket",
        (CROSS_KIND, AUTHORITY, now_text, now_text, horizon_text),
    ).fetchall()
    return [
        {
            "station_id": str(row[0]),
            "season_bin": str(row[1]),
            "lead_bucket": str(row[2]),
            "newest_valid_until_utc": str(row[3]),
            "live_activation_count": int(row[4]),
        }
        for row in rows
    ]


def stalled_system_scopes(
    conn: sqlite3.Connection, *, now_text: str, horizon_text: str,
) -> list[dict[str, Any]]:
    """System scopes whose every live activation ends by ``horizon_text``."""

    rows = conn.execute(
        f"SELECT station_id,ensemble_system_id_or_star,season_bin,"
        f"lead_bucket,MAX(valid_until_utc),COUNT(*) FROM {ACTIVATION_TABLE} "
        f"WHERE artifact_kind=? AND authority_scope=? "
        f"AND available_system_set_hash_or_star='*' "
        f"AND valid_from_utc<=? AND valid_until_utc>? "
        f"GROUP BY station_id,ensemble_system_id_or_star,season_bin,"
        f"lead_bucket HAVING MAX(valid_until_utc)<=? "
        f"ORDER BY station_id,ensemble_system_id_or_star,season_bin,"
        f"lead_bucket",
        (SYSTEM_KIND, AUTHORITY, now_text, now_text, horizon_text),
    ).fetchall()
    return [
        {
            "station_id": str(row[0]),
            "ensemble_system_id": str(row[1]),
            "season_bin": str(row[2]),
            "lead_bucket": str(row[3]),
            "newest_valid_until_utc": str(row[4]),
            "live_activation_count": int(row[5]),
        }
        for row in rows
    ]


def _newest_cross_activation_per_pairing(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    season_bin: str,
    lead_bucket: str,
    now_text: str,
) -> list[dict[str, Any]]:
    """One activation per (system set, cycle map, set hash, run cycle).

    The reader ranks a scope's candidates by exact system-set and cycle
    match before anything else, so a stalled scope keeps one live row per
    distinct pairing it held, not only the single newest row.
    """

    rows = _fetch_dicts(
        conn,
        f"SELECT a.activation_id,a.artifact_id,a.run_cycle,"
        f"a.run_cycle_by_system_json,a.run_cycle_by_system_sha256,"
        f"a.settlement_definition,a.available_system_set_hash_or_star,"
        f"a.generation,a.valid_from_utc,a.valid_until_utc,"
        f"r.ensemble_system_ids_json "
        f"FROM {ACTIVATION_TABLE} a "
        f"JOIN {CROSS_ARTIFACT_TABLE} r "
        f"ON r.cross_ensemble_artifact_id=a.artifact_id "
        f"WHERE a.artifact_kind=? AND a.station_id=? AND a.season_bin=? "
        f"AND a.lead_bucket=? AND a.authority_scope=? "
        f"AND a.valid_from_utc<=? AND a.valid_until_utc>? "
        f"ORDER BY a.valid_from_utc DESC,a.generation DESC,a.activation_id",
        (
            CROSS_KIND, station_id, season_bin, lead_bucket, AUTHORITY,
            now_text, now_text,
        ),
    )
    newest: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row["ensemble_system_ids_json"]),
            str(row["run_cycle_by_system_sha256"] or ""),
            str(row["available_system_set_hash_or_star"]),
            str(row["run_cycle"]),
        )
        newest.setdefault(key, row)
    return list(newest.values())


def _newest_system_activation(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    system_id: str,
    season_bin: str,
    lead_bucket: str,
    run_cycle: str,
) -> dict[str, Any] | None:
    """The exact-key system activation the reader would rank first."""

    return _fetch_dict(
        conn,
        f"SELECT activation_id,artifact_id,run_cycle,"
        f"run_cycle_by_system_json,run_cycle_by_system_sha256,"
        f"settlement_definition,generation,valid_from_utc,valid_until_utc "
        f"FROM {ACTIVATION_TABLE} "
        f"WHERE artifact_kind=? AND station_id=? "
        f"AND ensemble_system_id_or_star=? AND season_bin=? "
        f"AND lead_bucket=? AND run_cycle=? "
        f"AND available_system_set_hash_or_star='*' "
        f"AND run_cycle_by_system_json=? AND run_cycle_by_system_sha256=? "
        f"AND authority_scope=? "
        f"ORDER BY generation DESC,valid_from_utc DESC LIMIT 1",
        (
            SYSTEM_KIND, station_id, system_id, season_bin, lead_bucket,
            run_cycle, _json({system_id: run_cycle}),
            content_sha256({system_id: run_cycle}), AUTHORITY,
        ),
    )


def plan_renewals(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    within_days: float = RENEW_WITHIN_DAYS,
    valid_days: int = VALID_DAYS,
) -> dict[str, Any]:
    """Every renewal the clock would mint now.  Read-only.

    Cross scopes first: each stalled scope contributes its newest live
    activation per pairing, and each of those pulls in the member system
    activations it needs alive for at least as long.  Then system scopes
    with nothing fresher at any cycle (members of a renewed cross are
    covered already and are not listed twice).
    """

    if within_days <= 0.0:
        raise ContractError("native_certificate_renewal_within_days_invalid")
    if valid_days < 1 or valid_days > 90:
        raise ContractError("native_certificate_renewal_valid_days_invalid")
    now_text = utc_text(now)
    horizon_text = utc_text(now + timedelta(days=within_days))
    until_text = utc_text(now + timedelta(days=valid_days))
    items: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    skipped: dict[str, int] = {}
    labels_by_station: dict[str, tuple[str, ...] | None] = {}

    def _labels(station_id: str) -> tuple[str, ...] | None:
        if station_id not in labels_by_station:
            labels_by_station[station_id] = served_labels(
                station_id, now=now, valid_days=valid_days,
            )
        return labels_by_station[station_id]

    def _skip(reason: str | None) -> bool:
        if reason is None:
            return False
        skipped[reason] = skipped.get(reason, 0) + 1
        return True

    def _add(item: dict[str, Any]) -> None:
        if item["source_activation_id"] in seen_sources:
            return
        seen_sources.add(item["source_activation_id"])
        items.append(item)

    cross_scopes = stalled_cross_scopes(
        conn, now_text=now_text, horizon_text=horizon_text,
    )
    for scope in cross_scopes:
        if _skip(_scope_skip_reason(
            station_id=scope["station_id"], season_bin=scope["season_bin"],
            lead_bucket=scope["lead_bucket"], system_ids=(),
            labels=_labels(scope["station_id"]),
        )):
            continue
        for row in _newest_cross_activation_per_pairing(
            conn,
            station_id=scope["station_id"],
            season_bin=scope["season_bin"],
            lead_bucket=scope["lead_bucket"],
            now_text=now_text,
        ):
            try:
                pairing_systems = tuple(
                    str(value) for value in json.loads(
                        str(row["ensemble_system_ids_json"] or "[]")
                    )
                )
            except ValueError as exc:
                raise ContractError(
                    "native_certificate_renewal_system_set_unreadable"
                ) from exc
            if _skip(_scope_skip_reason(
                station_id=scope["station_id"],
                season_bin=scope["season_bin"],
                lead_bucket=scope["lead_bucket"],
                system_ids=pairing_systems,
                labels=_labels(scope["station_id"]),
            )):
                continue
            _add({
                "role": "cross",
                "artifact_kind": CROSS_KIND,
                "station_id": scope["station_id"],
                "ensemble_system_id_or_star": "*",
                "season_bin": scope["season_bin"],
                "lead_bucket": scope["lead_bucket"],
                "run_cycle": str(row["run_cycle"]),
                "run_cycle_by_system_sha256": row["run_cycle_by_system_sha256"],
                "source_activation_id": str(row["activation_id"]),
                "source_artifact_id": str(row["artifact_id"]),
                "source_generation": int(row["generation"]),
                "source_valid_until_utc": str(row["valid_until_utc"]),
            })
            try:
                cycle_by_system = json.loads(
                    str(row["run_cycle_by_system_json"] or "{}")
                )
            except ValueError as exc:
                raise ContractError(
                    "native_certificate_renewal_cycle_map_unreadable"
                ) from exc
            for system_id, run_cycle in sorted(cycle_by_system.items()):
                member = _newest_system_activation(
                    conn,
                    station_id=scope["station_id"],
                    system_id=str(system_id),
                    season_bin=scope["season_bin"],
                    lead_bucket=scope["lead_bucket"],
                    run_cycle=str(run_cycle),
                )
                if member is None:
                    # A cross pairing whose member was never activated on
                    # this exact key could not have been served; the
                    # reader fails closed on it today and will tomorrow.
                    continue
                if str(member["valid_until_utc"]) >= until_text:
                    continue
                _add({
                    "role": "cross_member",
                    "artifact_kind": SYSTEM_KIND,
                    "station_id": scope["station_id"],
                    "ensemble_system_id_or_star": str(system_id),
                    "season_bin": scope["season_bin"],
                    "lead_bucket": scope["lead_bucket"],
                    "run_cycle": str(run_cycle),
                    "run_cycle_by_system_sha256": member[
                        "run_cycle_by_system_sha256"
                    ],
                    "source_activation_id": str(member["activation_id"]),
                    "source_artifact_id": str(member["artifact_id"]),
                    "source_generation": int(member["generation"]),
                    "source_valid_until_utc": str(member["valid_until_utc"]),
                })
    system_scopes = stalled_system_scopes(
        conn, now_text=now_text, horizon_text=horizon_text,
    )
    for scope in system_scopes:
        if _skip(_scope_skip_reason(
            station_id=scope["station_id"], season_bin=scope["season_bin"],
            lead_bucket=scope["lead_bucket"],
            system_ids=(scope["ensemble_system_id"],),
            labels=_labels(scope["station_id"]),
        )):
            continue
        rows = _fetch_dicts(
            conn,
            f"SELECT activation_id,artifact_id,run_cycle,"
            f"run_cycle_by_system_sha256,generation,valid_from_utc,"
            f"valid_until_utc FROM {ACTIVATION_TABLE} "
            f"WHERE artifact_kind=? AND station_id=? "
            f"AND ensemble_system_id_or_star=? AND season_bin=? "
            f"AND lead_bucket=? AND available_system_set_hash_or_star='*' "
            f"AND authority_scope=? AND valid_from_utc<=? "
            f"AND valid_until_utc>? "
            f"ORDER BY valid_from_utc DESC,generation DESC,activation_id",
            (
                SYSTEM_KIND, scope["station_id"],
                scope["ensemble_system_id"], scope["season_bin"],
                scope["lead_bucket"], AUTHORITY, now_text, now_text,
            ),
        )
        newest_per_cycle: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            key = (
                str(row["run_cycle"]),
                str(row["run_cycle_by_system_sha256"] or ""),
            )
            newest_per_cycle.setdefault(key, row)
        for row in newest_per_cycle.values():
            _add({
                "role": "system",
                "artifact_kind": SYSTEM_KIND,
                "station_id": scope["station_id"],
                "ensemble_system_id_or_star": scope["ensemble_system_id"],
                "season_bin": scope["season_bin"],
                "lead_bucket": scope["lead_bucket"],
                "run_cycle": str(row["run_cycle"]),
                "run_cycle_by_system_sha256": row[
                    "run_cycle_by_system_sha256"
                ],
                "source_activation_id": str(row["activation_id"]),
                "source_artifact_id": str(row["artifact_id"]),
                "source_generation": int(row["generation"]),
                "source_valid_until_utc": str(row["valid_until_utc"]),
            })
    return {
        "schema": "native_ensemble_certificate_renewal_plan_v1",
        "now_utc": now_text,
        "renew_within_days": float(within_days),
        "renewed_valid_until_utc": until_text,
        "stalled_cross_scopes": cross_scopes,
        "stalled_system_scopes": system_scopes,
        "skipped": skipped,
        "items": items,
    }


# --------------------------------------------------------------------------
# Minting a renewal: copy the evidence, advance the generation, receipt it
# --------------------------------------------------------------------------


def _renewed_artifact(
    conn: sqlite3.Connection,
    *,
    table: str,
    primary_key: str,
    id_prefix: str,
    source_artifact_id: str,
    now_text: str,
    until_text: str,
) -> tuple[dict[str, Any], dict[str, Any], int]:
    """(source row, renewed row, rows written) for one artifact table."""

    source = _fetch_dict(
        conn, f"SELECT * FROM {table} WHERE {primary_key}=?",
        (source_artifact_id,),
    )
    if source is None:
        raise ContractError(
            f"native_certificate_renewal_{table}_source_missing"
        )
    identity = {
        column: value
        for column, value in source.items()
        if column not in (primary_key, "content_sha256")
    }
    identity["valid_from_utc"] = now_text
    identity["valid_until_utc"] = until_text
    sha = content_sha256(identity)
    renewed = {
        primary_key: id_prefix + sha[:24],
        **identity,
        "content_sha256": sha,
    }
    written = _insert_content_row(
        conn, table=table, primary_key=primary_key, row=renewed,
    )
    return source, renewed, written


def _renewed_activation(
    conn: sqlite3.Connection,
    *,
    source: Mapping[str, Any],
    renewed_artifact_id: str,
    generation_scope_sql: str,
    generation_scope_params: tuple[Any, ...],
    now_text: str,
    until_text: str,
) -> tuple[dict[str, Any], int]:
    generation = 1 + int(conn.execute(
        f"SELECT COALESCE(MAX(generation),0) FROM {ACTIVATION_TABLE} "
        f"WHERE {generation_scope_sql}",
        generation_scope_params,
    ).fetchone()[0])
    identity = {
        "artifact_kind": str(source["artifact_kind"]),
        "artifact_id": renewed_artifact_id,
        "station_id": str(source["station_id"]),
        "ensemble_system_id_or_star": str(
            source["ensemble_system_id_or_star"]
        ),
        "season_bin": str(source["season_bin"]),
        "lead_bucket": str(source["lead_bucket"]),
        "run_cycle": str(source["run_cycle"]),
        "run_cycle_by_system_json": source["run_cycle_by_system_json"],
        "run_cycle_by_system_sha256": source["run_cycle_by_system_sha256"],
        "settlement_definition": str(source["settlement_definition"]),
        "available_system_set_hash_or_star": str(
            source["available_system_set_hash_or_star"]
        ),
        "generation": generation,
        "valid_from_utc": now_text,
        "valid_until_utc": until_text,
        "activated_at_utc": now_text,
        "authority_scope": str(source["authority_scope"]),
    }
    sha = content_sha256(identity)
    row = {
        "activation_id": "native-activation-" + sha[:24],
        **identity,
        "content_sha256": sha,
    }
    written = _insert_content_row(
        conn, table=ACTIVATION_TABLE, primary_key="activation_id", row=row,
    )
    return row, written


def _receipt(
    conn: sqlite3.Connection,
    *,
    source_activation: Mapping[str, Any],
    renewed_activation: Mapping[str, Any],
    source_artifact_id: str,
    renewed_artifact_id: str,
    evidence_train_end_date: str,
    evidence_holdout_end_date: str,
    now_text: str,
    until_text: str,
) -> tuple[dict[str, Any], int]:
    identity = {
        "artifact_kind": str(source_activation["artifact_kind"]),
        "station_id": str(source_activation["station_id"]),
        "ensemble_system_id_or_star": str(
            source_activation["ensemble_system_id_or_star"]
        ),
        "season_bin": str(source_activation["season_bin"]),
        "lead_bucket": str(source_activation["lead_bucket"]),
        "run_cycle": str(source_activation["run_cycle"]),
        "run_cycle_by_system_sha256": source_activation[
            "run_cycle_by_system_sha256"
        ],
        "source_activation_id": str(source_activation["activation_id"]),
        "source_artifact_id": source_artifact_id,
        "renewed_activation_id": str(renewed_activation["activation_id"]),
        "renewed_artifact_id": renewed_artifact_id,
        "evidence_train_end_date": evidence_train_end_date,
        "evidence_holdout_end_date": evidence_holdout_end_date,
        "source_valid_until_utc": str(source_activation["valid_until_utc"]),
        "renewed_at_utc": now_text,
        "renewed_valid_until_utc": until_text,
        "renewal_reason": RENEWAL_REASON,
    }
    sha = content_sha256(identity)
    row = {
        "renewal_id": RENEWAL_ID_PREFIX + sha[:24],
        **identity,
        "content_sha256": sha,
    }
    written = _insert_content_row(
        conn, table=RENEWAL_TABLE, primary_key="renewal_id", row=row,
    )
    return row, written


def _source_activation(
    conn: sqlite3.Connection, activation_id: str,
) -> dict[str, Any]:
    row = _fetch_dict(
        conn, f"SELECT * FROM {ACTIVATION_TABLE} WHERE activation_id=?",
        (activation_id,),
    )
    if row is None:
        raise ContractError(
            "native_certificate_renewal_source_activation_missing"
        )
    return row


def _renew_system(
    conn: sqlite3.Connection,
    *,
    item: Mapping[str, Any],
    now_text: str,
    until_text: str,
    counts: dict[str, int],
) -> dict[str, Any]:
    source_activation = _source_activation(
        conn, str(item["source_activation_id"]),
    )
    source_artifact, renewed_artifact, written = _renewed_artifact(
        conn,
        table=SYSTEM_ARTIFACT_TABLE,
        primary_key="system_artifact_id",
        id_prefix="native-system-artifact-",
        source_artifact_id=str(source_activation["artifact_id"]),
        now_text=now_text,
        until_text=until_text,
    )
    counts["system_artifacts"] += written
    activation, written = _renewed_activation(
        conn,
        source=source_activation,
        renewed_artifact_id=str(renewed_artifact["system_artifact_id"]),
        generation_scope_sql=(
            "artifact_kind=? AND station_id=? "
            "AND ensemble_system_id_or_star=? AND season_bin=? "
            "AND lead_bucket=? AND run_cycle=? "
            "AND available_system_set_hash_or_star='*'"
        ),
        generation_scope_params=(
            SYSTEM_KIND,
            str(source_activation["station_id"]),
            str(source_activation["ensemble_system_id_or_star"]),
            str(source_activation["season_bin"]),
            str(source_activation["lead_bucket"]),
            str(source_activation["run_cycle"]),
        ),
        now_text=now_text,
        until_text=until_text,
    )
    counts["activations"] += written
    receipt, written = _receipt(
        conn,
        source_activation=source_activation,
        renewed_activation=activation,
        source_artifact_id=str(source_artifact["system_artifact_id"]),
        renewed_artifact_id=str(renewed_artifact["system_artifact_id"]),
        evidence_train_end_date=str(source_artifact["train_end_date"]),
        evidence_holdout_end_date=str(source_artifact["holdout_end_date"]),
        now_text=now_text,
        until_text=until_text,
    )
    counts["receipts"] += written
    return receipt


def _renew_cross(
    conn: sqlite3.Connection,
    *,
    item: Mapping[str, Any],
    now_text: str,
    until_text: str,
    counts: dict[str, int],
) -> dict[str, Any]:
    source_activation = _source_activation(
        conn, str(item["source_activation_id"]),
    )
    source_artifact, renewed_artifact, written = _renewed_artifact(
        conn,
        table=CROSS_ARTIFACT_TABLE,
        primary_key="cross_ensemble_artifact_id",
        id_prefix="native-cross-artifact-",
        source_artifact_id=str(source_activation["artifact_id"]),
        now_text=now_text,
        until_text=until_text,
    )
    counts["cross_artifacts"] += written
    activation, written = _renewed_activation(
        conn,
        source=source_activation,
        renewed_artifact_id=str(
            renewed_artifact["cross_ensemble_artifact_id"]
        ),
        # The same key ``native_ensemble_oos`` advances a cross generation
        # over: station, season, lead, run cycle and the available-set hash.
        generation_scope_sql=(
            "artifact_kind=? AND station_id=? AND season_bin=? "
            "AND lead_bucket=? AND run_cycle=? "
            "AND available_system_set_hash_or_star=?"
        ),
        generation_scope_params=(
            CROSS_KIND,
            str(source_activation["station_id"]),
            str(source_activation["season_bin"]),
            str(source_activation["lead_bucket"]),
            str(source_activation["run_cycle"]),
            str(source_activation["available_system_set_hash_or_star"]),
        ),
        now_text=now_text,
        until_text=until_text,
    )
    counts["activations"] += written
    evidence = _fetch_dict(
        conn,
        f"SELECT train_end_date,holdout_end_date "
        f"FROM {SELECTION_EVIDENCE_TABLE} WHERE content_sha256=?",
        (str(source_artifact["selection_evidence_sha256"]),),
    )
    if evidence is None:
        # Older cross artifacts hashed their evidence under another rule;
        # the artifact's own start is then the newest date its fit can be.
        fallback = str(source_artifact["valid_from_utc"])[:10]
        evidence = {"train_end_date": fallback, "holdout_end_date": fallback}
    receipt, written = _receipt(
        conn,
        source_activation=source_activation,
        renewed_activation=activation,
        source_artifact_id=str(source_artifact["cross_ensemble_artifact_id"]),
        renewed_artifact_id=str(renewed_artifact["cross_ensemble_artifact_id"]),
        evidence_train_end_date=str(evidence["train_end_date"]),
        evidence_holdout_end_date=str(evidence["holdout_end_date"]),
        now_text=now_text,
        until_text=until_text,
    )
    counts["receipts"] += written
    return receipt


def renew_expiring_certificates(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    commit: bool = True,
    within_days: float = RENEW_WITHIN_DAYS,
    valid_days: int = VALID_DAYS,
) -> dict[str, Any]:
    """Mint every renewal ``plan_renewals`` lists.  The lane's one entry.

    Without ``commit`` this is the plan alone and writes nothing, so it can
    run on a read-only connection.  With it, the table is installed if the
    store lacks it (the resident never applies schema at start), every
    renewal is written under one savepoint, and the receipts come back.
    """

    plan = plan_renewals(
        conn, now=now, within_days=within_days, valid_days=valid_days,
    )
    counts = {
        "system_artifacts": 0, "cross_artifacts": 0,
        "activations": 0, "receipts": 0,
    }
    result = {
        "schema": "native_ensemble_certificate_renewal_v1",
        "committed": False,
        "now_utc": plan["now_utc"],
        "renewed_valid_until_utc": plan["renewed_valid_until_utc"],
        "stalled_cross_scopes": len(plan["stalled_cross_scopes"]),
        "stalled_system_scopes": len(plan["stalled_system_scopes"]),
        "skipped": dict(plan["skipped"]),
        "planned": len(plan["items"]),
        "counts": counts,
        "receipts": [],
        "plan": plan,
    }
    if not commit:
        result["renewal_table_present"] = table_exists(conn, RENEWAL_TABLE)
        return result
    from .ssot import (  # noqa: PLC0415 - schema module is heavy
        apply_native_ensemble_certificate_renewal_schema,
    )

    # The writer lock first, then the schema, then the rows: a deferred
    # transaction upgraded after a read gets SQLITE_BUSY_SNAPSHOT at once on
    # a store that commits several times a second (see the schema applier).
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    apply_native_ensemble_certificate_renewal_schema(conn, commit=False)
    now_text = plan["now_utc"]
    until_text = plan["renewed_valid_until_utc"]
    conn.execute("SAVEPOINT native_certificate_renewal")
    try:
        for item in plan["items"]:
            if item["artifact_kind"] == CROSS_KIND:
                receipt = _renew_cross(
                    conn, item=item, now_text=now_text,
                    until_text=until_text, counts=counts,
                )
            else:
                receipt = _renew_system(
                    conn, item=item, now_text=now_text,
                    until_text=until_text, counts=counts,
                )
            result["receipts"].append({**receipt, "role": item["role"]})
        conn.execute("RELEASE SAVEPOINT native_certificate_renewal")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT native_certificate_renewal")
        conn.execute("RELEASE SAVEPOINT native_certificate_renewal")
        raise
    conn.commit()
    result["committed"] = True
    result["renewal_table_present"] = True
    return result


# --------------------------------------------------------------------------
# The board's reading
# --------------------------------------------------------------------------


def _days(start: datetime, end_text: Any) -> float:
    return (_moment(end_text) - start).total_seconds() / 86_400.0


def certificate_health(
    conn: sqlite3.Connection, *, now: datetime,
) -> dict[str, Any]:
    """Which scopes serve on renewed evidence, and which are about to end.

    Two questions.  Renewed: the scopes whose live certificate is a renewal
    (nothing fresher has been minted since), with the age of the evidence
    behind it -- amber on sight, red at a month.  Unrenewed: the scopes
    whose newest certificate ends inside the warn window and that the lane
    has not renewed -- the lane itself has not run; red inside three days.
    """

    now_text = utc_text(now)
    if not table_exists(conn, ACTIVATION_TABLE):
        return {
            "available": False, "health": "amber",
            "note": "读不出：激活表不存在",
        }
    renewals_known = table_exists(conn, RENEWAL_TABLE)
    renewed: list[dict[str, Any]] = []
    if renewals_known:
        rows = _fetch_dicts(
            conn,
            f"SELECT artifact_kind,station_id,ensemble_system_id_or_star,"
            f"season_bin,lead_bucket,run_cycle,renewed_activation_id,"
            f"evidence_train_end_date,evidence_holdout_end_date,"
            f"renewed_at_utc,renewed_valid_until_utc "
            f"FROM {RENEWAL_TABLE} WHERE renewed_valid_until_utc>? "
            f"AND renewed_at_utc<=? "
            f"ORDER BY station_id,artifact_kind,season_bin,lead_bucket,"
            f"renewed_at_utc DESC",
            (now_text, now_text),
        )
        by_scope: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
        for row in rows:
            key = (
                str(row["station_id"]), str(row["artifact_kind"]),
                str(row["ensemble_system_id_or_star"]),
                str(row["season_bin"]), str(row["lead_bucket"]),
            )
            by_scope.setdefault(key, []).append(row)
        for key, scope_rows in by_scope.items():
            station_id, kind, system_or_star, season_bin, lead_bucket = key
            newest = scope_rows[0]
            renewed_ids = {
                str(row["renewed_activation_id"]) for row in scope_rows
            }
            placeholders = ",".join("?" for _ in renewed_ids)
            # Fresher = minted by the lane after the newest renewal.  The
            # renewal's own source rows are still live until their end and
            # are not evidence the lane came back.
            fresher = conn.execute(
                f"SELECT COUNT(*) FROM {ACTIVATION_TABLE} "
                f"WHERE artifact_kind=? AND station_id=? "
                f"AND ensemble_system_id_or_star=? AND season_bin=? "
                f"AND lead_bucket=? AND authority_scope=? "
                f"AND valid_from_utc>? AND valid_from_utc<=? "
                f"AND valid_until_utc>? "
                f"AND activation_id NOT IN ({placeholders})",
                (
                    kind, station_id, system_or_star, season_bin,
                    lead_bucket, AUTHORITY, str(newest["renewed_at_utc"]),
                    now_text, now_text, *sorted(renewed_ids),
                ),
            ).fetchone()[0]
            evidence_age_days = -_days(
                now, str(newest["evidence_holdout_end_date"]) + "T00:00:00Z"
            )
            renewed.append({
                "station_id": station_id,
                "artifact_kind": kind,
                "ensemble_system_id_or_star": system_or_star,
                "season_bin": season_bin,
                "lead_bucket": lead_bucket,
                "renewal_count": len(scope_rows),
                "newest_renewed_at_utc": str(newest["renewed_at_utc"]),
                "evidence_holdout_end_date": str(
                    newest["evidence_holdout_end_date"]
                ),
                "evidence_age_days": round(evidence_age_days, 1),
                "fresher_activation_count": int(fresher),
                "serving_on_renewal": int(fresher) == 0,
            })
    serving = [row for row in renewed if row["serving_on_renewal"]]
    horizon_text = utc_text(now + timedelta(days=EXPIRY_WARN_DAYS))
    unrenewed: list[dict[str, Any]] = []
    labels_by_station: dict[str, tuple[str, ...] | None] = {}

    def _labels(station_id: str) -> tuple[str, ...] | None:
        if station_id not in labels_by_station:
            labels_by_station[station_id] = served_labels(
                station_id, now=now, valid_days=VALID_DAYS,
            )
        return labels_by_station[station_id]

    for scope in stalled_cross_scopes(
        conn, now_text=now_text, horizon_text=horizon_text,
    ):
        if _scope_skip_reason(
            station_id=scope["station_id"], season_bin=scope["season_bin"],
            lead_bucket=scope["lead_bucket"], system_ids=(),
            labels=_labels(scope["station_id"]),
        ):
            continue
        # A scope whose every live pairing carries a retired product is
        # not one the clock would renew, so it is not one to warn about.
        if not any(
            _scope_skip_reason(
                station_id=scope["station_id"],
                season_bin=scope["season_bin"],
                lead_bucket=scope["lead_bucket"],
                system_ids=tuple(
                    str(value) for value in json.loads(
                        str(row["ensemble_system_ids_json"] or "[]")
                    )
                ),
                labels=_labels(scope["station_id"]),
            ) is None
            for row in _newest_cross_activation_per_pairing(
                conn,
                station_id=scope["station_id"],
                season_bin=scope["season_bin"],
                lead_bucket=scope["lead_bucket"],
                now_text=now_text,
            )
        ):
            continue
        unrenewed.append({
            "artifact_kind": CROSS_KIND,
            "ensemble_system_id_or_star": "*",
            **scope,
            "days_left": round(
                _days(now, scope["newest_valid_until_utc"]), 2
            ),
        })
    for scope in stalled_system_scopes(
        conn, now_text=now_text, horizon_text=horizon_text,
    ):
        if _scope_skip_reason(
            station_id=scope["station_id"], season_bin=scope["season_bin"],
            lead_bucket=scope["lead_bucket"],
            system_ids=(scope["ensemble_system_id"],),
            labels=_labels(scope["station_id"]),
        ):
            continue
        unrenewed.append({
            "artifact_kind": SYSTEM_KIND,
            "ensemble_system_id_or_star": scope["ensemble_system_id"],
            **scope,
            "days_left": round(
                _days(now, scope["newest_valid_until_utc"]), 2
            ),
        })
    unrenewed.sort(key=lambda row: row["days_left"])
    oldest_evidence = max(
        (row["evidence_age_days"] for row in serving), default=None,
    )
    soonest_end = min(
        (row["days_left"] for row in unrenewed), default=None,
    )
    if (
        (soonest_end is not None and soonest_end < EXPIRY_RED_DAYS)
        or (
            oldest_evidence is not None
            and oldest_evidence >= STALE_EVIDENCE_RED_DAYS
        )
    ):
        health = "red"
    elif serving or unrenewed:
        health = "amber"
    else:
        health = "green"
    notes: list[str] = []
    if serving:
        stations = sorted({row["station_id"] for row in serving})
        notes.append(
            f"{len(serving)} 个 scope 在用续期证书（证据最老 "
            f"{oldest_evidence:.0f} 天；{'、'.join(stations[:6])}"
            f"{' 等' if len(stations) > 6 else ''}）"
        )
    if unrenewed:
        notes.append(
            f"{len(unrenewed)} 个 scope 将在 {soonest_end:.1f} 天内到期且未续"
            "（续期车道没跑）"
        )
    if not renewals_known:
        notes.append("续期回执表尚未建立（第一次 --commit 时建）")
    return {
        "available": True,
        "health": health,
        "note": "；".join(notes) if notes else "证书全部新鲜，无续期在服务",
        "renewed_scope_count": len(serving),
        "recovered_scope_count": len(renewed) - len(serving),
        "unrenewed_scope_count": len(unrenewed),
        "oldest_renewed_evidence_days": oldest_evidence,
        "soonest_unrenewed_end_days": soonest_end,
        "renew_within_days": RENEW_WITHIN_DAYS,
        "valid_days": VALID_DAYS,
        "renewed": serving[:200],
        "unrenewed": unrenewed[:200],
    }


__all__ = [
    "ACTIVE_SYSTEM_IDS",
    "EXPIRY_RED_DAYS",
    "EXPIRY_WARN_DAYS",
    "RENEWAL_REASON",
    "RENEWAL_TABLE",
    "RENEW_WITHIN_DAYS",
    "STALE_EVIDENCE_RED_DAYS",
    "VALID_DAYS",
    "certificate_health",
    "plan_renewals",
    "renew_expiring_certificates",
    "served_labels",
    "stalled_cross_scopes",
    "stalled_system_scopes",
    "utc_text",
]
