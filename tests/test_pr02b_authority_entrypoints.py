from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from strict_asof_forecast_p1_ingress_writer import (
    StrictForecastP1IngressError,
    persist_tier_a_forecast_p1_proposal,
)


ROOT = Path(__file__).resolve().parents[1]

RETIRED_ENTRYPOINTS = (
    "scripts/replay_bayes_basket_target_p1.py",
    "scripts/commit_bayes_basket_target_p1_no_live_replay.py",
    "scripts/materialize_45station_p1_no_live.py",
    "scripts/activate_p1_schema_and_forecast_ingress.py",
    "scripts/append_strict_asof_forecast_p1_ingress.py",
    "scripts/p1_forecast_ingress_staging_probe.py",
    "scripts/materialize_official_nwp_daily_peak_p1_ingress.py",
    "scripts/append_official_nwp_daily_peak_p1_ingress.py",
    "official_nwp_daily_peak_strict_asof_runtime.py",
    "scripts/polymarket_monitor_daemon.sh",
    "scripts/amsc_zuuu_metar_taf_poll_once.sh",
    "scripts/watch_zuuu_speci_once.py",
    "scripts/run_amsc_zuuu_metar_taf_poll_immutable.sh",
    "scripts/task_queue_worker.py",
    "scripts/enqueue_task.py",
    "scripts/apply_amsc_zuuu_fact_snapshot_plan.py",
    "scripts/weatherctl.sh",
    "scripts/weather_monitor_launchagent_entrypoint.sh",
    "scripts/weather_service_control.sh",
    "scripts/install_monitor_launchdaemon.sh",
    "scripts/install_launchdaemon.sh",
    "scripts/install_passwordless_service_control.sh",
    "scripts/WeatherControl.applescript",
    "scripts/build_weather_control_app.sh",
    "scripts/cutover_reported_x_probability_epoch.sh",
    "scripts/cutover_cumulative_strong_likelihood_v5.sh",
    "scripts/cutover_physical_state_bayes_v6_production.sh",
    "scripts/cutover_physical_state_bayes_v6_runtime_hotfix.sh",
    "scripts/cutover_amsc_slot_complete_v6_and_trade_caps.sh",
    "scripts/recover_archive_after_failed_slot_complete_cutover.sh",
    "scripts/refresh_reported_x_runtime_manifest_epoch.sh",
    "scripts/amsc_zuuu_metar_taf_poll_once.slot_complete_candidate.sh",
    "scripts/run_amsc_p10_shadow_source_sidecar.py",
    "scripts/apply_amsc_probability_fast_lane_v6_from_plan.py",
    "scripts/apply_amsc_taf_forecast_revision_from_plan.py",
    "scripts/amsc_probability_pending_queue.py",
    "identifiable_physical_probability_worker.py",
    "production_probability_v6_amsc_archive_ingress.py",
    "production_amsc_archive_probability_ingress.py",
    "production_reported_x_amsc_archive_probability_ingress.py",
    "p2_canonical_amsc_forward_writer.py",
    "scripts/materialize_production_probability_activation.py",
    "scripts/materialize_reported_x_successor_activation.py",
    "production_probability_v6_weak_revision_event_runtime.py",
    "scripts/apply_amsc_taf_forecast_revision_v6_from_plan.py",
    "scripts/apply_current_weak_source_revisions_v6.py",
)


def test_retired_p1_authority_entrypoints_are_absent() -> None:
    present = [path for path in RETIRED_ENTRYPOINTS if (ROOT / path).exists()]
    assert present == []


def test_forecast_or_nwp_p1_writer_rejects_before_any_sql() -> None:
    conn = sqlite3.connect(":memory:")
    calls: list[tuple[object, ...]] = []

    def deny(*args: object) -> int:
        calls.append(args)
        return sqlite3.SQLITE_DENY

    conn.set_authorizer(deny)
    with pytest.raises(
        StrictForecastP1IngressError,
        match="forecast_or_nwp_cannot_materialize_p1_use_p0_or_p0m_pipeline",
    ):
        persist_tier_a_forecast_p1_proposal(conn, {"authority_scope": "FORMAL"})
    assert calls == []


def test_resident_worker_retains_only_ordered_probability_stages() -> None:
    worker = (ROOT / "bayes_basket_target" / "production_worker.py").read_text(
        encoding="utf-8"
    )
    p0 = worker.index("p0 = self.materialize_resident_p0")
    p0m = worker.index("p0m = self._materialize_resident_p0m")
    p1 = worker.index("p1 = self._materialize_resident_p1")
    assert p0 < p0m < p1
    assert '"stage_order": ["P0", "P0-M", "P1"]' in worker


