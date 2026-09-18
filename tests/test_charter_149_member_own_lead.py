"""Charter 149: a member is calibrated by the fit of its own run's lead.

The packet has one ``training_lead_bucket`` -- the cross weights are fitted
per lead -- but a system riding another lead (a split rider) was fitted by
the trainer under its own hour lead.  Production asked under the packet's
lead instead, so at ZUUU every h12_24 packet handed ICON and ECCC (12Z,
h24_48) their 00Z h12_24 fit (+1.32C / +0.76C) while their own 12Z h24_48
fit (+0.86C / +1.14C) sat unread; 30% of the fleet's P0 envelopes between
09-12 and 09-18 served some member under the wrong lead.

The fixture already carries a split rider: ECMWF_AIFS_TEST's 00Z run sits
24h ahead of the event day (h12_24) inside an h24_48 packet, and its only
fit is h24_48.  Each test below adds or withholds a fit under its own lead.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.native_ensemble_p0 import compile_native_ensemble_p0
from tests.test_native_ensemble_p0 import (  # noqa: F401  (autouse fixtures)
    _contest_era_membership,
    _install_mixed_cycle_calibrations,
    _map_the_fixture_station_season,
    _mixed_packet,
)

RIDER = "ECMWF_AIFS_TEST"
DECISION_AT = "2026-07-01T13:00:00Z"


def _clone_fit(
    conn: sqlite3.Connection,
    *,
    lead_bucket: str,
    run_cycle: str,
    bias_shift_c: float,
) -> str:
    """Copy the rider's h24_48 00Z fit under another (lead, cycle)."""

    columns = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(calibration_native_ensemble_system_artifact)"
        )
    ]
    artifact = dict(zip(columns, conn.execute(
        "SELECT * FROM calibration_native_ensemble_system_artifact"
        " WHERE ensemble_system_id=? AND lead_bucket='h24_48'"
        " AND run_cycle='00Z'",
        (RIDER,),
    ).fetchone()))
    bias = json.loads(artifact["bias_parameters_json"])
    bias["bias_c"] = float(bias.get("bias_c") or 0.0) + bias_shift_c
    bias.pop("correction_form", None)
    bias.pop("bias_bin_cuts_c", None)
    bias.pop("bias_bin_shifts_c", None)
    artifact.update({
        "system_artifact_id": (
            f"native-system-artifact-{RIDER}-{lead_bucket}-{run_cycle}"
        ),
        "lead_bucket": lead_bucket,
        "run_cycle": run_cycle,
        "bias_parameters_json": json.dumps(bias, sort_keys=True),
    })
    artifact["content_sha256"] = content_sha256({
        key: value for key, value in artifact.items()
        if key != "content_sha256"
    })
    conn.execute(
        "INSERT INTO calibration_native_ensemble_system_artifact("
        + ",".join(artifact) + ") VALUES("
        + ",".join("?" for _ in artifact) + ")",
        tuple(artifact.values()),
    )
    act_columns = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(calibration_native_ensemble_activation)"
        )
    ]
    activation = dict(zip(act_columns, conn.execute(
        "SELECT * FROM calibration_native_ensemble_activation"
        " WHERE artifact_kind='NATIVE_ENSEMBLE_SYSTEM_CALIBRATION'"
        " AND ensemble_system_id_or_star=? AND lead_bucket='h24_48'"
        " AND run_cycle='00Z'",
        (RIDER,),
    ).fetchone()))
    cycles = {RIDER: run_cycle}
    activation.update({
        "activation_id": (
            f"native-system-activation-{RIDER}-{lead_bucket}-{run_cycle}"
        ),
        "artifact_id": artifact["system_artifact_id"],
        "lead_bucket": lead_bucket,
        "run_cycle": run_cycle,
        "run_cycle_by_system_json": json.dumps(
            cycles, sort_keys=True, separators=(",", ":")
        ),
        "run_cycle_by_system_sha256": content_sha256(cycles),
    })
    activation["content_sha256"] = content_sha256({
        key: value for key, value in activation.items()
        if key != "content_sha256"
    })
    conn.execute(
        "INSERT INTO calibration_native_ensemble_activation("
        + ",".join(activation) + ") VALUES("
        + ",".join("?" for _ in activation) + ")",
        tuple(activation.values()),
    )
    conn.commit()
    return str(artifact["content_sha256"])


