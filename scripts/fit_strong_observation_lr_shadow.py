#!/usr/bin/env python3
"""Fit one strong-LR shadow candidate from explicit frozen JSON inputs only."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strong_observation_lr_fit_runtime import fit_strong_observation_lr_shadow_candidate  # noqa: E402


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _load(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return _mapping(value)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--training-dataset", type=Path, required=True)
    parser.add_argument("--latent-transition-labels", type=Path, required=True)
    parser.add_argument("--temperature-grid-c", required=True, help="JSON numeric array, e.g. '[30,30.5,31]'")
    parser.add_argument("--laplace-alpha", type=float, default=1.0)
    parser.add_argument("--temperature-grid-tolerance-c", type=float, default=0.26)
    args = parser.parse_args(argv)
    try:
        grid = json.loads(args.temperature_grid_c)
    except json.JSONDecodeError:
        grid = []
    try:
        labels = json.loads(args.latent_transition_labels.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        labels = []
    result = fit_strong_observation_lr_shadow_candidate(
        training_dataset=_load(args.training_dataset),
        latent_transition_labels=labels if isinstance(labels, list) else [],
        temperature_grid_c=grid if isinstance(grid, list) else [],
        laplace_alpha=args.laplace_alpha,
        temperature_grid_tolerance_c=args.temperature_grid_tolerance_c,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
