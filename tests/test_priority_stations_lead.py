"""User ruling 2026-10-22: the priority stations lead every per-scope ordering."""

from bayes_basket_target import production_worker as pw


def test_priority_station_rows_lead_stably(monkeypatch):
    monkeypatch.setattr(pw, "PRIORITY_STATIONS", frozenset({"ZUUU"}))
    rows = [
        ("EGLC", "2026-10-22"),
        ("ZUUU", "2026-10-21"),
        ("KLGA", "2026-10-22"),
        ("ZUUU", "2026-10-22"),
    ]
    assert pw._priority_scopes_first(rows) == [
        ("ZUUU", "2026-10-21"),
        ("ZUUU", "2026-10-22"),
        ("EGLC", "2026-10-22"),
        ("KLGA", "2026-10-22"),
    ]


def test_empty_priority_keeps_order(monkeypatch):
    monkeypatch.setattr(pw, "PRIORITY_STATIONS", frozenset())
    rows = [("ZUUU", "2026-10-22"), ("EGLC", "2026-10-22")]
    assert pw._priority_scopes_first(rows) == rows
    assert not pw._priority_station("ZUUU")


def test_default_priority_is_zuuu():
    assert "ZUUU" in pw.PRIORITY_STATIONS or pw.os.environ.get(
        "PM_PRIORITY_STATIONS"
    ) is not None
