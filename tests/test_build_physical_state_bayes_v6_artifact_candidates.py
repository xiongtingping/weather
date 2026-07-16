from __future__ import annotations

import gzip
import json

from physical_state_bayes_v6.artifact_contracts import validate_observation_artifact, validate_transition_artifact
from scripts.build_physical_state_bayes_v6_artifact_candidates import build


def test_builder_emits_non_shadow_candidates_with_lineage(tmp_path) -> None:
    source = tmp_path / "rows.jsonl.gz"
    rows = []
    for day in ("2026-07-01", "2026-07-02"):
        temperatures = [25.0, 27.0, 29.0, 29.0, 28.0, 29.0, 28.0, 27.0]
        final = max(temperatures)
        observed = -99.0
        for hour, temperature in enumerate(temperatures):
            previous = temperatures[hour - 1] if hour else temperature
            observed = max(observed, temperature)
            rows.append(
                {
                    "event_date_local": day,
                    "valid_time_utc": f"{day}T{hour:02d}:00:00Z",
                    "temperature_c": temperature,
                    "dewpoint_c": 20.0,
                    "temperature_delta_1h": temperature - previous,
                    "remaining_warming_c": final - observed,
                    "hours_since_observed_max": 0.0 if temperature == observed else 1.0,
                    "local_hour": hour + 10,
                    "cloud_cover_pct": 90.0 if hour == 4 else 10.0,
                    "cloud_text": "OVC030" if hour == 4 else "FEW040",
                    "current_precipitation_reported": hour == 4,
                    "current_convective_weather": False,
                    "convective_cloud_reported": False,
                    "daily_max_label_eligible": True,
                    "core_qc_bad": False,
                }
            )
    with gzip.open(source, "wt", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")
    output = tmp_path / "artifacts"
    report = build(source, output, training_end_exclusive="2027-01-01")
    transition = json.loads((output / "transition_artifact.candidate.json").read_text())
    observation = json.loads((output / "observation_artifact.candidate.json").read_text())
    assert report["ok"] is True
    assert report["shadow_runtime"] is False
    assert transition["production_runtime_eligible"] is False
    assert observation["production_runtime_eligible"] is False
    validate_transition_artifact(transition)
    validate_observation_artifact(observation)
