"""Two processes reading JAXA hours into one cache file keep each other's reads."""

from __future__ import annotations

import json

from bayes_basket_target import jaxa_ptree_radiation as jaxa


def test_persist_keeps_hours_another_process_wrote(tmp_path, monkeypatch) -> None:
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"schema": jaxa.CACHE_SCHEMA, "hours": {"2026-10-20T00:00Z": {"RKPK": 77.9}}}))
    monkeypatch.setattr(jaxa, "CACHE_PATH", path)
    monkeypatch.setattr(jaxa, "_CACHE", None)
    monkeypatch.setattr(jaxa, "_CACHE_STAMP", None)
    monkeypatch.setattr(jaxa, "_CLP", {})
    monkeypatch.setattr(jaxa, "_L2_STATE", {"pending": False, "checked": -1e9})
    mine = jaxa._load()
    # Another process reads 01Z and writes the file after this one loaded.
    path.write_text(json.dumps({"schema": jaxa.CACHE_SCHEMA, "hours": {
        "2026-10-20T00:00Z": {"RKPK": 77.9}, "2026-10-20T01:00Z": {"RKPK": 93.2}},
        "l2_latest": {"scan_utc": "2026-10-20T01:20Z", "values": {"RKPK": 90.0}}}))
    mine["2026-10-20T02:00Z"] = {"RKPK": 85.2}
    jaxa._persist(mine)
    hours = json.loads(path.read_text())["hours"]
    assert set(hours) == {"2026-10-20T00:00Z", "2026-10-20T01:00Z", "2026-10-20T02:00Z"}
    assert json.loads(path.read_text())["l2_latest"]["scan_utc"] == "2026-10-20T01:20Z"


def test_load_folds_in_a_newer_file(tmp_path, monkeypatch) -> None:
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"schema": jaxa.CACHE_SCHEMA, "hours": {"2026-10-20T00:00Z": {"RKPK": 77.9}}}))
    monkeypatch.setattr(jaxa, "CACHE_PATH", path)
    monkeypatch.setattr(jaxa, "_CACHE", None)
    monkeypatch.setattr(jaxa, "_CACHE_STAMP", None)
    monkeypatch.setattr(jaxa, "_CLP", {})
    monkeypatch.setattr(jaxa, "_L2_STATE", {"pending": False, "checked": -1e9})
    jaxa._load()
    path.write_text(json.dumps({"schema": jaxa.CACHE_SCHEMA, "hours": {"2026-10-20T01:00Z": {"RKPK": 93.2}}}))
    import os
    os.utime(path, ns=(1, 2))   # a different mtime even within one clock tick
    assert jaxa.measured("RKPK", []) == {}
    assert set(jaxa._load()) == {"2026-10-20T00:00Z", "2026-10-20T01:00Z"}
