#!/usr/bin/env python3
"""Build the non-writing canonical V1→V2 AWC authority contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json, write_bounded_json_if_changed,
)
from bayes_basket_target.truth_authority_supersession import (  # noqa: E402
    build_canonical_truth_authority_supersession_contract,
    build_truth_authority_review_scope_audit,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--archive-database", type=Path, required=True)
    parser.add_argument("--authority-projection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    database = args.database.expanduser().resolve(strict=True)
    projection = load_bounded_json(
        args.authority_projection.expanduser().resolve(strict=True)
    )
    archive = args.archive_database.expanduser().resolve(strict=True)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn, sqlite3.connect(
        f"file:{archive}?mode=ro", uri=True
    ) as archive_conn:
        conn.execute("PRAGMA query_only=ON")
        archive_conn.execute("PRAGMA query_only=ON")
        legacy_scopes = []
        for (raw_json,) in conn.execute(
            "SELECT fact_json FROM canonical_ssot_fact "
            "WHERE fact_type='truth_observation_day_bounds'"
        ):
            raw = json.loads(str(raw_json))
            if "authority_scope_projection_content_sha256" not in raw:
                legacy_scopes.append((
                    str(raw["station_id"]), str(raw["event_date"]),
                    str(raw["settlement_timezone"]),
                ))
        review_audit = build_truth_authority_review_scope_audit(
            archive_conn,
            legacy_scopes=legacy_scopes,
            authority_scope_projection=projection,
        )
        contract = build_canonical_truth_authority_supersession_contract(
            conn, authority_scope_projection=projection,
            review_scope_audit=review_audit,
        )
    write_bounded_json_if_changed(args.output.expanduser().resolve(), contract)
    print(json.dumps({
        "state": "MATERIALIZED_UNPUBLISHED",
        "revision_fact_count": len(
            contract["awc_observation_contract"]["revision_facts"]
        ),
        "day_bounds_fact_count": len(
            contract["awc_observation_contract"]["day_bounds_facts"]
        ),
        "supersession_fact_count": len(contract["supersession_facts"]),
        "review_scope_blocker_count": contract["review_scope_audit"][
            "review_scope_blocker_count"
        ],
        "content_sha256": contract["content_sha256"],
        "writes_database": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
