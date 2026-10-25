"""Charter 158: the TAF likelihood is tempered by whether its weather explains it
(2026-09-19, 「taf是人工在模型基础上加了一些看不见的信息…这些增量信息才是关键」→「上线」).

One line, four groups by the TAF's disagreement with P0's served mean and its
own cloud / precipitation; each group's exponent is multiplied by its lambda.
The lambdas serve only while the refit's walk-forward beats lambda = 1; an
artifact without them serves charter 150's numbers exactly.
"""
from __future__ import annotations

import importlib.util
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bayes_basket_target import taf_likelihood as TL
from bayes_basket_target.contracts import ContractError
from tests.test_charter_150_p1_joint_likelihood import _p0m, _root_with_centre, _taf_artifact

ROOT = Path(__file__).resolve().parents[1]
CENTRES = {"23": 23.0, "24": 24.0, "25": 25.0, "26": 26.0}


def _fitter():
    spec = importlib.util.spec_from_file_location(
        "_fit_taf_charter158", ROOT / "scripts" / "fit_taf_likelihood.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _tempered(state: str = TL.GATE_SERVE, **lambdas) -> dict:
    body = _taf_artifact()
    body[TL.TEMPERING_KEY] = {
        "rule": TL.TEMPERING_RULE,
        "state": state,
        "lambda_by_group": {g: 1.0 for g in TL.TEMPERING_GROUPS} | lambdas,
    }
    return body


@pytest.mark.parametrize(
    "x, cloud, precip, group",
    [
        (+2.0, 0.2, 0.1, TL.GROUP_REASONED),         # warm under clear, dry skies
        (-2.0, 0.3, 0.5, TL.GROUP_REASONED),         # cold with rain
        (-2.0, 0.8, 0.0, TL.GROUP_REASONED),         # cold under overcast
        (-2.0, 0.6, 0.29, TL.GROUP_UNREASONED_COLD),  # cold with neither
        (+2.0, 0.7, 0.0, TL.GROUP_OTHER),            # warm under cloud
        (+2.0, 0.2, 0.4, TL.GROUP_OTHER),            # warm with rain
        (-0.9, 0.0, 0.0, TL.GROUP_SMALL),            # under a degree
        (+1.0, 0.4, 0.0, TL.GROUP_REASONED),         # the edges are inclusive
        (-3.0, None, 0.0, TL.GROUP_SMALL),           # no weather
        (None, 0.2, 0.1, TL.GROUP_SMALL),            # no served mean
    ],
)
def test_the_group_reads_direction_and_the_taf_s_own_weather(x, cloud, precip, group) -> None:
    assert TL.disagreement_group(x, cloud, precip) == group


def test_lambda_serves_only_under_its_own_gate() -> None:
    assert TL.tempering_lambda(_taf_artifact(), TL.GROUP_UNREASONED_COLD) == 1.0
    closed = _tempered(TL.GATE_IDENTITY, UNREASONED_COLD=0.5)
    assert TL.tempering_lambda(closed, TL.GROUP_UNREASONED_COLD) == 1.0
    opened = _tempered(UNREASONED_COLD=0.5)
    assert TL.tempering_lambda(opened, TL.GROUP_UNREASONED_COLD) == 0.5
    assert TL.tempering_lambda(opened, TL.GROUP_REASONED) == 1.0
    # A group the table does not name reads 1.
    del opened[TL.TEMPERING_KEY]["lambda_by_group"][TL.GROUP_OTHER]
    assert TL.tempering_lambda(opened, TL.GROUP_OTHER) == 1.0


@pytest.mark.parametrize("value", [float("nan"), -0.25, 3.5, "x"])
def test_a_malformed_lambda_is_refused(value) -> None:
    with pytest.raises(ContractError):
        TL.tempering_lambda(_tempered(UNREASONED_COLD=value), TL.GROUP_UNREASONED_COLD)


def test_the_tempered_likelihood_is_the_line_to_the_power_lambda() -> None:
    kw = dict(station_id="ZUUU", tx_c=22.0, root_center_c=24.0,
              bucket_centers_c=CENTRES, buckets=list(CENTRES))
    weather = dict(root_mean_c=24.2, taf_cloud_cover=0.3, taf_precip_prob=0.1)
    plain, _ = TL.compile_taf_likelihood(_taf_artifact(), **kw)
    tempered, receipt = TL.compile_taf_likelihood(
        _tempered(UNREASONED_COLD=0.5), **kw, **weather
    )
    for bucket in CENTRES:
        assert tempered[bucket] == pytest.approx(plain[bucket] ** 0.5)
    assert receipt["tempering"]["group"] == TL.GROUP_UNREASONED_COLD
    assert receipt["tempering"]["lambda"] == 0.5
    assert receipt["tempering"]["x_served_c"] == pytest.approx(-2.2)
    # The same TAF with rain in it keeps the line's full pull.
    wet, receipt = TL.compile_taf_likelihood(
        _tempered(UNREASONED_COLD=0.5), **kw,
        root_mean_c=24.2, taf_cloud_cover=0.3, taf_precip_prob=0.6,
    )
    assert wet == plain and receipt["tempering"]["group"] == TL.GROUP_REASONED


def test_without_the_block_charter_150_is_served_unchanged() -> None:
    kw = dict(station_id="ZUUU", tx_c=22.0, root_center_c=24.0,
              bucket_centers_c=CENTRES, buckets=list(CENTRES))
    bare, r1 = TL.compile_taf_likelihood(_taf_artifact(), **kw)
    with_weather, r2 = TL.compile_taf_likelihood(
        _taf_artifact(), **kw, root_mean_c=24.2, taf_cloud_cover=0.3, taf_precip_prob=0.1
    )
    assert bare == with_weather
    x = 22.0 - 24.0
    for bucket, centre in CENTRES.items():
        assert bare[bucket] == pytest.approx(math.exp(-((x - 0.5 * (centre - 24.0)) ** 2) / 2.0))
    assert r1["tempering"]["state"] == "ABSENT" and r2["tempering"]["lambda"] == 1.0


def test_an_open_line_gate_leaves_the_tempering_gate_alone() -> None:
    served = _tempered(TL.GATE_IDENTITY, UNREASONED_COLD=0.5)
    served["gate"] = {"state": TL.GATE_SERVE}
    assert served[TL.TEMPERING_KEY]["state"] == TL.GATE_IDENTITY
    assert TL.tempering_lambda(served, TL.GROUP_UNREASONED_COLD) == 1.0


def test_p0m_groups_on_the_root_s_served_mean_and_the_capture_s_weather() -> None:
    root = _root_with_centre()
    mean = sum(float(b) * p for b, p in root.probabilities.items()) / sum(root.probabilities.values())
    artifact = _tempered(UNREASONED_COLD=0.5)
    tx = round(mean) - 3.0
    from bayes_basket_target.materialization import materialize_p0m_observed

    def seat(cloud, precip):
        return materialize_p0m_observed(
            root, p0_root=root, forecast_revision_id="taf-1",
            forecast_source_object_sha256="e" * 64, taf_tx_c=tx,
            decision_at_utc="2026-07-23T05:00:00Z", fresh_until_utc="2026-07-23T06:00:00Z",
            bridge_sha256="b" * 64, taf_likelihood_artifact=artifact,
            taf_cloud_cover=cloud, taf_precip_prob=precip,
        )

    dry = seat(0.2, 0.0)
    receipt = json.loads(dry.lineage[TL.LINEAGE_KEY])
    assert dry.posterior_version == "p0m_taf_likelihood_v37"
    assert receipt["tempering"]["group"] == TL.GROUP_UNREASONED_COLD
    assert receipt["tempering"]["x_served_c"] == pytest.approx(tx - mean)
    x = tx - 38.0
    weighted = {
        b: p * math.exp(-0.5 * (x - 0.5 * (float(b) - 38.0)) ** 2 / 2.0)
        for b, p in root.probabilities.items()
    }
    total = sum(weighted.values())
    for bucket, value in weighted.items():
        assert dry.probabilities[bucket] == pytest.approx(value / total)
    # Rain explains the cold TAF: the line pulls at full strength, as v7 did.
    wet = seat(0.2, 0.8)
    v7 = _p0m(root, _taf_artifact(), tx=tx)
    assert json.loads(wet.lineage[TL.LINEAGE_KEY])["tempering"]["group"] == TL.GROUP_REASONED
    for bucket in root.probabilities:
        assert wet.probabilities[bucket] == pytest.approx(v7.probabilities[bucket])
    # The cold, dry TAF pulls less far than the wet one.
    centre = lambda env: sum(float(b) * p for b, p in env.probabilities.items())
    assert centre(wet) < centre(dry) < mean


def _case(day: int, station: str, tx: float, truth_bucket: str, cloud: float, precip: float) -> dict:
    centres = {str(k): float(k) for k in range(20, 29)}
    probabilities = {b: math.exp(-((c - 24.0) ** 2) / 2.0) for b, c in centres.items()}
    total = sum(probabilities.values())
    probabilities = {b: v / total for b, v in probabilities.items()}
    # The TAF lands the afternoon before the day; the truth commits the
    # morning after it (the walk-forward is strictly as-of these moments).
    midnight = datetime(2026, 9, day, tzinfo=timezone.utc)
    decided = (midnight - timedelta(hours=12)).timestamp()
    committed = (midnight + timedelta(days=1, hours=2)).timestamp()
    return {
        "station_id": station, "event_date": f"2026-09-{day:02d}", "tx_c": tx,
        "decided_at": decided,
        "truth_revisions": [(committed, 0, float(truth_bucket), truth_bucket)],
        "c0": 24.0, "centers": centres, "probabilities": probabilities,
        "root_mean_c": 24.0, "truth_bucket": truth_bucket, "truth_c": float(truth_bucket),
        "cloud_cover": cloud, "precip_prob": precip,
        "group": TL.disagreement_group(tx - 24.0, cloud, precip), "weight": 1.0,
    }


def test_the_refit_tempers_a_cold_taf_whose_days_do_not_follow_it() -> None:
    fitter = _fitter()
    cases = []
    for day in range(1, 29):
        for i in range(6):
            station = f"S{i}"
            # Warm with reason: the day follows the TAF.
            cases.append(_case(day, station, 27.0, "27", 0.1, 0.0))
            # Cold without reason: the day stays where P0 had it.
            cases.append(_case(day, station + "c", 21.0, "24" if i % 3 else "22", 0.2, 0.0))
    line_cases = cases
    line = {"schema": TL.SCHEMA, "gate": {"state": TL.GATE_SERVE}, **fitter.fit(line_cases)}
    lambdas, weights = fitter.fit_tempering(line, cases)
    assert lambdas[TL.GROUP_UNREASONED_COLD] < 1.0
    assert lambdas[TL.GROUP_REASONED] == 1.0            # not a tempered group
    assert weights[TL.GROUP_UNREASONED_COLD] == pytest.approx(28 * 6)
    gate = fitter.walk_forward_tempering(line_cases, cases)
    assert gate["state"] == TL.GATE_SERVE
    assert gate["by_day_mean_gain_vs_lambda_one_nat"] > 0.0
    # A group too thin to fit keeps 1.
    thin, _ = fitter.fit_tempering(line, cases[:10])
    assert thin[TL.GROUP_UNREASONED_COLD] == 1.0


def test_the_refit_writes_identity_when_tempering_does_not_pay(monkeypatch) -> None:
    fitter = _fitter()
    cases = []
    for day in range(1, 29):
        for i in range(6):
            # Cold without reason, and the day does follow it.
            cases.append(_case(day, f"S{i}", 21.0, "21", 0.2, 0.0))
            cases.append(_case(day, f"W{i}", 24.0, "24", 0.2, 0.0))
    # Whatever the earlier days pick, a lambda that loses on the day it had
    # not seen is not served.
    monkeypatch.setattr(
        fitter, "fit_tempering",
        lambda line, rows: ({g: (0.0 if g == TL.GROUP_UNREASONED_COLD else 1.0)
                             for g in TL.TEMPERING_GROUPS}, {}),
    )
    gate = fitter.walk_forward_tempering(cases, cases)
    assert gate["state"] == TL.GATE_IDENTITY
    assert gate["by_day_mean_gain_vs_lambda_one_nat"] < 0.0
    too_few = fitter.walk_forward_tempering(cases[:12 * 9], cases[:12 * 9])
    assert too_few["state"] == TL.GATE_IDENTITY and too_few["reason"] == "too_few_days"
