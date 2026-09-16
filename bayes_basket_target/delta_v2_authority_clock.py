"""The delta-v2 execution authority's clock: validity, renewal, hand-off.

Every candidate production prices resolves two dated things: a policy
activation (``resolve_delta_v2_policy``) and a fill distribution
(``resolve_delta_v2_fill_distribution``).  Both refuse outside
``valid_from <= at < valid_until``, and both were minted with thirty days of
validity by scripts nothing re-ran on a clock:

* the fill batch fitted 2026-08-22T04:37:38Z ends 2026-09-21T04:37:38Z, and
  no job refits or renews it;
* the policy re-pin runs daily but skips a scope whose pin is already
  current, so the activations it minted on 08-28 and 08-31 end 09-27 and
  09-30 with nothing extending them.

Either end refuses every candidate at every station
(``delta_v2_fill_distribution_not_active``, ``delta_v2_activation_missing``).
Found 2026-09-17, four days before the first.

Three rules, kept here so the fitting script, the re-pin and the board read
one definition:

1. Validity is a year.  The thirty days were never a statement about how long
   a fit stays true -- keeping the numbers fresh is the refit cadence's job --
   they were a date that halts production when nobody is watching.
2. A scope inside RENEW_WITHIN_DAYS of its end is renewed: the same content
   under a new window.  A renewed fill batch carries exactly the distribution
   hashes it copied, so its batch fingerprint is the one the policies already
   pin and the numbers production executes against do not move.
3. A refit hands off at one instant: the new fill batch and the re-pinned
   policy generation both start at the same ``valid_from``, so there is no
   window in which the resolver picks the new batch while the policy still
   pins the old one (the 2026-08-22..28 outage).  The start sits
   HANDOFF_LEAD_MINUTES ahead so the rows are all committed before any
   reader can resolve them -- a batch read half-written would cache the
   wrong fingerprint in that process for its lifetime.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from .basket_kernel.contracts import content_sha256
from .delta_v2_artifact import (
    ACTIVATION_SCHEMA,
    NO_LIVE_ACTIVATION_DECISION,
    PROMOTION_SCHEMA,
    commit_delta_v2_authority,
)
from .delta_v2_fill_artifact import (
    TABLE_ARTIFACT,
    DeltaV2FillDistributionArtifact,
    _fill_batch_fingerprint,
)

VALIDITY_DAYS = 365
RENEW_WITHIN_DAYS = 60
HANDOFF_LEAD_MINUTES = 30
# The board's two questions.  An authority ending inside a month is amber and
# inside a week red: the daily lane renews at sixty days, so either reading
# means that lane has not run for a month.  A scope that has had no new fill
# batch for two weeks missed two weekly refits.
EXPIRY_WARN_DAYS = 30
EXPIRY_RED_DAYS = 7
REFIT_EVERY_DAYS = 7
REFIT_STALE_DAYS = 14
# One price every minted grid carries (0.001..0.999 per tick), so the board
# reads a batch's window from a handful of index rows instead of walking the
# ~80k rows of a scope.  The renewal and the re-pin do not use it: they must
# see exactly the batch the resolver sees.
PROBE_PRICE = 0.5
RENEWAL_ID_PREFIX = "delta-v2-fill-renewal-"
FILL_PROMOTION_DECISION = "NO_LIVE_ACTIVATED"
_WRITE_CHUNK = 20_000

FILL_COLUMNS = (
    "artifact_id", "content_sha256", "schema", "station_id", "stage",
    "season_bin", "maturity_bin", "liquidity_bin", "bucket", "price",
    "timeout_seconds", "queue_ahead_min", "queue_ahead_max", "spread_min",
    "spread_max", "depth_min", "depth_max", "quote_version_scope",
    "oos_gate_version", "oos_gate_passed", "consecutive_forward_windows",
    "compared_baselines", "oos_metrics", "sample_n", "holdout_n",
    "trained_through_utc", "valid_from_utc", "valid_until_utc",
    "authority_scope", "fill_distribution_artifact_sha256",
    "maker_execution_costs", "fok_execution_costs", "artifact_json",
)


def utc_text(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _moment(text: Any) -> datetime:
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def days_between(start: datetime, end_text: Any) -> float:
    return (_moment(end_text) - start).total_seconds() / 86_400.0


def latest_policy_activations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The newest generation of every policy scope, as plain dicts."""

    cursor = conn.execute(
        "SELECT * FROM basket_delta_v2_policy_activation AS outer"
        " WHERE generation = ("
        "   SELECT MAX(generation) FROM basket_delta_v2_policy_activation"
        "   WHERE station_id=outer.station_id AND stage=outer.stage"
        "     AND season_bin=outer.season_bin"
        "     AND maturity_bin=outer.maturity_bin"
        "     AND liquidity_bin=outer.liquidity_bin"
        "     AND strategy_version=outer.strategy_version"
        "     AND authority_scope=outer.authority_scope)"
        " ORDER BY station_id, maturity_bin, strategy_version"
    )
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def resolver_fill_batch(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    stage: str,
    season_bin: str,
    maturity_bin: str,
    liquidity_bin: str,
    authority_scope: str,
    evaluation_at_utc: str,
    probe_price: float | None = None,
) -> dict[str, str] | None:
    """The fill batch the resolver picks at ``evaluation_at_utc``.

    Mirrors the resolver's preference order -- most specific scope first,
    then newest valid_from -- because a pin has to name the batch the
    finalist will actually belong to, not merely the newest batch in sight.
    ``probe_price`` narrows the read to one grid price for the board.
    """

    probe = "" if probe_price is None else " AND bucket='*' AND price=?"
    params: list[Any] = [
        station_id, stage, season_bin, maturity_bin, liquidity_bin,
        authority_scope,
    ]
    if probe_price is not None:
        params.append(float(probe_price))
    params += [evaluation_at_utc, evaluation_at_utc]
    best: tuple[int, str, str, str, str] | None = None
    for batch_station, batch_from, batch_until, trained in conn.execute(
        f"SELECT DISTINCT station_id, valid_from_utc, valid_until_utc,"
        f" trained_through_utc FROM {TABLE_ARTIFACT}"
        " WHERE station_id IN (?, '*') AND stage IN (?, '*')"
        "   AND season_bin=? AND maturity_bin=? AND liquidity_bin=?"
        f"   AND authority_scope=?{probe}"
        "   AND valid_from_utc<=? AND valid_until_utc>?",
        params,
    ):
        specificity = 0 if str(batch_station) == "*" else 1
        key = (
            specificity, str(batch_from), str(batch_station),
            str(batch_until), str(trained),
        )
        if best is None or key > best:
            best = key
    if best is None:
        return None
    return {
        "station_id": best[2],
        "valid_from_utc": best[1],
        "valid_until_utc": best[3],
        "trained_through_utc": best[4],
    }


