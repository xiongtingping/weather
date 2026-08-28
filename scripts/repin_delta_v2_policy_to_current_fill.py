#!/usr/bin/env python3
"""Re-pin every live delta-v2 policy to the fill batch production resolves.

Every candidate that reached the execution compile after 2026-08-22 died on
``delta_v2_fill_distribution_policy_binding_mismatch``, and no data existed
that could have satisfied the check.  The policies pin the placeholder fill
generation (one sha per scope, minted 07-29); the 08-22 refit replaced it
with measured per-cell distributions -- 151 hashes per scope -- and the
resolver picks that newer batch by valid_from while the binding check
demanded equality with a single pinned sha.  The fitting script's own
re-mint step updated the execution grids but never touched the pin.

This mints the successor generation of each live policy activation --
artifact, promotion and activation together, generation + 1, the same
append-only path the fitting script uses -- with the pin set to the
CURRENT fill batch's fingerprint (``content_sha256`` over the batch's
sorted distribution hashes, the identity the resolver now accepts
alongside the legacy exact form).  The batch is resolved per scope by the
resolver's own preference: newest valid_from among rows active at the
supplied evaluation instant.

Validity runs 30 days from now, which also carries these policies across
the 2026-08-31T00:00:00Z expiry the previous generation was minted with.

Read-only unless ``--commit``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.basket_kernel.contracts import (  # noqa: E402
    content_sha256,
)
from bayes_basket_target.delta_v2_artifact import (  # noqa: E402
    ACTIVATION_SCHEMA,
    NO_LIVE_ACTIVATION_DECISION,
    PROMOTION_SCHEMA,
    commit_delta_v2_authority,
)
from bayes_basket_target.delta_v2_fill_artifact import (  # noqa: E402
    TABLE_ARTIFACT,
    _fill_batch_fingerprint,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def _resolver_batch(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    stage: str,
    season_bin: str,
    maturity_bin: str,
    liquidity_bin: str,
    authority_scope: str,
    evaluation_at_utc: str,
) -> tuple[str, str] | None:
    """(batch station_id, valid_from) of the batch the resolver picks NOW.

    Mirrors the resolver's preference order -- most specific scope first,
    then newest valid_from -- because the pin has to name the batch the
    finalist will actually belong to, not merely the newest batch in
    sight.  The distinction is load-bearing: RKPK's own fill rows exist
    only in the placeholder generation, so until that generation expires
    on 2026-08-31 the resolver's finalist for RKPK is an RKPK row from the
    OLD batch, and a pin naming the new '*' batch would fail three days
    early.  Conversely, after expiry the finalist falls to '*', and a pin
    naming the old RKPK batch fails from then on.  This script is
    therefore idempotent and meant to be re-run (daily, from the sweep):
    each run pins what the resolver picks that day, and a pin already
    current is a skip.
    """

    best: tuple[int, str, str] | None = None
    for batch_station, batch_from in conn.execute(
        f"SELECT DISTINCT station_id, valid_from_utc FROM {TABLE_ARTIFACT}"
        " WHERE station_id IN (?, '*') AND stage IN (?, '*')"
        "   AND season_bin=? AND maturity_bin=? AND liquidity_bin=?"
        "   AND authority_scope=?"
        "   AND valid_from_utc<=? AND valid_until_utc>?",
        (
            station_id, stage, season_bin, maturity_bin, liquidity_bin,
            authority_scope, evaluation_at_utc, evaluation_at_utc,
        ),
    ):
        specificity = 0 if str(batch_station) == "*" else 1
        key = (specificity, str(batch_from), str(batch_station))
        if best is None or key > best:
            best = key
    return None if best is None else (best[2], best[1])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--commit", action="store_true",
        help="mint the re-pinned generation (default: report only)",
    )
    args = parser.parse_args()
    database = args.db.expanduser().resolve(strict=True)
    if args.commit:
        conn = sqlite3.connect(str(database), timeout=60.0)
    else:
        conn = sqlite3.connect(
            f"file:{database}?mode=ro", uri=True, timeout=60.0
        )
    conn.execute("PRAGMA busy_timeout=60000")
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    valid_from = (
        now + timedelta(minutes=1)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    valid_until = (
        now + timedelta(days=30)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    minted = skipped = 0
    for activation in conn.execute(
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
    ).fetchall():
        scope = (
            str(activation["station_id"]), str(activation["stage"]),
            str(activation["season_bin"]), str(activation["maturity_bin"]),
            str(activation["liquidity_bin"]),
            str(activation["strategy_version"]),
        )
        artifact_row = conn.execute(
            "SELECT * FROM basket_delta_v2_policy_artifact"
            " WHERE artifact_id=?",
            (activation["artifact_id"],),
        ).fetchone()
        if artifact_row is None:
            print(f"SKIP {scope}: activation names a missing artifact")
            skipped += 1
            continue
        batch = _resolver_batch(
            conn,
            station_id=scope[0],
            stage=scope[1],
            season_bin=scope[2],
            maturity_bin=scope[3],
            liquidity_bin=scope[4],
            authority_scope=str(activation["authority_scope"]),
            evaluation_at_utc=stamp,
        )
        if batch is None:
            print(f"SKIP {scope}: no active fill batch to pin")
            skipped += 1
            continue
        batch_station, batch_from = batch
        # The fingerprint is over the batch's own scope -- the station the
        # winning rows actually carry -- exactly as the resolver computes
        # it for the finalist.
        fingerprint = _fill_batch_fingerprint(
            conn,
            station_id=batch_station,
            stage=scope[1],
            season_bin=scope[2],
            maturity_bin=scope[3],
            liquidity_bin=scope[4],
            valid_from_utc=batch_from,
            authority_scope=str(activation["authority_scope"]),
        )
        payload = json.loads(artifact_row["artifact_json"])
        if str(payload.get("fill_distribution_artifact_sha256")) == (
            fingerprint
        ):
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
        promotion_id = "delta-v2-policy-no-live-" + updated["artifact_id"]
        promotion = {
            "schema": PROMOTION_SCHEMA,
            "promotion_id": promotion_id,
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
        print(
            f"{'MINT' if args.commit else 'WOULD-MINT'} {scope}"
            f" gen {activation['generation']}→"
            f"{activation_payload['generation']}"
            f" batch=({batch_station},{batch_from})"
            f" pin={fingerprint[:16]}…"
            f" 窗口 {valid_from} → {valid_until}"
        )
        if args.commit:
            receipt = commit_delta_v2_authority(
                conn,
                artifact=updated,
                promotion=promotion,
                activation=activation_payload,
            )
            if receipt.get("state") != "COMMITTED":
                print(f"  !! commit state={receipt.get('state')}")
                skipped += 1
                continue
        minted += 1
    print(f"\n{'minted' if args.commit else 'would mint'}={minted} "
          f"skipped={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
