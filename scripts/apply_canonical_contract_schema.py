#!/usr/bin/env python3
"""Retired unsafe V10 canonical-schema installer.

The old command could create a V10 fact table outside the explicit,
receipted V13-to-V14 migration.  It is deliberately retained only as a
fail-closed compatibility tombstone.  Use
``scripts/migrate_canonical_ssot_fact_v14.py`` instead.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


RETIRED_MESSAGE = (
    "canonical_contract_schema_legacy_apply_retired:"
    "use_scripts_migrate_canonical_ssot_fact_v14"
)


def migrate(db: Path, *, apply: bool) -> dict[str, object]:
    # Do not resolve, open, inspect, or mutate the supplied path.  Any old
    # automation must fail before it can become an alternative ingress.
    del db, apply
    raise RuntimeError(RETIRED_MESSAGE)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("data/bayes_basket_target_ssot.db"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate(args.db, apply=bool(args.apply)), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