def _scope_of(activation: Mapping[str, Any]) -> dict[str, str]:
    return {
        "station_id": str(activation["station_id"]),
        "stage": str(activation["stage"]),
        "season_bin": str(activation["season_bin"]),
        "maturity_bin": str(activation["maturity_bin"]),
        "liquidity_bin": str(activation["liquidity_bin"]),
        "authority_scope": str(activation["authority_scope"]),
    }


# --- fill batches ----------------------------------------------------------


def fill_row_values(artifact: Mapping[str, Any]) -> tuple[Any, ...]:
    """One artifact as the stored columns, in FILL_COLUMNS order."""

    return (
        artifact["artifact_id"], artifact["content_sha256"],
        artifact["schema"], artifact["station_id"], artifact["stage"],
        artifact["season_bin"], artifact["maturity_bin"],
        artifact["liquidity_bin"], artifact["bucket"], artifact["price"],
        artifact["timeout_seconds"], artifact["queue_ahead_min"],
        artifact["queue_ahead_max"], artifact["spread_min"],
        artifact["spread_max"], artifact["depth_min"], artifact["depth_max"],
        artifact["quote_version_scope"], artifact["oos_gate_version"],
        1 if artifact["oos_gate_passed"] else 0,
        artifact["consecutive_forward_windows"],
        json.dumps(artifact["compared_baselines"], sort_keys=True),
        json.dumps(artifact["oos_metrics"], sort_keys=True),
        artifact["sample_n"], artifact["holdout_n"],
        artifact["trained_through_utc"], artifact["valid_from_utc"],
        artifact["valid_until_utc"], artifact["authority_scope"],
        artifact["fill_distribution_artifact_sha256"],
        json.dumps(artifact["maker_execution_costs"], sort_keys=True),
        json.dumps(artifact["fok_execution_costs"], sort_keys=True),
        json.dumps(dict(artifact), sort_keys=True),
    )


