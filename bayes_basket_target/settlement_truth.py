"""Resident finalization of official station-day settlement truth.

The market registry is the sole timezone authority.  This module reads the
already-canonical AMSC/AWC observation ledger, freezes a bounded daily maximum
only after the registry settlement window closes, and appends one
content-addressed truth revision to the target SSOT.  It never reads NWP,
probability, TAF, prices, orders, or a gateway.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import sqlite3
from typing import Any, Mapping, Sequence

from .contracts import (
    MAX_REPORT_TRANSMISSION_LEAD_SECONDS,
    ContractError,
    assert_timestamp_not_future,
    canonical_json,
    content_sha256,
)
from .canonical_contract import (
    CONTRACT_VERSION,
    CanonicalTruthFact,
    SCHEMA as CANONICAL_CONTRACT_SCHEMA,
)
from .canonical_ingress import (
    AWC_DAILY_TRUTH_PROJECTION_SCHEMA,
    ingest_canonical_contract,
)
from .correction_review import assert_station_day_corrections_resolved
from .resource_bounds import assert_process_memory_budget
from .settlement_time import settlement_day_window
from .source_policy import (
    source_policy_reason,
)


_MAX_TRANSMISSION_LEAD = timedelta(
    seconds=MAX_REPORT_TRANSMISSION_LEAD_SECONDS
)
SCHEMA = "bayes_basket_target_resident_settlement_truth_v1"
AUTHORITY_SCOPE = "FORMAL_NATIVE_ELIGIBLE"
CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1 = (
    "CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1"
)
SOURCE_ROLE = CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1
MAX_TRUTH_SCOPES = 45
# A settlement day holds 24 routine hourly reports; everything above that is
# SPECI, which a station issues as conditions demand rather than on a schedule.
# 64 was below what an ordinary busy day produces at the stations that report
# most often -- LTAC reached 90 and RCSS 79 over six weeks -- and because the
# cap fails the scope closed rather than trimming it, those station-days could
# never settle at all, at any later time.  This is a resource bound, not a
# statement about how many reports a day may contain, so it is set clear of
# observed traffic while still refusing a runaway scope.
MAX_OBSERVATIONS_PER_SCOPE = 256


def _utc(name: str, value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(
            str(value or "").replace("Z", "+00:00")
        )
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _canonical_stamp(value: datetime) -> str:
    """Use the canonical contract's normalized UTC representation."""

    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _current_canonical_awc_truths(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
) -> list[dict[str, Any]]:
    """Read formal revisions only from canonical SSOT, never its projection."""

    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='canonical_ssot_fact'"
    ).fetchone()
    if table is None:
        raise ContractError("settlement_truth_canonical_ssot_fact_missing")
    rows = conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact "
        "WHERE fact_type='truth' AND scope_key LIKE ? LIMIT ?",
        (f"{station_id}|{event_date}|%", MAX_TRUTH_SCOPES + 1),
    ).fetchall()
    if len(rows) > MAX_TRUTH_SCOPES:
        raise ContractError("settlement_truth_canonical_revision_cap_exceeded")
    facts: list[dict[str, Any]] = []
    revisions: set[int] = set()
    for row in rows:
        try:
            raw = json.loads(str(row[0]))
        except (TypeError, ValueError) as exc:
            raise ContractError("settlement_truth_canonical_fact_json_invalid") from exc
        if not isinstance(raw, Mapping):
            raise ContractError("settlement_truth_canonical_fact_mapping_invalid")
        try:
            fact = CanonicalTruthFact.from_mapping(raw)
        except ContractError as exc:
            raise ContractError("settlement_truth_canonical_fact_invalid") from exc
        if fact.station_id != station_id or fact.event_date != event_date:
            raise ContractError("settlement_truth_canonical_scope_mismatch")
        if (
            fact.source_lane != "AWC"
            or fact.source_role != CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1
            or fact.authority_scope != AUTHORITY_SCOPE
            or not fact.closed_day
        ):
            continue
        if fact.truth_revision_no in revisions:
            raise ContractError("settlement_truth_canonical_revision_ambiguous")
        revisions.add(fact.truth_revision_no)
        facts.append(fact.to_mapping())
    return facts


