"""Charter 217: ECMWF AIFS ENS is its own independence family.

User ruling 2026-09-25, 「把 IFS 和 AIFS 拆成两个家族」.  Three things have
to hold: the registered row moves from ECMWF to ECMWF_AIFS exactly once, by its
own step, and the registry's append-only guard survives the move; the stable pool then
gives IFS and AIFS a family's share each and mints the IFS+AIFS pair; and an
artifact minted before the split keeps serving under the map it was
certified under instead of failing closed until the rotation re-mints it.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from bayes_basket_target import ecmwf_aifs_ens_archive as aifs
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.native_ensemble_p0 import (
    CERTIFIED_FAMILY_MAP_SQL,
    served_family_map,
)
from bayes_basket_target.ssot import apply_production_ssot_schema
from bayes_basket_target.stable_pool import family_balanced_weights, pool_subsets

IFS = "ECMWF_IFS_ENS_OPEN_PF50"
AIFS = aifs.SYSTEM_ID
GUARD = "calibration_native_system_no_update"


def _registry_with_the_pre_split_row() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    pre_split = {**aifs.SYSTEM_IDENTITY, "independence_family_id": "ECMWF"}
    columns = sorted(pre_split)
    conn.execute(
        "INSERT INTO calibration_native_ensemble_system("
        + ",".join(columns) + ",content_sha256) VALUES("
        + ",".join("?" for _ in columns) + ",?)",
        [pre_split[c] for c in columns] + [content_sha256(pre_split)],
    )
    conn.commit()
    return conn


def _row(conn: sqlite3.Connection) -> tuple[str, str]:
    return conn.execute(
        "SELECT independence_family_id,content_sha256 "
        "FROM calibration_native_ensemble_system WHERE ensemble_system_id=?",
        (AIFS,),
    ).fetchone()


def _guard(conn: sqlite3.Connection) -> str:
    return conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
        (GUARD,),
    ).fetchone()[0]


def test_ingest_leaves_the_pre_split_row_until_the_relabel_step() -> None:
    # Capture subprocesses run this code before the resident is restarted
    # onto it; relabelling from there would fail the old compiler closed.
    conn = _registry_with_the_pre_split_row()
    aifs.ensure_system(conn)
    assert _row(conn) == ("ECMWF", aifs.PRE_SPLIT_REGISTRY_CONTENT_SHA256)


def test_the_pre_split_row_moves_to_aifs_own_family_once() -> None:
    conn = _registry_with_the_pre_split_row()
    assert _row(conn) == ("ECMWF", aifs.PRE_SPLIT_REGISTRY_CONTENT_SHA256)
    guard = _guard(conn)
    assert aifs.relabel_pre_split_registration(conn) is True
    conn.commit()
    assert _row(conn) == ("ECMWF_AIFS", content_sha256(aifs.SYSTEM_IDENTITY))
    assert _guard(conn) == guard
    # The guard is back: the row cannot be edited again.
    with pytest.raises(sqlite3.DatabaseError, match="append_only"):
        conn.execute(
            "UPDATE calibration_native_ensemble_system SET canonical_name='x' "
            "WHERE ensemble_system_id=?",
            (AIFS,),
        )
    assert aifs.relabel_pre_split_registration(conn) is False
    aifs.ensure_system(conn)
    assert _row(conn) == ("ECMWF_AIFS", content_sha256(aifs.SYSTEM_IDENTITY))


def test_a_fresh_registry_registers_aifs_under_its_own_family() -> None:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    assert aifs.relabel_pre_split_registration(conn) is False
    aifs.ensure_system(conn)
    assert _row(conn) == ("ECMWF_AIFS", content_sha256(aifs.SYSTEM_IDENTITY))


def test_a_row_that_is_not_the_pre_split_one_is_never_edited() -> None:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    other = {**aifs.SYSTEM_IDENTITY, "independence_family_id": "SOMETHING_ELSE"}
    columns = sorted(other)
    conn.execute(
        "INSERT INTO calibration_native_ensemble_system("
        + ",".join(columns) + ",content_sha256) VALUES("
        + ",".join("?" for _ in columns) + ",?)",
        [other[c] for c in columns] + [content_sha256(other)],
    )
    assert aifs.relabel_pre_split_registration(conn) is False
    with pytest.raises(ContractError, match="content_conflict"):
        aifs.ensure_system(conn)
    assert _row(conn)[0] == "SOMETHING_ELSE"


def test_the_stable_pool_gives_ifs_and_aifs_a_family_share_each() -> None:
    family = {
        IFS: "ECMWF",
        AIFS: aifs.INDEPENDENCE_FAMILY_ID,
        "NOAA_GEFS_05": "NOAA_GEFS",
        "NOAA_AIGEFS_OPS_025": "NOAA_AIGEFS",
        "DWD_ICON_GLOBAL_EPS": "DWD_ICON_EPS",
        "ECCC_GEM_GLOBAL_ENS": "ECCC_GEM",
    }
    baseline = family_balanced_weights(list(family), family)
    assert all(abs(weight - 1.0 / 6.0) < 1e-12 for weight in baseline.values())
    assert tuple(sorted((IFS, AIFS))) in pool_subsets(list(family), family)


def _predictions(rows: list[tuple[str, str, dict[str, str]]]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_cross_native_ensemble_oos_prediction("
        "cross_ensemble_artifact_id TEXT, station_id TEXT, "
        "selection_evidence_sha256 TEXT, system_family_ids_json TEXT)"
    )
    conn.executemany(
        "INSERT INTO calibration_cross_native_ensemble_oos_prediction "
        "VALUES(?,?,?,?)",
        [(ARTIFACT, s, sha, json.dumps(m, sort_keys=True)) for s, sha, m in rows],
    )
    return conn


REGISTRY = {IFS: "ECMWF", AIFS: "ECMWF_AIFS", "NOAA_GEFS_05": "NOAA_GEFS"}
SELECTION = "a" * 64
ARTIFACT = "cross-artifact-1"


def test_an_artifact_minted_under_the_split_is_served_on_the_registry() -> None:
    conn = _predictions([])
    caps = {"ECMWF": 0.75, "ECMWF_AIFS": 0.75, "NOAA_GEFS": 0.75}
    assert served_family_map(
        conn, station_id="ZUUU", cross_artifact_id=ARTIFACT, selection_sha=SELECTION,
        registry_family_by_system=REGISTRY, family_caps=caps,
    ) == REGISTRY


def test_an_artifact_minted_before_the_split_serves_under_its_certified_map() -> None:
    certified = {IFS: "ECMWF", AIFS: "ECMWF", "NOAA_GEFS_05": "NOAA_GEFS"}
    conn = _predictions([("ZUUU", SELECTION, certified)])
    caps = {"ECMWF": 0.75, "NOAA_GEFS": 0.75}
    assert served_family_map(
        conn, station_id="ZUUU", cross_artifact_id=ARTIFACT, selection_sha=SELECTION,
        registry_family_by_system=REGISTRY, family_caps=caps,
    ) == certified


def test_a_map_nobody_certified_fails_closed() -> None:
    caps = {"ECMWF": 0.75, "NOAA_GEFS": 0.75}
    # No certified predictions at all.
    with pytest.raises(ContractError, match="native_family_map_uncertified"):
        served_family_map(
            _predictions([]), station_id="ZUUU", cross_artifact_id=ARTIFACT, selection_sha=SELECTION,
            registry_family_by_system=REGISTRY, family_caps=caps,
        )
    # Certified under a map whose families the artifact's caps do not name.
    with pytest.raises(ContractError, match="native_family_map_uncertified"):
        served_family_map(
            _predictions([("ZUUU", SELECTION, REGISTRY)]),
            station_id="ZUUU", cross_artifact_id=ARTIFACT, selection_sha=SELECTION,
            registry_family_by_system=REGISTRY, family_caps=caps,
        )
    # Another station's predictions are not this artifact's.
    with pytest.raises(ContractError, match="native_family_map_uncertified"):
        served_family_map(
            _predictions([("KLGA", SELECTION, {**REGISTRY, AIFS: "ECMWF"})]),
            station_id="ZUUU", cross_artifact_id=ARTIFACT, selection_sha=SELECTION,
            registry_family_by_system=REGISTRY, family_caps=caps,
        )


def test_the_certified_map_is_a_seek_on_the_artifact_index() -> None:
    # The first cut selected by station and selection sha, which walked every
    # prediction the station had minted: ~11 s a compile on the live store.
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    plan = " ".join(
        str(row[-1])
        for row in conn.execute(
            "EXPLAIN QUERY PLAN " + CERTIFIED_FAMILY_MAP_SQL,
            ("a", "ZUUU", SELECTION),
        )
    )
    assert "USING INDEX" in plan and "cross_ensemble_artifact_id=?" in plan
    assert "SCAN" not in plan