def test_only_three_user_domain_services_and_targetctl_remain() -> None:
    installer = (ROOT / "scripts" / "install_launchagent.sh").read_text(
        encoding="utf-8"
    )
    targetctl = (ROOT / "scripts" / "targetctl.sh").read_text(encoding="utf-8")
    # The guard this test exists for is the installer's own list: it manages
    # three user-domain services and no more.  The repository's launchd
    # directory has grown past that, and the growth is accounted for --
    # four of the eight extras were folded into the resident daemon's
    # hosted sweeps on 2026-08-24 and are installed disabled, and four run
    # on their own clocks for reasons recorded below.  Pinning the
    # directory to the known set keeps a new one from arriving unnoticed
    # without pretending the four superseded definitions are not on disk.
    installer_managed = [
        "com.weather.clob_order_service.plist",
        "com.weather.market_ws.plist",
        "com.weather.weather_basket_yes.plist",
    ]
    superseded_by_hosted_sweeps = [
        "com.weather.maker_shadow_probe.plist",
        "com.weather.paper_trading_ledger_sweep.plist",
        "com.weather.pr12_awc_observation_sweep.plist",
        "com.weather.settlement_truth_sweep.plist",
    ]
    # On their own timers because a resident that hosts them is the wrong
    # owner, each for its own recorded reason.
    own_clock = [
        "com.weather.official_nwp_raw_retention.plist",
        # 2026-09-09: the render was a hosted sweep with a 2400s timeout,
        # and a render that needs longer is SIGTERMed before it writes --
        # so it wrote nothing, refreshed no cached section, and the next
        # one started colder and was killed sooner in its own work.  A
        # render is a reader: nothing waits on it, so it left the resident
        # for launchd, which imposes no timeout.  2026-09-23 (user ruling
        # 「加超时」): a render throttled by ``taskpolicy -b`` sat five hours
        # in disk wait holding its read snapshot; the WAL could not reset,
        # grew to 14.9 GB and filled the disk.  The sweep script now carries
        # its own deadline (PM_DASHBOARD_RENDER_TIMEOUT_SEC, default 1500 s,
        # tests/test_dashboard_render_timeout.py); the plist still has none.
        "com.weather.dashboard_render.plist",
        # 2026-09-16 (user ruling: 前端改为热更新): redraws the page with the
        # template on disk around the last render's payload whenever the
        # template changes.  A file watch, not a clock, and a reader like the
        # render -- no resident has a turn to host it in.
        "com.weather.dashboard_reinflate.plist",
        # 2026-09-20 (charter 164): replays the tiebreak rule over history and
        # scores the registered hypotheses, once a day at 08:10Z when the
        # previous day's truth has landed for the whole fleet.  A reader of
        # the store and a writer only of its own file, so it belongs behind
        # no writer lane; and a daily pass has no place in a resident whose
        # turn is eight minutes long.
        "com.weather.tiebreak_cell_shadow.plist",
        # 2026-09-21 (b0fe8f5e): the board's static file server on 8791 is a
        # launchd resident rather than a session's preview process, for the
        # same reason the live control server is (2026-09-13): a session
        # ending must not take the page down.  A reader of rendered files
        # and a writer of nothing.
        "com.weather.dashboard_static.plist",
        # 2026-09-23 (charter 207): our settled buckets against the ones the
        # market paid, every three hours.  Gamma over the network, the store
        # through a read-only connection held for seconds, two files under
        # data/ -- a reader with no place in a writer turn.
        "com.weather.settlement_truth_reconciliation.plist",
        # 2026-09-24 (user ruling 「单开研究节渲染」): the board's heavy
        # sections, one an hour, which the page render's 1,500 s cannot hold
        # (bias_half_life 1,249 s and the like) and now serves from their
        # pages.  A reader of the store and a writer of one section-cache
        # file; like the page render it belongs to no resident's turn.
        "com.weather.dashboard_research_render.plist",
    ]
    # The board's write path.  Not a production owner -- it places nothing
    # and holds no state -- but the LIVE switch is drawn disabled while it is
    # down, so it cannot live and die with a session's preview (2026-09-13).
    operator_control = [
        "com.weather.live_control.plist",
    ]
    launchd = sorted(path.name for path in (ROOT / "launchd").glob("*.plist"))
    assert launchd == sorted(
        installer_managed + superseded_by_hosted_sweeps + own_clock
        + operator_control
    )
    for name in operator_control:
        assert name in installer
    # Superseded is not merely "on disk but not default".  The installer must
    # not offer these at all: a lane the resident hosts inside the SSOT writer
    # turn, installed again on its own launchd clock, writes outside that turn.
    # That is what happened to the ledger sweep on 2026-09-11 -- it sat in
    # OPTIONAL_PLISTS, got installed, and starved the hosted capture lanes of
    # the real lock for as long as it ran.
    offered = installer.split("SUPPORTED_PLISTS=", 1)[0]
    for name in superseded_by_hosted_sweeps:
        assert name not in offered, f"{name} is hosted by the resident; the installer must not offer it"
    for name in installer_managed:
        assert name in installer
    for label in (
        "com.weather.weather_basket_yes",
        "com.weather.market_ws",
        "com.weather.clob_order_service",
    ):
        assert label in installer
        assert label in targetctl
    assert "com.weather.task_queue_worker.plist" not in installer.split(
        "DEFAULT_PLISTS=(", 1
    )[1].split(")", 1)[0]


def test_retired_probability_manifest_cannot_authorize_network_or_production() -> None:
    from production_probability_runtime_manifest import (
        PRODUCTION_RUNTIME_AUTHORIZED,
        RETIRED_NETWORK_ENTRYPOINTS,
        RUNTIME_FILES,
        build_current_runtime_manifest,
    )

    manifest = build_current_runtime_manifest()
    assert PRODUCTION_RUNTIME_AUTHORIZED is False
    assert manifest["authority_scope"] == "RETIRED_HISTORY_ONLY"
    assert manifest["production_runtime_authorized"] is False
    assert manifest["network_entrypoints"] == []
    assert RETIRED_NETWORK_ENTRYPOINTS == ("scripts/fetch_zuuu_metar_taf.py",)
    assert "scripts/fetch_zuuu_metar_taf.py" not in RUNTIME_FILES