def _registry_scope(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    as_of_utc: str,
) -> tuple[str, str, str]:
    rows = conn.execute(
        "SELECT snapshot_sha256,settlement_timezone,source_lane,refreshed_at "
        "FROM basket_target_market_registry "
        "WHERE station_id=? AND event_date=? AND source_eligible=1 "
        "AND julianday(refreshed_at)<=julianday(?) "
        "ORDER BY refreshed_at DESC,snapshot_sha256 DESC",
        (station_id, event_date, as_of_utc),
    ).fetchall()
    if not rows:
        raise ContractError("settlement_truth_registry_scope_not_unique")
    registry_snapshot_sha = str(rows[0][0] or "").strip().lower()
    latest_rows = [
        row for row in rows
        if str(row[0] or "").strip().lower() == registry_snapshot_sha
    ]
    scopes = {
        (
            str(row[1] or "").strip(),
            str(row[2] or "").strip().upper(),
        )
        for row in latest_rows
    }
    if len(scopes) != 1:
        raise ContractError("settlement_truth_registry_scope_not_unique")
    if (
        len(registry_snapshot_sha) != 64
        or any(
            char not in "0123456789abcdef"
            for char in registry_snapshot_sha
        )
    ):
        raise ContractError("settlement_truth_registry_snapshot_sha_invalid")
    timezone_name, source_lane = next(iter(scopes))
    if not timezone_name:
        raise ContractError("settlement_truth_registry_timezone_missing")
    # AMSC is retained solely for offline historical comparison.  The formal
    # writer must never turn it into a truth row, even when an old registry
    # snapshot remains present.
    if source_lane != "AWC":
        raise ContractError("settlement_truth_formal_source_lane_must_be_awc")
    settlement_day_window(event_date, timezone_name)
    return timezone_name, source_lane, registry_snapshot_sha