def _fill_promotion_values(
    artifact: Mapping[str, Any], promoted_at: str
) -> tuple[Any, ...]:
    return (
        "delta-v2-fill-no-live-" + str(artifact["artifact_id"]),
        artifact["artifact_id"], artifact["content_sha256"], 1,
        FILL_PROMOTION_DECISION, artifact["oos_gate_version"], promoted_at,
        content_sha256({
            "artifact_id": artifact["artifact_id"],
            "artifact_content_sha256": artifact["content_sha256"],
            "decision": FILL_PROMOTION_DECISION,
            "gate_version": artifact["oos_gate_version"],
            "decided_at_utc": promoted_at,
        }),
        content_sha256({
            "promotion_of": artifact["content_sha256"],
            "decided_at_utc": promoted_at,
        }),
        promoted_at,
    )


def write_fill_batch(
    conn: sqlite3.Connection,
    artifacts: Sequence[Mapping[str, Any]],
    *,
    promoted_at: str,
) -> dict[str, int]:
    """Append a batch and its no-live promotions, committing in chunks.

    Chunked so the write lock is never held for the whole batch.  That is
    only safe because every caller starts the batch in the future: a reader
    resolving at the present cannot select rows whose ``valid_from`` has not
    arrived, so a half-written batch is invisible until it is whole.

    An artifact the resolver cannot promote is one it refuses
    (``delta_v2_fill_distribution_not_promoted``), so each row is written
    with its promotion, and the gate version on the receipt is the
    artifact's own.
    """

    before = conn.total_changes
    artifact_sql = (
        f"INSERT OR IGNORE INTO {TABLE_ARTIFACT} ({','.join(FILL_COLUMNS)})"
        f" VALUES ({','.join('?' * len(FILL_COLUMNS))})"
    )
    promotion_sql = (
        "INSERT OR IGNORE INTO basket_delta_v2_fill_promotion ("
        "promotion_id,artifact_id,artifact_content_sha256,decision_seq,"
        "decision,gate_version,decided_at_utc,receipt_sha256,"
        "content_sha256,created_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?)"
    )
    for offset in range(0, len(artifacts), _WRITE_CHUNK):
        chunk = artifacts[offset:offset + _WRITE_CHUNK]
        conn.executemany(artifact_sql, [fill_row_values(a) for a in chunk])
        conn.executemany(
            promotion_sql,
            [_fill_promotion_values(a, promoted_at) for a in chunk],
        )
        conn.commit()
    return {
        "artifacts": len(artifacts),
        "rows_written": conn.total_changes - before,
    }


