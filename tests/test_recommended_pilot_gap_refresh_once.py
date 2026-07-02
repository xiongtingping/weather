from __future__ import annotations

from pathlib import Path
import sys
import json

from scripts import recommended_pilot_gap_refresh_once as once


def test_build_commands_runs_market_model_then_market_for_each_slug() -> None:
    rows = [
        {
            "slug": "highest-temperature-in-ankara-on-june-1-2026",
            "event_date": "2026-06-01",
        }
    ]

    commands = once.build_commands(
        python_bin="python",
        rows=rows,
        market_refresh=True,
        model_refresh=True,
        materialize_facts=True,
        facts_date="2026-05-31",
    )

    assert [phase for _slug, phase, _cmd in commands] == [
        "market_refresh",
        "model_refresh",
        "post_model_market_refresh",
        "materialize_facts",
    ]
    assert commands[0][2][-2:] == ["--market-refresh-only", "--no-auto-trade"]
    assert "--force-model-refresh" in commands[1][2]
    assert commands[0][2][commands[0][2].index("--date") + 1] == "2026-06-01"
    assert commands[-1][2] == [
        "python",
        "ui/server.py",
        "--materialize-facts",
        "--facts-date",
        "2026-05-31",
        "--facts-days",
        "1",
    ]


def test_build_commands_can_disable_model_refresh() -> None:
    rows = [
        {
            "slug": "highest-temperature-in-ankara-on-june-2-2026",
            "event_date": "2026-06-02",
        }
    ]

    commands = once.build_commands(
        python_bin="python",
        rows=rows,
        market_refresh=True,
        model_refresh=False,
        materialize_facts=False,
        facts_date="2026-05-31",
    )

    assert [phase for _slug, phase, _cmd in commands] == ["market_refresh"]


def test_market_rows_follow_priority_slugs() -> None:
    plan = {
        "priority_slugs": [
            "highest-temperature-in-ankara-on-june-1-2026",
            "highest-temperature-in-atlanta-on-june-1-2026",
        ],
        "plan": [
            {
                "open_markets": [
                    {
                        "slug": "highest-temperature-in-ankara-on-june-1-2026",
                        "event_date": "2026-06-01",
                    },
                    {
                        "slug": "highest-temperature-in-ankara-on-june-2-2026",
                        "event_date": "2026-06-02",
                    },
                ]
            },
            {
                "open_markets": [
                    {
                        "slug": "highest-temperature-in-atlanta-on-june-1-2026",
                        "event_date": "2026-06-01",
                    }
                ]
            },
        ],
    }

    rows = once._market_rows(plan, max_slugs=2)

    assert [row["slug"] for row in rows] == [
        "highest-temperature-in-ankara-on-june-1-2026",
        "highest-temperature-in-atlanta-on-june-1-2026",
    ]


def test_health_blocked_filter_limits_rows_and_phases(tmp_path: Path) -> None:
    health_path = tmp_path / "health.json"
    health_path.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "slug": "highest-temperature-in-atlanta-on-june-2-2026",
                        "blockers": ["tick_missing"],
                    },
                    {
                        "slug": "highest-temperature-in-ankara-on-june-2-2026",
                        "blockers": ["model_missing", "fact_missing"],
                    },
                    {
                        "slug": "highest-temperature-in-ankara-on-june-1-2026",
                        "blockers": [],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "slug": "highest-temperature-in-atlanta-on-june-2-2026",
            "event_date": "2026-06-02",
        },
        {
            "slug": "highest-temperature-in-ankara-on-june-2-2026",
            "event_date": "2026-06-02",
        },
        {
            "slug": "highest-temperature-in-ankara-on-june-1-2026",
            "event_date": "2026-06-01",
        },
    ]

    phase_map = once._health_blocked_phase_map(health_path)
    filtered = once._apply_health_blocked_filter(rows, phase_map)
    commands = once.build_commands(
        python_bin="python",
        rows=filtered,
        market_refresh=True,
        model_refresh=True,
        materialize_facts=False,
        facts_date="2026-06-02",
    )

    assert [row["slug"] for row in filtered] == [
        "highest-temperature-in-atlanta-on-june-2-2026",
        "highest-temperature-in-ankara-on-june-2-2026",
    ]
    assert [(slug, phase) for slug, phase, _cmd in commands] == [
        ("highest-temperature-in-atlanta-on-june-2-2026", "market_refresh"),
        ("highest-temperature-in-ankara-on-june-2-2026", "model_refresh"),
    ]


def test_run_returns_structured_timeout() -> None:
    result = once._run(
        [
            sys.executable,
            "-c",
            "import time; print('started'); time.sleep(2)",
        ],
        cwd=Path.cwd(),
        dry_run=False,
        timeout_sec=1,
    )

    assert result["returncode"] == 124
    assert result["timed_out"] is True
    assert result["timeout_sec"] == 1


def test_run_returns_structured_startup_error() -> None:
    result = once._run(
        ["definitely-missing-python-for-refresh-test"],
        cwd=Path.cwd(),
        dry_run=False,
        timeout_sec=1,
    )

    assert result["returncode"] == 127
    assert result["startup_error"] == "FileNotFoundError"


def test_main_writes_refresh_once_report(tmp_path: Path, capsys) -> None:
    plan_path = tmp_path / "plan.json"
    out_path = tmp_path / "refresh_once.json"
    plan_path.write_text(
        json.dumps(
            {
                "priority_slugs": ["highest-temperature-in-ankara-on-june-1-2026"],
                "plan": [
                    {
                        "open_markets": [
                            {
                                "slug": "highest-temperature-in-ankara-on-june-1-2026",
                                "event_date": "2026-06-01",
                            }
                        ]
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    old_argv = sys.argv
    try:
        sys.argv = [
            "recommended_pilot_gap_refresh_once.py",
            "--plan",
            str(plan_path),
            "--out",
            str(out_path),
            "--no-only-health-blocked",
            "--dry-run",
            "--json",
        ]
        rc = once.main()
    finally:
        sys.argv = old_argv

    assert rc == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["row_count"] == 1
    assert payload["results"][0]["dry_run"] is True
    assert json.loads(capsys.readouterr().out)["row_count"] == 1