def materialize_settled_station_truth(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    as_of_utc: str,
) -> dict[str, Any]:
    """Append one exact station-day truth revision, or deterministically skip."""

    station = str(station_id or "").strip().upper()
    day = str(event_date or "").strip()
    if not station or not day:
        raise ContractError("settlement_truth_scope_missing")
    as_of = _utc(
        "settlement_truth_as_of_utc",
        assert_timestamp_not_future(
            as_of_utc,
            name="settlement_truth_as_of_utc",
        ),
    )
    timezone_name, source_lane, registry_snapshot_sha = _registry_scope(
        target_conn,
        station_id=station,
        event_date=day,
        as_of_utc=_stamp(as_of),
    )
    window = settlement_day_window(day, timezone_name)
    start = _utc(
        "settlement_truth_window_start",
        window["target_day_start_utc"],
    )
    end = _utc(
        "settlement_truth_window_end",
        window["target_day_end_utc"],
    )
    if as_of < end:
        return {
            "schema": SCHEMA,
            "state": "skipped_open_settlement_day",
            "station_id": station,
            "event_date": day,
            "settlement_timezone": timezone_name,
            "settlement_closed_at_utc": _stamp(end),
            "wrote": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        }

    try:
        correction_review = assert_station_day_corrections_resolved(
            canonical_conn,
            source_lane=source_lane.lower(),
            station_id=station,
            event_date=day,
            window_start_utc=window["target_day_start_utc"],
            window_end_utc=window["target_day_end_utc"],
            as_of_utc=_stamp(as_of),
        )
    except ContractError as exc:
        raise ContractError(
            f"settlement_truth_correction_requires_manual_review:{exc}"
        ) from exc
    rows = canonical_conn.execute(
        "SELECT observation_id,report_type,valid_at,"
        "reported_temperature_c,raw_sha256,canonical_committed_at,"
        "correction_suspected "
        "FROM reported_trajectory_observations "
        "WHERE station_id=? AND source_lane=? "
        "AND julianday(valid_at)>=julianday(?) "
        "AND julianday(valid_at)<julianday(?) "
        "AND julianday(canonical_committed_at)<=julianday(?) "
        "ORDER BY valid_at,canonical_committed_at,observation_id LIMIT ?",
        (
            station,
            source_lane.lower(),
            window["target_day_start_utc"],
            window["target_day_end_utc"],
            _stamp(as_of),
            MAX_OBSERVATIONS_PER_SCOPE + 1,
        ),
    ).fetchall()
    if len(rows) > MAX_OBSERVATIONS_PER_SCOPE:
        raise ContractError("settlement_truth_observation_scope_cap_exceeded")
    hourly: list[dict[str, Any]] = []
    hourly_by_valid_at: dict[str, dict[str, Any]] = {}
    for row in rows:
        report_type = str(row[1] or "").strip().upper()
        if report_type not in {"METAR", "SPECI"}:
            raise ContractError("settlement_truth_report_type_invalid")
        valid_at = _utc("settlement_truth_valid_at", row[2])
        committed_at = _utc("settlement_truth_committed_at", row[5])
        # P1 and settlement have to admit the same physical set or the stage
        # is scored against evidence it was never allowed to read.  P1 allows
        # a report's nominal minute to lead its commit by a transmission
        # jitter, so this side allows exactly the same lead.
        if (
            not (start <= valid_at < end)
            or valid_at - committed_at > _MAX_TRANSMISSION_LEAD
        ):
            raise ContractError("settlement_truth_observation_time_invalid")
        if int(row[6] or 0) != 0:
            continue
        # The formal AWC lane is the same source-policy contract used by the
        # realtime ingress: every routine report at the provider's own
        # official minute is formal, and effective SPECI keeps its official
        # minute.  Providers publish on station-specific cadences -- :00 in
        # much of Asia, :20/:50 or :25/:55 in Europe, :51-:58 in the United
        # States, half-hourly extras at some hubs -- and all of them state
        # official temperatures for the settlement day, so tmax is the
        # maximum over all of them.  Any surviving policy reason is therefore
        # a genuine contract violation, never a cadence artifact.
        policy_reason = source_policy_reason(
            source_lane=source_lane.lower(),
            report_type=report_type,
            valid_at=valid_at,
            expected_lane="awc",
        )
        if policy_reason:
            raise ContractError(
                "settlement_truth_observation_source_policy_invalid:"
                + policy_reason
            )
        try:
            temperature_c = float(row[3])
        except (TypeError, ValueError) as exc:
            raise ContractError("settlement_truth_temperature_invalid") from exc
        if not math.isfinite(temperature_c):
            raise ContractError("settlement_truth_temperature_invalid")
        raw_sha = str(row[4] or "").strip().lower()
        if (
            len(raw_sha) != 64
            or any(char not in "0123456789abcdef" for char in raw_sha)
        ):
            raise ContractError("settlement_truth_raw_sha256_invalid")
        item = {
            "observation_id": str(row[0] or "").strip(),
            "report_type": report_type,
            "valid_at_utc": _stamp(valid_at),
            "canonical_committed_at_utc": _stamp(committed_at),
            "reported_temperature_c": temperature_c,
            "raw_sha256": raw_sha,
        }
        previous = hourly_by_valid_at.get(item["valid_at_utc"])
        if previous is not None:
            # The settled fact is the temperature: `official_tmax_c` below is
            # `max(reported_temperature_c)`, and `raw_sha256` only ever reaches
            # the audit payload.  Two reports for one valid time that agree on
            # temperature therefore state the same fact and cannot change the
            # truth, whichever is kept.
            #
            # Requiring the raw bytes to match as well treated ordinary METAR
            # behaviour as a contradiction: a COR revision, a transmission
            # truncation ("BKN20" for "BKN200") and the clean report all carry
            # the same 15/13 temperature group but hash differently.  That
            # rejected 292 scopes on raw bytes alone and 80 more on METAR vs
            # SPECI, fail-closing settled-truth ingest for every station -- the
            # 46-to-4 station collapse -- while exactly one scope disagreed on
            # temperature.
            #
            # A genuine disagreement still fails closed: the truth is not
            # guessable when two reports claim different temperatures.
            if (
                previous["reported_temperature_c"]
                == item["reported_temperature_c"]
            ):
                continue
            raise ContractError(
                "settlement_truth_hourly_revision_ambiguous"
            )
        hourly_by_valid_at[item["valid_at_utc"]] = item
        hourly.append(item)
    if not hourly or any(not item["observation_id"] for item in hourly):
        raise ContractError("settlement_truth_hourly_observation_missing")

    # Reports must cover the hours the maximum can occur in, or the "max over
    # what exists" is the max of the wrong part of the day.  LTAC 2026-08-01
    # settled at 17C from ten night reports -- the day's only evidence after a
    # capture outage -- while the model consensus stood eleven degrees higher;
    # neighbouring days read 26 and 32.  Nothing about the value was
    # implausible per report, so no per-report check can catch it: the defect
    # is the shape of the evidence, a day whose warm hours are simply absent.
    # Three formal reports across the local 10:00-19:00 window is lenient for
    # every reporting cadence in the network (hourly and half-hourly stations
    # carry 9-18); a day that cannot meet it stays open and settles when the
    # gap backfills inside AWC's retention, or never -- unsettled is the
    # honest answer to an unobserved afternoon.
    warm_start = _utc(
        "settlement_truth_warm_window_start",
        window["target_day_start_utc"],
    ) + timedelta(hours=10)
    warm_end = warm_start + timedelta(hours=9)
    warm_count = sum(
        1
        for item in hourly
        if warm_start <= _utc(
            "settlement_truth_warm_window_probe", item["valid_at_utc"]
        ) < warm_end
    )
    if warm_count < 3:
        raise ContractError(
            "settlement_truth_daytime_coverage_insufficient:"
            f"warm_window_reports={warm_count}"
        )

    evidence = {
        "schema": "settlement_truth_source_evidence_v1",
        "station_id": station,
        "event_date": day,
        "settlement_timezone": timezone_name,
        "registry_snapshot_sha256": registry_snapshot_sha,
        "source_lane": source_lane,
        "correction_review_ids": list(
            correction_review.get("review_ids") or []
        ),
        "settlement_window": {
            "start_utc": _stamp(start),
            "end_utc": _stamp(end),
        },
        "observations": hourly,
    }
    source_payload_sha = content_sha256(evidence)
    official_tmax_c = max(
        float(item["reported_temperature_c"]) for item in hourly
    )
    canonical_rows = _current_canonical_awc_truths(
        target_conn,
        station_id=station,
        event_date=day,
    )
    if any(
        _utc(
            "settlement_truth_canonical_committed_at",
            row["canonical_committed_at_utc"],
        ) > as_of
        for row in canonical_rows
    ):
        # A historical as-of replay must never append a revision beneath a
        # canonical revision that is already known to this SSOT but not to the
        # supplied decision time.  The caller must use an isolated historical
        # replay database instead.
        raise ContractError("settlement_truth_canonical_revision_after_asof")
    latest = max(
        canonical_rows,
        key=lambda row: int(row["truth_revision_no"]),
        default=None,
    )
    if (
        latest is not None
        and float(latest["truth_value"]) == official_tmax_c
        and latest["truth_content_sha256"] == source_payload_sha
    ):
        canonical_truth = dict(latest)
        idempotent_source = True
    else:
        revision_no = (
            0 if latest is None else int(latest["truth_revision_no"]) + 1
        )
        truth_seed = {
            "station_id": station,
            "event_date": day,
            "truth_revision_no": revision_no,
            "truth_content_sha256": source_payload_sha,
        }
        truth_id = "truth_" + content_sha256(truth_seed)[:40]
        canonical_identity = {
            "truth_id": truth_id,
            "station_id": station,
            "event_date": day,
            "truth_revision_no": revision_no,
            "truth_value": official_tmax_c,
            "truth_content_sha256": source_payload_sha,
            "canonical_committed_at_utc": _canonical_stamp(as_of),
            "station_day_end_utc": _canonical_stamp(end),
            "closed_day": True,
            "source_lane": "AWC",
            "source_role": CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1,
            "authority_scope": AUTHORITY_SCOPE,
            "settlement_timezone": timezone_name,
        }
        canonical_truth = {
            **canonical_identity,
            "content_sha256": content_sha256(canonical_identity),
        }
        idempotent_source = False

    published_at = _canonical_stamp(
        max(
            _utc(
                "settlement_truth_published_at",
                item["valid_at_utc"],
            )
            for item in hourly
        )
    )
    received_at = _canonical_stamp(
        max(
            _utc(
                "settlement_truth_received_at",
                item["canonical_committed_at_utc"],
            )
            for item in hourly
        )
    )
    if published_at > received_at:
        raise ContractError("settlement_truth_source_timestamp_order_invalid")
    legacy_identity = {
        "truth_id": canonical_truth["truth_id"],
        "station_id": canonical_truth["station_id"],
        "local_date": canonical_truth["event_date"],
        "settlement_timezone": canonical_truth["settlement_timezone"],
        "official_tmax_c": canonical_truth["truth_value"],
        "source_lane": canonical_truth["source_lane"],
        "settlement_definition": "official_daily_tmax_c",
        "revision_no": canonical_truth["truth_revision_no"],
        "published_at_utc": published_at,
        "received_at_utc": received_at,
        "canonical_committed_at_utc": canonical_truth[
            "canonical_committed_at_utc"
        ],
        "timestamps_complete": 1,
        "qc_status": "ACCEPTED",
        "source_role": canonical_truth["source_role"],
        "source_payload_sha256": canonical_truth["truth_content_sha256"],
        # The legacy projection keeps bounded source evidence only.  The
        # canonical fact independently pins its source payload hash, role and
        # settlement timezone; neither side may be reconstructed from the
        # other by a reader.
        "observation_ids_json": canonical_json(evidence),
        "authority_scope": canonical_truth["authority_scope"],
    }
    projection_row = {
        **canonical_truth,
        "settlement_definition": legacy_identity["settlement_definition"],
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
        "as_of_utc": _canonical_stamp(as_of),
        "builder_id": "awc.closed_day.truth.v1",
        "model_facts": [],
        "ensemble_member_facts": [],
        "native_arrival_facts": [],
        "ensemble_member_receipt_manifest_facts": [],
        "ensemble_member_arrival_binding_facts": [],
        "truth_facts": [canonical_truth],
        "residual_facts": [],
        "activation_facts": [],
    }
    canonical_packet = {
        **contract_identity,
        "content_sha256": content_sha256(contract_identity),
    }
    caller_owns_transaction = target_conn.in_transaction
    ingress = ingest_canonical_contract(
        target_conn,
        canonical_packet,
        commit=True,
        finalize_transaction=not caller_owns_transaction,
        awc_daily_truth_projection={
            "schema": AWC_DAILY_TRUTH_PROJECTION_SCHEMA,
            "truth_rows": [projection_row],
        },
    )
    projection = ingress["awc_daily_truth_projection"]
    wrote = bool(
        ingress["inserted_count"] or projection["inserted_count"]
    )
    assert_process_memory_budget()
    return {
        "schema": SCHEMA,
        "state": (
            "idempotent"
            if idempotent_source and not wrote
            else "staged_in_caller_transaction"
            if caller_owns_transaction
            else "committed"
        ),
        "transaction_state": ingress["state"],
        "station_id": station,
        "event_date": day,
        "settlement_timezone": timezone_name,
        "truth_id": canonical_truth["truth_id"],
        "revision_no": canonical_truth["truth_revision_no"],
        "official_tmax_c": canonical_truth["truth_value"],
        "content_sha256": projection_row["content_sha256"],
        "canonical_truth_content_sha256": canonical_truth["content_sha256"],
        "canonical_fact_id": "truth:" + canonical_truth["content_sha256"],
        "wrote": wrote,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }


# Failures that mean "this station-day has nothing to settle", as opposed to
# "what this station-day contains cannot be trusted".
#
# The distinction decides whether the batch cursor may advance.  A scope with no
# eligible hourly report is simply not settleable -- the AWC feed had a gap on
# that day -- and retrying it forever cannot change that.  126 such station-days
# accumulated from 2026-08-02, filled the 45-scope batch window, and every batch
# came back all-failed; the caller then declined to advance the cursor, so the
# same doomed scopes were reselected on every cycle and no later station-day was
# ever reached.  Settled truth for all 45 stations stopped, and with it P1 and
# the METAR trajectory training folds.
#
# Anything not listed here is treated as a consistency failure and still stops
# the batch.  That default is deliberate: a new error code should have to be
# classified on purpose, because silently skipping an unrecognised one is how a
# real contradiction would get walked past.
AVAILABILITY_BLOCKER_REASONS = frozenset({
    "settlement_truth_hourly_observation_missing",
    "settlement_truth_canonical_ssot_fact_missing",
    "settlement_truth_scope_missing",
})


def blocker_class(reason: str) -> str:
    """Whether a blocker is about availability or about trust."""

    code = str(reason or "").split(":", 1)[-1].strip()
    for candidate in (str(reason or "").strip(), code):
        head = candidate.split(":", 1)[0].strip()
        if head in AVAILABILITY_BLOCKER_REASONS:
            return "AVAILABILITY"
    return "CONSISTENCY"


