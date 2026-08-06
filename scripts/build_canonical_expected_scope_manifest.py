#!/usr/bin/env python3
"""Build PR04 expected-scope bases and the single release manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.canonical_expected_scope_manifest import (
    build_model_oos_scope_basis,
    compile_expected_scope_manifest,
)
from bayes_basket_target.model_system_coverage import validate_coverage_matrix
from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json


def _model_basis(args: argparse.Namespace) -> int:
    matrix = validate_coverage_matrix(load_bounded_json(args.coverage_matrix))
    result = build_model_oos_scope_basis(
        matrix, release_as_of_utc=args.as_of_utc
    )
    write_bounded_json(args.output, result)
    print(json.dumps({
        "schema": result["schema"],
        "scope_type": result["scope_type"],
        "expected_key_count": len(result["expected_keys"]),
        "key_evidence_count": len(result["expected_key_evidence"]),
        "content_sha256": result["content_sha256"],
        "output": str(args.output),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _compile(args: argparse.Namespace) -> int:
    bases = [load_bounded_json(path) for path in args.basis]
    result = compile_expected_scope_manifest(bases, as_of_utc=args.as_of_utc)
    write_bounded_json(args.output, result)
    print(json.dumps({
        "schema": result["schema"],
        "scope_count": len(result["scope_expectations"]),
        "content_sha256": result["content_sha256"],
        "output": str(args.output),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    model = commands.add_parser("model-basis")
    model.add_argument("--coverage-matrix", type=Path, required=True)
    model.add_argument("--as-of-utc", required=True)
    model.add_argument("--output", type=Path, required=True)
    model.set_defaults(run=_model_basis)
    compile_parser = commands.add_parser("compile")
    compile_parser.add_argument("--basis", type=Path, action="append", required=True)
    compile_parser.add_argument("--as-of-utc", required=True)
    compile_parser.add_argument("--output", type=Path, required=True)
    compile_parser.set_defaults(run=_compile)
    args = parser.parse_args()
    return int(args.run(args))


if __name__ == "__main__":
    raise SystemExit(main())
