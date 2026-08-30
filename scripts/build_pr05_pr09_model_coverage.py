#!/usr/bin/env python3
"""Build the single read-only PR05--PR09 model-system coverage matrix.

This is an outer assembler only.  It calls the existing PR05, PR06, PR07,
and PR08/09 adapters, revalidates each shared matrix at one frozen clock, and
then rebuilds exactly one 21-product × 45-station × d0/d1/d2 coverage
matrix.  It neither contacts a network endpoint nor writes SSOT, artifacts,
activations, P0, orders, or service state.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.benchmark_model_coverage import (  # noqa: E402
    GLOBAL_DETERMINISTIC_PRODUCTS,
    REGIONAL_BENCHMARK_PRODUCTS,
    build_global_deterministic_coverage_rows,
    build_regional_benchmark_coverage_rows,
    load_pr05_inputs,
)
from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.icon_global_eps_coverage_matrix import (  # noqa: E402
    GLOBAL_EPS,
    build_icon_global_eps_coverage_matrix,
)
from bayes_basket_target.model_system_coverage import (  # noqa: E402
    build_coverage_matrix,
    canonical_station_ids,
    validate_coverage_matrix,
)
from bayes_basket_target.native_ensemble_terminal_matrix import (  # noqa: E402
    SYSTEM_SPECS,
    build_pr08_pr09_terminal_matrix,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json_if_changed,
)
from scripts.audit_ecmwf_pf50_aifs_terminal_coverage import (  # noqa: E402
    AIFS_REPUBLISHER_PRODUCT,
    AIFS_SINGLE_PRODUCT,
    DEFAULT_AIFS_OOS,
    DEFAULT_AIFS_READINESS,
    DEFAULT_AIFS_REPUBLISHER_OOS,
    DEFAULT_PF50_ARCHIVE,
    DEFAULT_PF50_OOS_LINKAGE,
    PF50_PRODUCT,
    build_terminal_matrix as build_pr06_coverage_matrix,
)
from bayes_basket_target.fleet import expected_station_count


COMPILED_AT_UTC = "2026-08-03T21:08:00Z"
MAX_INPUT_BYTES = 64 * 1024 * 1024

PR05_PRODUCTS = (*GLOBAL_DETERMINISTIC_PRODUCTS, *REGIONAL_BENCHMARK_PRODUCTS)
PR06_PRODUCTS = (PF50_PRODUCT, AIFS_SINGLE_PRODUCT, AIFS_REPUBLISHER_PRODUCT)
PR07_PRODUCTS = (GLOBAL_EPS,)
PR08_PR09_PRODUCTS = tuple(SYSTEM_SPECS)
EXPECTED_PRODUCT_IDS = tuple(sorted({
    *PR05_PRODUCTS,
    *PR06_PRODUCTS,
    *PR07_PRODUCTS,
    *PR08_PR09_PRODUCTS,
}))
EXPECTED_SCOPE_COUNT = (
    expected_station_count() * 3 * len(EXPECTED_PRODUCT_IDS)
)

DEFAULT_D1_RECEIPT = (
    ROOT / "output/audits/d1_canonical_residual_45station_20260804_post_pr02c_v2/build_receipt.json"
)
DEFAULT_REGIONAL_AUDIT = ROOT / "output/audits/regional_model_candidate_closure_20260803_post_proxy_refresh.json"
DEFAULT_ICON_READINESS = ROOT / "output/audits/dwd_icon_global_eps_official_readiness_20260802_latest.json"
DEFAULT_ICON_STRICT_OOS = ROOT / "output/audits/icon_global_eps_strict_oos_20260802_latest.json"
DEFAULT_GEFS_AUDIT = ROOT / "output/audits/native_ensemble_oos/GEFS_45station_historical_d2_20plus8_oos.json"
DEFAULT_GEPS_AUDIT = ROOT / "output/audits/native_ensemble_oos/native_forward_oos_45station_final_20260801.json"


def _load_json(path: Path | str) -> Mapping[str, Any]:
    source = Path(path).expanduser()
    if source.is_symlink():
        raise ContractError("pr05_pr09_coverage_input_symlink_forbidden")
    source = source.resolve(strict=True)
    if not source.is_file() or source.stat().st_size > MAX_INPUT_BYTES:
        raise ContractError("pr05_pr09_coverage_input_invalid")
    value = load_bounded_json(source, limit=MAX_INPUT_BYTES)
    if not isinstance(value, Mapping):
        raise ContractError("pr05_pr09_coverage_input_mapping_required")
    return value


def _validated_source_matrix(
    matrix: Mapping[str, Any],
    *,
    expected_products: Sequence[str],
) -> dict[str, Any]:
    validated = validate_coverage_matrix(matrix)
    if validated["as_of_utc"] != COMPILED_AT_UTC:
        raise ContractError("pr05_pr09_coverage_source_as_of_mismatch")
    if tuple(validated["product_ids"]) != tuple(sorted(expected_products)):
        raise ContractError("pr05_pr09_coverage_source_product_set_mismatch")
    if (
        validated["cross_selection_count"] != 0
        or validated["cross_activation_count"] != 0
        or validated["p0_count"] != 0
    ):
        raise ContractError("pr05_pr09_coverage_source_cross_or_p0_forbidden")
    return validated


def build_pr05_pr09_coverage_matrix(
    *,
    pr05_matrix: Mapping[str, Any],
    pr06_matrix: Mapping[str, Any],
    pr07_matrix: Mapping[str, Any],
    pr08_pr09_matrix: Mapping[str, Any],
) -> dict[str, Any]:
    """Rebuild the one exact 2,835-scope shared coverage matrix."""

    matrices = (
        _validated_source_matrix(pr05_matrix, expected_products=PR05_PRODUCTS),
        _validated_source_matrix(pr06_matrix, expected_products=PR06_PRODUCTS),
        _validated_source_matrix(pr07_matrix, expected_products=PR07_PRODUCTS),
        _validated_source_matrix(
            pr08_pr09_matrix,
            expected_products=PR08_PR09_PRODUCTS,
        ),
    )
    rows = [row for matrix in matrices for row in matrix["rows"]]
    final = build_coverage_matrix(
        rows,
        expected_product_ids=EXPECTED_PRODUCT_IDS,
        as_of_utc=COMPILED_AT_UTC,
    )
    validated = validate_coverage_matrix(final)
    if validated["scope_count"] != EXPECTED_SCOPE_COUNT:
        raise ContractError("pr05_pr09_coverage_scope_count_invalid")
    if (
        validated["cross_selection_count"] != 0
        or validated["cross_activation_count"] != 0
        or validated["p0_count"] != 0
    ):
        raise ContractError("pr05_pr09_coverage_cross_or_p0_forbidden")
    return validated


def build_from_local_evidence(
    *,
    database: Path | str,
    d1_build_receipt: Path | str,
    regional_audit: Path | str,
    pf50_archives: Sequence[Path | str],
    pf50_oos_linkage: Path | str,
    aifs_readiness: Path | str,
    aifs_oos: Path | str,
    aifs_republisher_oos: Path | str,
    icon_official_readiness: Path | str,
    icon_strict_oos: Path | str,
    gefs_audit: Path | str,
    geps_audit: Path | str,
) -> dict[str, Any]:
    """Call the four existing local adapters with one immutable outer clock."""

    facts, d1_receipt, regional = load_pr05_inputs(
        db=database,
        d1_build_receipt=d1_build_receipt,
        regional_audit=regional_audit,
    )
    pr05_matrix = build_coverage_matrix(
        (
            *build_global_deterministic_coverage_rows(
                facts,
                d1_build_receipt=d1_receipt,
                as_of_utc=COMPILED_AT_UTC,
            ),
            *build_regional_benchmark_coverage_rows(
                regional,
                as_of_utc=COMPILED_AT_UTC,
            ),
        ),
        expected_product_ids=PR05_PRODUCTS,
        as_of_utc=COMPILED_AT_UTC,
    )
    pr06_matrix = build_pr06_coverage_matrix(
        pf50_archives=[_load_json(path) for path in pf50_archives],
        pf50_oos_linkage=_load_json(pf50_oos_linkage),
        aifs_readiness=_load_json(aifs_readiness),
        aifs_oos=_load_json(aifs_oos),
        aifs_republisher_oos=_load_json(aifs_republisher_oos),
        as_of_utc=COMPILED_AT_UTC,
        station_ids=canonical_station_ids(),
    )
    pr07_matrix = build_icon_global_eps_coverage_matrix(
        official_readiness=_load_json(icon_official_readiness),
        strict_oos=_load_json(icon_strict_oos),
        compiled_at_utc=COMPILED_AT_UTC,
    )
    pr08_pr09_matrix = build_pr08_pr09_terminal_matrix(
        gefs_audit=_load_json(gefs_audit),
        geps_audit=_load_json(geps_audit),
        station_ids=canonical_station_ids(),
        gefs_audit_pointer=str(Path(gefs_audit)),
        geps_audit_pointer=str(Path(geps_audit)),
        compiled_at_utc=COMPILED_AT_UTC,
    )
    return build_pr05_pr09_coverage_matrix(
        pr05_matrix=pr05_matrix,
        pr06_matrix=pr06_matrix,
        pr07_matrix=pr07_matrix,
        pr08_pr09_matrix=pr08_pr09_matrix,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "data/bayes_basket_target_ssot.db")
    parser.add_argument("--d1-build-receipt", type=Path, default=DEFAULT_D1_RECEIPT)
    parser.add_argument("--regional-audit", type=Path, default=DEFAULT_REGIONAL_AUDIT)
    parser.add_argument("--pf50-archive", type=Path, action="append", default=[])
    parser.add_argument("--pf50-oos-linkage", type=Path, default=DEFAULT_PF50_OOS_LINKAGE)
    parser.add_argument("--aifs-readiness", type=Path, default=DEFAULT_AIFS_READINESS)
    parser.add_argument("--aifs-oos", type=Path, default=DEFAULT_AIFS_OOS)
    parser.add_argument("--aifs-republisher-oos", type=Path, default=DEFAULT_AIFS_REPUBLISHER_OOS)
    parser.add_argument("--icon-official-readiness", type=Path, default=DEFAULT_ICON_READINESS)
    parser.add_argument("--icon-strict-oos", type=Path, default=DEFAULT_ICON_STRICT_OOS)
    parser.add_argument("--gefs-audit", type=Path, default=DEFAULT_GEFS_AUDIT)
    parser.add_argument("--geps-audit", type=Path, default=DEFAULT_GEPS_AUDIT)
    parser.add_argument("--compiled-at-utc", default=COMPILED_AT_UTC)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.compiled_at_utc != COMPILED_AT_UTC:
        raise ContractError("pr05_pr09_coverage_compiled_at_fixed")
    matrix = build_from_local_evidence(
        database=args.database,
        d1_build_receipt=args.d1_build_receipt,
        regional_audit=args.regional_audit,
        pf50_archives=tuple(args.pf50_archive) or (DEFAULT_PF50_ARCHIVE,),
        pf50_oos_linkage=args.pf50_oos_linkage,
        aifs_readiness=args.aifs_readiness,
        aifs_oos=args.aifs_oos,
        aifs_republisher_oos=args.aifs_republisher_oos,
        icon_official_readiness=args.icon_official_readiness,
        icon_strict_oos=args.icon_strict_oos,
        gefs_audit=args.gefs_audit,
        geps_audit=args.geps_audit,
    )
    output = args.output.expanduser().resolve()
    wrote = write_bounded_json_if_changed(output, matrix)
    print(json.dumps({
        "output": str(output),
        "output_wrote": wrote,
        "compiled_at_utc": matrix["as_of_utc"],
        "scope_count": matrix["scope_count"],
        "state_counts": matrix["state_counts"],
        "cross_selection_count": matrix["cross_selection_count"],
        "cross_activation_count": matrix["cross_activation_count"],
        "p0_count": matrix["p0_count"],
        "content_sha256": matrix["content_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI wrapper.
    raise SystemExit(main())