def renewed_fill_batch(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    stage: str,
    season_bin: str,
    maturity_bin: str,
    liquidity_bin: str,
    authority_scope: str,
    valid_from_utc: str,
    starts_at: datetime,
    now: datetime,
) -> list[dict[str, Any]]:
    """The batch copied under a new window, every row re-validated.

    Content is untouched apart from the window and the id, so the set of
    distribution hashes -- and with it the batch fingerprint the policies
    pin -- is the one being renewed.  ``trained_through`` stays the fit's
    own: a renewal is not a refit and must not read like one.
    """

    new_from = utc_text(starts_at)
    new_until = utc_text(now + timedelta(days=VALIDITY_DAYS))
    renewed: list[dict[str, Any]] = []
    for (payload,) in conn.execute(
        f"SELECT artifact_json FROM {TABLE_ARTIFACT}"
        " WHERE station_id=? AND stage=? AND season_bin=? AND maturity_bin=?"
        "   AND liquidity_bin=? AND authority_scope=? AND valid_from_utc=?",
        (
            station_id, stage, season_bin, maturity_bin, liquidity_bin,
            authority_scope, valid_from_utc,
        ),
    ):
        body = dict(json.loads(str(payload)))
        body.pop("content_sha256", None)
        previous = str(body["artifact_id"])
        body["valid_from_utc"] = new_from
        body["valid_until_utc"] = new_until
        body["artifact_id"] = RENEWAL_ID_PREFIX + content_sha256({
            "renewal_of": previous, "valid_from_utc": new_from,
        })[:24]
        body["content_sha256"] = content_sha256(body)
        DeltaV2FillDistributionArtifact.from_mapping(body)
        renewed.append(body)
    return renewed


def renew_expiring_fill_batches(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    commit: bool,
    within_days: float = RENEW_WITHIN_DAYS,
    log: Callable[[str], None] = print,
) -> dict[str, int]:
    """Renew every served fill batch inside ``within_days`` of its end."""

    evaluation = utc_text(now)
    starts_at = now + timedelta(minutes=HANDOFF_LEAD_MINUTES)
    seen: set[tuple[str, ...]] = set()
    renewed = skipped = rows = 0
    for activation in latest_policy_activations(conn):
        scope = _scope_of(activation)
        batch = resolver_fill_batch(
            conn, **scope, evaluation_at_utc=evaluation
        )
        if batch is None:
            log(f"NO-BATCH {tuple(scope.values())}: nothing active to renew")
            skipped += 1
            continue
        batch_scope = {**scope, "station_id": batch["station_id"]}
        key = (*batch_scope.values(), batch["valid_from_utc"])
        if key in seen:
            continue
        seen.add(key)
        left = days_between(now, batch["valid_until_utc"])
        if left >= within_days:
            skipped += 1
            continue
        pending = conn.execute(
            f"SELECT 1 FROM {TABLE_ARTIFACT}"
            " WHERE station_id=? AND stage=? AND season_bin=?"
            "   AND maturity_bin=? AND liquidity_bin=? AND authority_scope=?"
            "   AND valid_from_utc>? AND valid_until_utc>? LIMIT 1",
            (*batch_scope.values(), batch["valid_from_utc"], evaluation),
        ).fetchone()
        if pending is not None:
            log(f"PENDING {key}: a later batch already follows this one")
            skipped += 1
            continue
        copies = renewed_fill_batch(
            conn, **batch_scope, valid_from_utc=batch["valid_from_utc"],
            starts_at=starts_at, now=now,
        )
        log(
            f"{'RENEW' if commit else 'WOULD-RENEW'} {key}"
            f" {len(copies)} 行，剩 {left:.1f} 天"
            f" → {utc_text(starts_at)} 起一年"
        )
        if commit and copies:
            rows += write_fill_batch(
                conn, copies, promoted_at=evaluation
            )["rows_written"]
        renewed += 1
    return {"renewed": renewed, "skipped": skipped, "rows_written": rows}