def materialize_settled_training_truths(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection,
    *,
    as_of_utc: str,
    max_scopes: int = MAX_TRUTH_SCOPES,
    after_fact_rowid: int = 0,
    additional_scopes: Sequence[tuple[str, str]] = (),
) -> dict[str, Any]:
    """Finalize one bounded page of downstream and bootstrap truth scopes.

    A fixed ``SELECT DISTINCT ... LIMIT`` either blocks as soon as history
    exceeds the station cap or repeatedly scans the oldest scopes forever.
    The fact-rowid cursor makes the projection bounded and eventually revisits
    every historical scope, including a scope that receives a later fact.
    ``additional_scopes`` lets an upstream forward-evidence owner bootstrap
    truth before the first basket training fact exists; this module still
    receives only station/date identities and never reads model evidence.
    """

    if not 1 <= int(max_scopes) <= MAX_TRUTH_SCOPES:
        raise ContractError("settlement_truth_scope_limit_invalid")
    if (
        isinstance(after_fact_rowid, bool)
        or int(after_fact_rowid) < 0
    ):
        raise ContractError("settlement_truth_cursor_invalid")
    cursor = int(after_fact_rowid)
    supplemental: list[tuple[str, str]] = []
    supplemental_seen: set[tuple[str, str]] = set()
    if (
        not isinstance(additional_scopes, Sequence)
        or isinstance(additional_scopes, (str, bytes))
        or len(additional_scopes) > int(max_scopes)
    ):
        raise ContractError("settlement_truth_additional_scope_count_invalid")
    for raw_scope in additional_scopes:
        if (
            not isinstance(raw_scope, Sequence)
            or isinstance(raw_scope, (str, bytes))
            or len(raw_scope) != 2
        ):
            raise ContractError("settlement_truth_additional_scope_invalid")
        scope = (
            str(raw_scope[0] or "").strip().upper(),
            str(raw_scope[1] or "").strip(),
        )
        if not all(scope):
            raise ContractError("settlement_truth_additional_scope_invalid")
        if scope in supplemental_seen:
            continue
        supplemental_seen.add(scope)
        supplemental.append(scope)

    def _page(
        after_rowid: int,
        page_limit: int,
    ) -> list[tuple[Any, Any, Any]]:
        if page_limit <= 0:
            return []
        return target_conn.execute(
            "SELECT station_id,event_date,max(rowid) AS source_rowid "
            "FROM basket_policy_training_fact WHERE rowid>? "
            "GROUP BY station_id,event_date "
            "ORDER BY source_rowid,station_id,event_date LIMIT ?",
            (after_rowid, int(page_limit)),
        ).fetchall()

    remaining = int(max_scopes) - len(supplemental)
    # Read enough fact scopes to fill the residual page even when some are
    # duplicates of bootstrap scopes, plus one row to preserve has_more.
    fact_probe_limit = remaining + len(supplemental_seen) + 1
    rows = _page(cursor, fact_probe_limit)
    wrapped = False
    if not rows and cursor > 0:
        rows = _page(0, fact_probe_limit)
        wrapped = True
    selected: list[tuple[Any, Any, Any]] = []
    for row in rows:
        scope = (
            str(row[0] or "").strip().upper(),
            str(row[1] or "").strip(),
        )
        if scope in supplemental_seen:
            continue
        if len(selected) >= remaining:
            break
        selected.append(row)
    consumed_rowids = {
        int(row[2])
        for row in selected
    }
    has_more = any(
        int(row[2]) not in consumed_rowids
        and (
            str(row[0] or "").strip().upper(),
            str(row[1] or "").strip(),
        )
        not in supplemental_seen
        for row in rows
    )
    fact_scopes = [
        (
            str(row[0] or "").strip().upper(),
            str(row[1] or "").strip(),
        )
        for row in selected
    ]
    scopes = supplemental + fact_scopes
    next_source_rowid = (
        max(int(row[2]) for row in selected)
        if selected
        else cursor
    )
    results: list[dict[str, Any]] = []
    blockers: list[dict[str, str]] = []
    for scope_index, (station_id, event_date) in enumerate(scopes):
        savepoint = f"settlement_truth_scope_{scope_index}"
        target_conn.execute(f"SAVEPOINT {savepoint}")
        try:
            result = materialize_settled_station_truth(
                target_conn,
                canonical_conn,
                station_id=str(station_id),
                event_date=str(event_date),
                as_of_utc=as_of_utc,
            )
            target_conn.execute(f"RELEASE {savepoint}")
            results.append(result)
        except Exception as exc:
            target_conn.execute(f"ROLLBACK TO {savepoint}")
            target_conn.execute(f"RELEASE {savepoint}")
            reason = f"{type(exc).__name__}:{exc}"
            blockers.append({
                "station_id": str(station_id),
                "event_date": str(event_date),
                "reason": reason,
                "blocker_class": blocker_class(reason),
            })
        assert_process_memory_budget()
    return {
        "schema": "bayes_basket_target_resident_settlement_truth_batch_v1",
        "state": (
            "partial_fail_closed"
            if blockers and results
            else "blocked_fail_closed"
            if blockers
            else "complete"
        ),
        # True when nothing in this batch was a trust failure, so the caller may
        # record the gaps and move past them instead of reselecting them.
        "advance_safe": not any(
            item.get("blocker_class") != "AVAILABILITY" for item in blockers
        ),
        "scope_count": len(scopes),
        "additional_scope_count": len(supplemental),
        "training_fact_scope_count": len(fact_scopes),
        "written_count": sum(bool(item.get("wrote")) for item in results),
        "source_cursor_before": cursor,
        "source_cursor_after": next_source_rowid,
        "cursor_wrapped": wrapped,
        "has_more": has_more,
        "max_scopes": int(max_scopes),
        "results": results,
        "blockers": blockers,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }


__all__ = [
    "AUTHORITY_SCOPE",
    "AVAILABILITY_BLOCKER_REASONS",
    "CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1",
    "MAX_OBSERVATIONS_PER_SCOPE",
    "MAX_TRUTH_SCOPES",
    "SCHEMA",
    "blocker_class",
    "materialize_settled_station_truth",
    "materialize_settled_training_truths",
]
