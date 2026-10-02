#!/usr/bin/env python3
"""Charter 245, one-off: activate charter 239's thin-pool far-lead slice at
width lambda 1.1 on the P0 centre/width table, keeping the stage pair as it is.

Evidence: docs/research/thin_pool_width_long_20261002 (pre-registered cf54cadf):
the slice lambda chosen forward on 2026-02..09 against 0.75, 5,817 station-days,
LogScore -0.0766 [-0.0974, -0.0573], bucket Brier -0.0051, CRPS -0.0019, both
halves passing charter 240; the rule's pick as of 2026-10-02 is 1.1.  The daily
centre/width refit keeps judging the slice and its veto (charter 239 (5))
removes it if it serves significantly worse than the stage lambda.  Written
through ``persist_and_activate_center_width``, the lane's own writer.
"""
from __future__ import annotations
import json, sqlite3, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import center_width_store as store  # noqa: E402

SLICE_LAMBDA = 1.1


def main() -> int:
    conn = sqlite3.connect(str(ROOT / "data" / "bayes_basket_target_ssot.db"), timeout=60.0)
    conn.execute("PRAGMA busy_timeout=60000")
    active = store.load_center_width(conn, stage="P0")
    if active is None or (active[0], active[1]) != (0.0, 0.75):
        sys.exit(f"unexpected active P0 pair {active}; refusing")
    if store.load_center_width_floor(conn, stage="P0") or store.load_center_width_tiers(conn, stage="P0"):
        sys.exit("active P0 row carries a floor or tiers; refusing")
    if store.load_center_width_slices(conn, stage="P0"):
        sys.exit("a slice is already active; refusing")
    evidence = {
        "schema": "center_width_manual_slice_activation_v1",
        "charter": 245,
        "ruling": "2026-10-02 熊先生「继续打家少偏窄」",
        "research": "docs/research/thin_pool_width_long_20261002",
        "protocol_commit": "cf54cadf",
        "slice_verdict": {"d_logscore": -0.0766, "ci_logscore": [-0.0974, -0.0573], "d_brier": -0.0051, "d_crps": -0.0019,
                          "halves_d_logscore": {"04-06": -0.0392, "07-09": -0.1155}, "station_days": 5817},
        "incumbent_sha256": active[2],
    }
    out = store.persist_and_activate_center_width(
        conn, stage="P0", delta_c=0.0, width_lambda=0.75, evidence=evidence, serving_day_count=8,
        width_slices={store.THIN_FAR_SLICE_NAME: store._slice_entry(SLICE_LAMBDA)},
    )
    conn.commit()
    print(json.dumps({"result": out, "active_slices": store.load_center_width_slices(conn, stage="P0")}, ensure_ascii=False))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
