#!/usr/bin/env python3
"""Bind a validated TAF transport OOS report to its exact live P0 fingerprint.

This command writes one content-addressed TAF artifact file only.  It never
opens SQLite, changes an activation pointer, restarts a service, or calls an
order gateway.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.calibration import (  # noqa: E402
    bind_taf_artifact_to_live_nwp_compatibility,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.taf_nwp_transport_oos import (  # noqa: E402
    SCHEMA as TRANSPORT_REPORT_SCHEMA,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--taf-artifact", type=Path, required=True)
    parser.add_argument("--transport-oos-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    artifact = load_bounded_json(args.taf_artifact.expanduser().resolve())
    report = load_bounded_json(
        args.transport_oos_report.expanduser().resolve()
    )
    if (
        report.get("schema") != TRANSPORT_REPORT_SCHEMA
        or report.get("state") != "PASS"
        or report.get("validated") is not True
        or not isinstance(report.get("live_fingerprint"), dict)
        or not isinstance(report.get("evidence"), dict)
    ):
        raise RuntimeError("taf_transport_oos_report_not_validated")
    bound = bind_taf_artifact_to_live_nwp_compatibility(
        taf_artifact=artifact,
        live_fingerprint=report["live_fingerprint"],
        transport_oos_evidence=report["evidence"],
    )
    output = args.output.expanduser().resolve()
    write_bounded_json(output, bound.to_dict())
    print(json.dumps({
        "schema": "bayes_basket_target_taf_live_compatibility_binding_v1",
        "state": "BOUND",
        "output": str(output),
        "artifact_id": bound.artifact_id,
        "content_sha256": bound.content_sha256,
        "nwp_live_compatibility_fingerprint_sha256": (
            bound.nwp_live_compatibility_fingerprint_sha256
        ),
        "nwp_live_compatibility_evidence_sha256": (
            bound.nwp_live_compatibility_evidence_sha256
        ),
        "nwp_live_compatibility_validated": (
            bound.nwp_live_compatibility_validated
        ),
        "writes_database": False,
        "calls_order_gateway": False,
        "places_orders": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