def _lineage(conn: sqlite3.Connection) -> dict[str, object]:
    compiled = compile_native_ensemble_p0(
        conn, forward_packet=_mixed_packet(), decision_at_utc=DECISION_AT,
    )
    return compiled["packet"]["native_probability_envelope"]["lineage"]


def _raw_centre() -> float:
    packet = _mixed_packet()
    for system in packet["systems"]:
        if system["ensemble_system_id"] == RIDER:
            values = [float(m["raw_tmax_c"]) for m in system["members"]]
            return sum(values) / len(values)
    raise AssertionError("rider missing from the fixture packet")


def test_the_fixture_rider_is_split_and_has_no_fit_under_its_own_lead() -> None:
    """Without an own-lead fit the old key is the last certified fallback."""

    conn = sqlite3.connect(":memory:")
    _install_mixed_cycle_calibrations(conn)
    lineage = _lineage(conn)
    assert lineage["training_lead_bucket"] == "h24_48"
    assert json.loads(lineage["hour_lead_bucket_by_system_json"])[RIDER] == (
        "h12_24"
    )
    assert RIDER in json.loads(lineage["hour_bucket_split_riders_json"])
    leads = json.loads(lineage["system_calibration_lead_by_system_json"])
    # No h12_24 fit exists, so the rider stays certified under the packet's
    # lead exactly as before -- not demoted to the peer path.
    assert leads[RIDER] == "h24_48"
    assert RIDER not in json.loads(
        lineage["provisional_parameter_sources_json"]
    )
    # A system on the packet's own lead asks one lead, as it always did.
    for system, lead in leads.items():
        if system != RIDER:
            assert lead == "h24_48"


def test_a_rider_is_served_its_own_leads_fit_when_one_exists() -> None:
    conn = sqlite3.connect(":memory:")
    _install_mixed_cycle_calibrations(conn)
    before = json.loads(
        _lineage(conn)["native_calibrated_system_centers_c_json"]
    )[RIDER]
    own_sha = _clone_fit(
        conn, lead_bucket="h12_24", run_cycle="00Z", bias_shift_c=0.5,
    )
    lineage = _lineage(conn)
    assert json.loads(lineage["system_calibration_lead_by_system_json"])[
        RIDER
    ] == "h12_24"
    assert json.loads(lineage["system_artifact_shas_json"])[RIDER] == own_sha
    assert RIDER not in json.loads(
        lineage["provisional_uncalibrated_system_ids_json"]
    )
    after = json.loads(
        lineage["native_calibrated_system_centers_c_json"]
    )[RIDER]
    assert abs((after - before) - 0.5) < 1e-6
    assert abs(after - (_raw_centre() + (before - _raw_centre()) + 0.5)) < 1e-6


def test_the_cross_cycle_borrow_reads_the_riders_own_lead_first() -> None:
    """Charter 101's borrow, asked at the lead the member actually has.

    The own lead holds a fit only at another cycle; the packet lead holds
    the exact cycle.  The member's lead outranks the member's cycle: it
    borrows the other-cycle own-lead fit rather than being certified under
    the packet lead's same-cycle one.
    """

    conn = sqlite3.connect(":memory:")
    _install_mixed_cycle_calibrations(conn)
    _clone_fit(
        conn, lead_bucket="h12_24", run_cycle="18Z", bias_shift_c=0.7,
    )
    lineage = _lineage(conn)
    sources = json.loads(lineage["provisional_parameter_sources_json"])
    assert sources[RIDER]["source"] == "OWN_FIT_OTHER_RUN_CYCLE"
    assert sources[RIDER]["borrowed_from_run_cycle"] == "18Z"
    assert sources[RIDER]["borrowed_from_lead_bucket"] == "h12_24"
    assert json.loads(lineage["system_calibration_lead_by_system_json"])[
        RIDER
    ] == "h12_24"
    assert RIDER in json.loads(
        lineage["provisional_uncalibrated_system_ids_json"]
    )


def test_the_charter_names_the_clause_and_its_narrowing_of_101() -> None:
    charter = (
        Path(__file__).resolve().parents[1]
        / "docs" / "constitution" / "SYSTEM_CHARTER.md"
    ).read_text(encoding="utf-8")
    assert "\n149. " in charter
    assert "第 149 条收窄" in charter