def newest_fill_batch_start(
    conn: sqlite3.Connection, *, now: datetime
) -> str | None:
    """The stalest scope's newest unexpired batch start (the refit gate)."""

    evaluation = utc_text(now)
    starts: list[str] = []
    for activation in latest_policy_activations(conn):
        scope = _scope_of(activation)
        row = conn.execute(
            f"SELECT MAX(valid_from_utc) FROM {TABLE_ARTIFACT}"
            " WHERE station_id IN (?, '*') AND stage IN (?, '*')"
            "   AND season_bin=? AND maturity_bin=? AND liquidity_bin=?"
            "   AND authority_scope=? AND bucket='*' AND price=?"
            "   AND valid_until_utc>?",
            (*scope.values(), PROBE_PRICE, evaluation),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        starts.append(str(row[0]))
    return min(starts) if starts else None


# --- policy generations ----------------------------------------------------


def repin_policies(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    commit: bool,
    handoff_at: datetime | None = None,
    renew_within_days: float = RENEW_WITHIN_DAYS,
    log: Callable[[str], None] = print,
) -> dict[str, int]:
    """Mint the next generation of every policy whose pin or window is stale.

    A generation is minted when the pin no longer names the batch the
    resolver picks, or when the newest generation ends inside
    ``renew_within_days``.  The second condition is new (2026-09-17): the pin
    check alone never extended a window, so a pin that stayed current let its
    thirty days run out.

    ``handoff_at`` is the refit's instant: the pin is read at it and the new
    generation starts at it, the same instant the new fill batch starts.
    """

    stamp = utc_text(now)
    evaluation = utc_text(handoff_at or now)
    valid_from = utc_text(handoff_at or now + timedelta(minutes=1))
    valid_until = utc_text(now + timedelta(days=VALIDITY_DAYS))
    minted = skipped = renewed = repinned = 0
    for activation in latest_policy_activations(conn):
        scope = _scope_of(activation)
        label = (*tuple(scope.values())[:5], str(activation["strategy_version"]))
        artifact_row = conn.execute(
            "SELECT artifact_json FROM basket_delta_v2_policy_artifact"
            " WHERE artifact_id=?",
            (activation["artifact_id"],),
        ).fetchone()
        if artifact_row is None:
            log(f"SKIP {label}: activation names a missing artifact")
            skipped += 1
            continue
        batch = resolver_fill_batch(
            conn, **scope, evaluation_at_utc=evaluation
        )
        if batch is None:
            log(f"SKIP {label}: no active fill batch to pin")
            skipped += 1
            continue
        # The fingerprint is over the batch's own scope -- the station the
        # winning rows actually carry -- exactly as the resolver computes it
        # for the finalist.
        fingerprint = _fill_batch_fingerprint(
            conn,
            station_id=batch["station_id"],
            stage=scope["stage"],
            season_bin=scope["season_bin"],
            maturity_bin=scope["maturity_bin"],
            liquidity_bin=scope["liquidity_bin"],
            valid_from_utc=batch["valid_from_utc"],
            authority_scope=scope["authority_scope"],
        )
        payload = json.loads(str(artifact_row[0]))
        pin_current = (
            str(payload.get("fill_distribution_artifact_sha256")) == fingerprint
        )
        left = days_between(
            handoff_at or now, activation["valid_until_utc"]
        )
        if pin_current and left >= renew_within_days:
            skipped += 1
            continue
        updated = dict(payload)
        updated["fill_distribution_artifact_sha256"] = fingerprint
        updated["trained_through_utc"] = stamp
        updated["valid_from_utc"] = valid_from
        updated["valid_until_utc"] = valid_until
        updated.pop("content_sha256", None)
        base = str(activation["artifact_id"]).split("-shadow")[0]
        base = base.split("-repin")[0]
        identity = content_sha256({
            key: value
            for key, value in updated.items()
            if key not in {
                "artifact_id", "valid_from_utc", "trained_through_utc",
            }
        })
        updated["artifact_id"] = base + "-repin" + identity[:12]
        updated["content_sha256"] = content_sha256(updated)
        promotion = {
            "schema": PROMOTION_SCHEMA,
            "promotion_id": "delta-v2-policy-no-live-" + updated["artifact_id"],
            "artifact_id": updated["artifact_id"],
            "decision_seq": 1,
            "decision": NO_LIVE_ACTIVATION_DECISION,
            "gate_version": updated["oos_gate_version"],
            "decided_at_utc": stamp,
            "receipt_sha256": content_sha256({
                "artifact_id": updated["artifact_id"],
                "artifact_content_sha256": updated["content_sha256"],
                "decision": NO_LIVE_ACTIVATION_DECISION,
                "gate_version": updated["oos_gate_version"],
                "decided_at_utc": stamp,
            }),
        }
        promotion["content_sha256"] = content_sha256(promotion)
        activation_payload = {
            "schema": ACTIVATION_SCHEMA,
            "activation_id": (
                str(activation["activation_id"]).split("-shadow")[0]
                .split("-repin")[0] + "-repin" + identity[:12]
            ),
            "artifact_id": updated["artifact_id"],
            "station_id": activation["station_id"],
            "stage": activation["stage"],
            "season_bin": activation["season_bin"],
            "maturity_bin": activation["maturity_bin"],
            "liquidity_bin": activation["liquidity_bin"],
            "strategy_version": activation["strategy_version"],
            "generation": int(activation["generation"]) + 1,
            "valid_from_utc": valid_from,
            "valid_until_utc": valid_until,
            "activated_at_utc": stamp,
            "authority_scope": activation["authority_scope"],
        }
        activation_payload["content_sha256"] = content_sha256(
            activation_payload
        )
        why = "pin" if not pin_current else f"renew(剩 {left:.1f} 天)"
        log(
            f"{'MINT' if commit else 'WOULD-MINT'} {label} {why}"
            f" gen {activation['generation']}→{activation_payload['generation']}"
            f" batch=({batch['station_id']},{batch['valid_from_utc']})"
            f" pin={fingerprint[:16]}… 窗口 {valid_from} → {valid_until}"
        )
        if commit:
            receipt = commit_delta_v2_authority(
                conn,
                artifact=updated,
                promotion=promotion,
                activation=activation_payload,
            )
            if receipt.get("state") != "COMMITTED":
                log(f"  !! commit state={receipt.get('state')}")
                skipped += 1
                continue
        minted += 1
        if pin_current:
            renewed += 1
        else:
            repinned += 1
    if commit:
        conn.commit()
    return {
        "minted": minted, "skipped": skipped,
        "renewed": renewed, "repinned": repinned,
    }


# --- the board -------------------------------------------------------------


def authority_health(
    conn: sqlite3.Connection, *, now: datetime
) -> dict[str, Any]:
    """How long production can still resolve its execution authority.

    Red when any scope ends inside a week or resolves nothing now; amber when
    one ends inside a month or has had no new fill batch for two weeks.
    """

    evaluation = utc_text(now)
    fill: list[dict[str, Any]] = []
    policy: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    notes: list[str] = []
    red = amber = False
    for activation in latest_policy_activations(conn):
        scope = _scope_of(activation)
        left = days_between(now, activation["valid_until_utc"])
        policy.append({
            "station_id": scope["station_id"],
            "maturity_bin": scope["maturity_bin"],
            "strategy_version": str(activation["strategy_version"]),
            "generation": int(activation["generation"]),
            "valid_until_utc": str(activation["valid_until_utc"]),
            "days_left": round(left, 1),
        })
        batch = resolver_fill_batch(
            conn, **scope, evaluation_at_utc=evaluation,
            probe_price=PROBE_PRICE,
        )
        key = (*scope.values(), str((batch or {}).get("valid_from_utc")))
        if key in seen:
            continue
        seen.add(key)
        if batch is None:
            fill.append({**scope, "active": False})
            red = True
            notes.append(f"{scope['station_id']}/{scope['maturity_bin']} 没有生效的成交概率批次")
            continue
        newest = conn.execute(
            f"SELECT MAX(valid_from_utc) FROM {TABLE_ARTIFACT}"
            " WHERE station_id IN (?, '*') AND stage IN (?, '*')"
            "   AND season_bin=? AND maturity_bin=? AND liquidity_bin=?"
            "   AND authority_scope=? AND bucket='*' AND price=?"
            "   AND valid_until_utc>?",
            (*scope.values(), PROBE_PRICE, evaluation),
        ).fetchone()[0]
        # A successor that starts before the served batch ends carries the
        # scope on; the end that matters is the successor's.  Right after a
        # renewal or a refit the served batch is still the old one for
        # HANDOFF_LEAD_MINUTES, and reading its end would call a covered
        # scope red.
        covered_until = conn.execute(
            f"SELECT MAX(valid_until_utc) FROM {TABLE_ARTIFACT}"
            " WHERE station_id IN (?, '*') AND stage IN (?, '*')"
            "   AND season_bin=? AND maturity_bin=? AND liquidity_bin=?"
            "   AND authority_scope=? AND bucket='*' AND price=?"
            "   AND valid_until_utc>? AND valid_from_utc<=?",
            (
                *scope.values(), PROBE_PRICE, evaluation,
                batch["valid_until_utc"],
            ),
        ).fetchone()[0] or batch["valid_until_utc"]
        fill.append({
            "station_id": batch["station_id"],
            "maturity_bin": scope["maturity_bin"],
            "active": True,
            "valid_from_utc": batch["valid_from_utc"],
            "valid_until_utc": batch["valid_until_utc"],
            "covered_until_utc": covered_until,
            "trained_through_utc": batch["trained_through_utc"],
            "days_left": round(days_between(now, covered_until), 1),
            "fit_age_days": round(
                -days_between(now, batch["trained_through_utc"]), 1
            ),
            "newest_batch_age_days": round(
                -days_between(now, newest or batch["valid_from_utc"]), 1
            ),
        })
    for row in fill:
        if not row.get("active"):
            continue
        if row["days_left"] < EXPIRY_RED_DAYS:
            red = True
        elif row["days_left"] < EXPIRY_WARN_DAYS:
            amber = True
        if row["newest_batch_age_days"] > REFIT_STALE_DAYS:
            amber = True
    for row in policy:
        if row["days_left"] < EXPIRY_RED_DAYS:
            red = True
        elif row["days_left"] < EXPIRY_WARN_DAYS:
            amber = True
    fill_left = [row["days_left"] for row in fill if row.get("active")]
    policy_left = [row["days_left"] for row in policy]
    if fill_left and min(fill_left) < EXPIRY_WARN_DAYS:
        notes.append(f"成交概率批次还剩 {min(fill_left):.1f} 天到期")
    if policy_left and min(policy_left) < EXPIRY_WARN_DAYS:
        notes.append(f"策略激活还剩 {min(policy_left):.1f} 天到期")
    stale = [
        row["newest_batch_age_days"] for row in fill
        if row.get("active") and row["newest_batch_age_days"] > REFIT_STALE_DAYS
    ]
    if stale:
        notes.append(f"成交概率 {max(stale):.0f} 天没有新批次（应每周重拟合）")
    if not policy:
        red = True
        notes.append("没有任何策略激活")
    return {
        "available": True,
        "health": "red" if red else ("amber" if amber else "green"),
        "note": "；".join(notes),
        "fill_days_left": min(fill_left) if fill_left else None,
        "policy_days_left": min(policy_left) if policy_left else None,
        "fit_age_days": max(
            (row["fit_age_days"] for row in fill if row.get("active")),
            default=None,
        ),
        "fill": fill,
        "policy": policy,
        "evaluated_at_utc": evaluation,
    }
