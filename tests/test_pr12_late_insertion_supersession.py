"""A late insertion no longer quarantines the whole day: end to end.

Builder, quarantine probe, and ingress run the way the sweep runs them,
against a temporary canonical store.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

from bayes_basket_target.awc_observation_builder import build_awc_observation_contract
from bayes_basket_target.awc_observation_contract import CanonicalAWCObservationContract
from bayes_basket_target.canonical_ingress import ingest_canonical_contract
from bayes_basket_target.canonical_ssot_v12_migration import (
    TARGET_TABLE_DDL as V12_TABLE_DDL,
    TARGET_TRIGGER_DDL as V12_TRIGGER_DDL,
)
from tests.test_awc_observation_builder import _evidence, _late_speci, _reseal, _slots, _stamp


def _script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "build_pr12_awc_observation_contract.py"
    spec = importlib.util.spec_from_file_location("build_pr12", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _open_day(evidence: dict, *, through_hour: int) -> dict:
    """The day as the sweep sees it mid-day: rows so far, clock before close."""

    value = deepcopy(evidence)
    value["rows"] = [row for row in value["rows"] if int(row["observation_id"].split("-")[1]) <= through_hour or int(row["observation_id"].split("-")[1]) >= 100]
    value["as_of_utc"] = _stamp(_slots("2026-08-01", "America/New_York")[through_hour] + timedelta(minutes=50))
    return _reseal(value)


def test_late_speci_is_identified_and_the_day_keeps_its_revisions(tmp_path: Path) -> None:
    script = _script()
    target = tmp_path / "target.db"
    conn = sqlite3.connect(target)
    conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
    conn.execute(V12_TABLE_DDL)
    for ddl in V12_TRIGGER_DDL:
        conn.execute(ddl)
    conn.commit()

    base = _evidence()
    projection_sha = base["authority_scope_projection"]["content_sha256"]
    # Round one: the day identified through 12:00 local.
    noon = _open_day(base, through_hour=12)
    noon = script._attach_stored_revision_facts(target, noon, projection_sha)
    assert noon["stored_revision_facts"] == []
    contract, _ = build_awc_observation_contract(noon)
    assert script._conflicting_day_scopes(target, contract) == {}
    ingest_canonical_contract(conn, contract, commit=True, ingested_at_utc="2026-08-01T17:00:00Z")
    conn.commit()
    stored_noon = conn.execute(
        "SELECT COUNT(*) FROM canonical_ssot_fact WHERE fact_type='truth_observation_revision'"
    ).fetchone()[0]
    assert stored_noon == 13

    # Round two, 15:50 local: three more hourly reports, and a 10:30 SPECI
    # that only just arrived.
    afternoon = deepcopy(base)
    afternoon["rows"].append(_late_speci("2026-08-01", "America/New_York", hour=10))
    afternoon = _open_day(afternoon, through_hour=15)
    afternoon = script._attach_stored_revision_facts(target, afternoon, projection_sha)
    assert len(afternoon["stored_revision_facts"]) == 13
    probe, receipt = build_awc_observation_contract(afternoon)
    # Nothing to quarantine: shifted reports are revision two, not a conflict.
    assert script._conflicting_day_scopes(target, probe) == {}
    assert receipt["late_insertion_supersession_count"] == 2  # 11:00 and 12:00
    parsed = CanonicalAWCObservationContract.from_mapping(probe)
    revisions = {x.raw["observation_id"]: x.raw["revision_no"] for x in parsed.revision_facts}
    assert revisions["obs-100"] == 1
    assert revisions["obs-11"] == 2 and revisions["obs-12"] == 2
    assert revisions["obs-13"] == 1 and revisions["obs-15"] == 1
    assert revisions["obs-10"] == 1
    ingest = ingest_canonical_contract(conn, probe, commit=True, ingested_at_utc="2026-08-01T20:00:00Z")
    conn.commit()
    # 10 unchanged facts replay; the SPECI, two revision-twos and three new
    # hourly reports insert.
    assert ingest["replayed_count"] == 11
    assert ingest["inserted_count"] == 6
    newest = {
        row[0]: int(row[1])
        for row in conn.execute(
            "SELECT json_extract(fact_json,'$.observation_id'), MAX(json_extract(fact_json,'$.revision_no')) "
            "FROM canonical_ssot_fact WHERE fact_type='truth_observation_revision' GROUP BY 1"
        )
    }
    assert newest["obs-11"] == 2 and newest["obs-12"] == 2 and newest["obs-100"] == 1
