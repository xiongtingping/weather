"""The weekly refit of the composite ΔT card needs admission (user 2026-10-28 「加准入门」): a fit
whose procedure, held out of the newest days, scores worse there than the card in use is not
written, and every run leaves a receipt."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docs" / "research" / "tmax_physics_m3_eval_20261021"))
import refit_gate as g  # noqa: E402


def _fit(version: str, sd: float = 1.0) -> dict:
    return {"version": version, "by_hour": {"13": {"fleet_resid_sd_c": sd}}}


@pytest.fixture()
def gate(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "RECEIPT", tmp_path / "refit_gate.json")
    monkeypatch.setattr(g, "INCUMBENT", tmp_path / "card.json")
    (tmp_path / "card.json").write_text(json.dumps(_fit("old")))
    rows = [{"station": "ZUUU", "date": f"2026-10-{d:02d}"} for d in range(1, 31)]
    return tmp_path, rows


def _scores(monkeypatch, probe_ls: float, card_ls: float, probe_err: float = 0.5) -> None:
    def score(rows, fit):
        ls = probe_ls if fit["version"] == "probe" else card_ls
        return {(r["station"], r["date"], 13): (2.0, 2.0 - probe_err, ls) for r in rows}

    monkeypatch.setattr(g, "score", score)


def test_a_probe_no_worse_than_the_card_is_admitted(gate, monkeypatch) -> None:
    tmp, rows = gate
    _scores(monkeypatch, probe_ls=1.31, card_ls=1.30)
    held: list = []
    v = g.judge(rows, _fit("new"), lambda r: held.append(r) or _fit("probe"))
    assert v["admitted"] and v["summary"]["held_out"] == ["2026-10-17", "2026-10-30"]
    assert all(r["date"] < "2026-10-17" for r in held[0])       # the probe never saw the held-out days
    assert json.loads((tmp / "refit_gate.json").read_text())["admitted"]


def test_a_probe_worse_than_the_card_beyond_tolerance_is_refused(gate, monkeypatch) -> None:
    tmp, rows = gate
    _scores(monkeypatch, probe_ls=1.40, card_ls=1.30)
    v = g.judge(rows, _fit("new"), lambda r: _fit("probe"))
    assert not v["admitted"] and "worse than the card in use" in v["reason"]
    assert not json.loads((tmp / "refit_gate.json").read_text())["admitted"]


def test_a_probe_that_does_not_beat_the_day_ahead_forecast_is_refused(gate, monkeypatch) -> None:
    _, rows = gate
    _scores(monkeypatch, probe_ls=1.0, card_ls=1.3, probe_err=2.5)
    v = g.judge(rows, _fit("new"), lambda r: _fit("probe"))
    assert not v["admitted"] and "does not beat the day-ahead" in v["reason"]


def test_new_rows_that_blow_the_residuals_up_are_refused(gate, monkeypatch) -> None:
    _, rows = gate
    _scores(monkeypatch, probe_ls=1.30, card_ls=1.30)
    v = g.judge(rows, _fit("new", sd=1.5), lambda r: _fit("probe", sd=1.0))
    assert not v["admitted"] and "blow the fit up" in v["reason"]


def test_without_a_card_in_use_only_the_absolute_checks_apply(gate, monkeypatch) -> None:
    tmp, rows = gate
    (tmp / "card.json").unlink()
    _scores(monkeypatch, probe_ls=9.0, card_ls=0.0)
    v = g.judge(rows, _fit("new"), lambda r: _fit("probe"))
    assert v["admitted"] and v["summary"]["logscore_diff"] is None
