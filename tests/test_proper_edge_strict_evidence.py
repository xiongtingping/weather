"""Adversarial boundaries for current-model offline evidence; no live services."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import sqlite3

import pytest

from scripts.strict_replay_clock import (EvidenceClockError, case_truth_known, day_end,
    exact_member_arrival, instant, select_truth_asof, training_fingerprint)
from scripts import replay_p0_stable_pool as replay


def truth(revision=0, value=22, stamp="2026-09-10T06:19:43Z"):
    return dict(truth_id=f"t{revision}", station_id="MMMX", local_date="2026-09-09",
                settlement_timezone="America/Mexico_City", settlement_definition="airport_day_high",
                revision_no=revision, official_tmax_c=value, source_lane="AWC",
                qc_status="ACCEPTED", authority_scope="FORMAL_NATIVE_ELIGIBLE",
                timestamps_complete=1, canonical_committed_at_utc=stamp)


def select(rows, at):
    return select_truth_asof(rows, station="MMMX", event_date="2026-09-09",
        timezone_name="America/Mexico_City", settlement_definition="airport_day_high", as_of=instant(at))


def case(revision=1, stamp="2026-09-24T14:21:59Z"):
    return dict(event_date="2026-09-09", timezone_name="America/Mexico_City", truth_id=f"t{revision}",
                truth_revision_no=revision, truth_canonical_committed_at_utc=stamp,
                member_set_sha256="set", values=[21,25], actual_tmax_c=24,
                member_run_content_shas=["a", "b"],archive_lead_bucket="d1")


def receipts():
    return {sha:dict(content_sha256=sha,station_id="MMMX",ensemble_system_id="ECMWF_IFS_ENS_OPEN_PF50",
            target_local_date="2026-09-09",archive_lead_bucket="d1",run_cycle="00Z",member_id=sha,
            qc_status="ACCEPTED",transport_provider_id="ECMWF_OPEN_DATA_S3",native_run_init_utc="2026-09-08T00:00:00Z",
            archive_query_at_utc=stamp) for sha,stamp in [('a','2026-09-08T06:00:00Z'),('b','2026-09-08T08:00:00Z')]}


def arrival(c=None, rs=None):
    return exact_member_arrival(c or case(),rs if rs is not None else receipts(),station="MMMX",system="ECMWF_IFS_ENS_OPEN_PF50",run_cycle="00Z")


def test_late_revision_is_selected_only_when_known_in_both_directions():
    old=truth();new=truth(1,24,"2026-09-24T14:21:59Z");new['source_lane']='SETTLEMENT_SOURCE'
    for at,expected in [('2026-09-15T12:00:00Z',22),('2026-09-24T14:21:59Z',24),('2026-09-24T14:21:58.999Z',22),('2026-09-25T00:00:00Z',24),('2026-09-15T12:00:00Z',22)]:
        assert select([new,old],at)['official_tmax_c']==expected


def test_future_revision_cannot_use_first_publication_clock():
    c=case();scope=('s','season','lead','cycle')
    f=replay.Forecasts({scope:[c]},{c['event_date']:instant('2026-09-10T06:19:43Z')},lambda *_:instant('2026-09-08T08:00:00Z'))
    assert f.train(scope,'2026-09-16',instant('2026-09-15T12:00:00Z'))==[]
    assert f.train(scope,'2026-09-26',instant('2026-09-24T14:21:59Z'))==[c]
    assert f.train(scope,'2026-09-16',instant('2026-09-15T12:00:00Z'))==[]


def test_latest_snapshot_gate_does_not_invent_earlier_truth():
    assert not case_truth_known(case(),instant('2026-09-15T12:00:00Z'))
    assert select([truth(),truth(1,24,'2026-09-24T14:21:59Z')],'2026-09-15T12:00:00Z')['truth_id']=='t0'


def test_member_set_waits_for_last_exact_receipt():
    assert arrival()==instant('2026-09-08T08:00:00Z')
    rs=receipts();rs['b']['archive_query_at_utc']='2026-09-22T00:00:00Z'
    assert arrival(rs=rs)==instant('2026-09-22T00:00:00Z')
    rs.pop('b')
    with pytest.raises(EvidenceClockError,match='exact_member_receipt_missing'):arrival(rs=rs)


@pytest.mark.parametrize('field,value,reason',[
    ('station_id','KATL','scope'),('archive_lead_bucket','h24_48','scope'),
    ('run_cycle','12Z','scope'),('transport_provider_id','OPEN_METEO','native'),
    ('qc_status','QUARANTINED','native'),('member_id','a','duplicate_member'),
    ('archive_query_at_utc','2026-09-07T00:00:00Z','before_run')])
def test_member_receipt_fails_closed(field,value,reason):
    rs=receipts();rs['b'][field]=value
    with pytest.raises(EvidenceClockError,match=reason):arrival(rs=rs)


def test_duplicate_member_hashes_and_unavailable_probe_data_rejected():
    c=case();c['member_run_content_shas']=['a','a']
    with pytest.raises(EvidenceClockError,match='identity'):arrival(c=c)
    c=case();del c['truth_canonical_committed_at_utc']
    with pytest.raises(EvidenceClockError,match='receipt_missing'):case_truth_known(c,instant('2026-10-01T00:00:00Z'))


@pytest.mark.parametrize('stamp',['2026-09-15T12:00:00',None,'not-time'])
def test_timezone_is_never_guessed(stamp):
    with pytest.raises(EvidenceClockError):instant(stamp)


def test_closed_day_dst_and_utc_boundaries():
    assert day_end('2026-11-01','America/New_York')==instant('2026-11-02T05:00:00Z')
    assert day_end('2026-03-08','America/New_York')==instant('2026-03-09T04:00:00Z')
    preclose=truth(stamp='2026-09-09T01:00:00Z')
    assert select([preclose],'2026-09-10T05:59:59Z') is None
    assert select([preclose],'2026-09-10T06:00:00Z') is not None


def test_revision_identity_and_contract_fail_closed():
    with pytest.raises(EvidenceClockError,match='ambiguous'):select([truth(),truth()],'2026-09-15T12:00:00Z')
    for key,value in [('station_id','OTHER'),('settlement_timezone','UTC'),('settlement_definition','other')]:
        r=truth();r[key]=value
        with pytest.raises(EvidenceClockError):select([r],'2026-09-15T12:00:00Z')


def test_latest_accepted_and_formal_lane_win_without_cross_source_max():
    rows=[truth(),truth(1,21,'2026-09-12T00:00:00Z')]
    rows[1]['source_lane']='SETTLEMENT_SOURCE'
    assert select(rows,'2026-09-15T12:00:00Z')['official_tmax_c']==21
    rows[1]['qc_status']='QUARANTINED'
    assert select(rows,'2026-09-15T12:00:00Z')['official_tmax_c']==22
    rows[0]['source_lane']='AMSC'
    assert select(rows,'2026-09-15T12:00:00Z') is None


def test_cache_key_changes_with_revision_and_member_set():
    c=case();a=training_fingerprint([c]);c['truth_revision_no']=2
    assert a!=training_fingerprint([c]);b=training_fingerprint([c]);c['member_set_sha256']='other'
    assert b!=training_fingerprint([c])


def test_production_case_loader_keeps_the_exact_canonical_truth_receipt():
    from tests.test_native_ensemble_oos_p0_pipeline import _insert_fixture
    from bayes_basket_target.native_ensemble_oos import _load_cases
    conn=sqlite3.connect(':memory:')
    try:
        _insert_fixture(conn)
        cases=_load_cases(conn,station_id='TEST',as_of_utc='2026-07-01T00:00:00Z')
        flat=[c for rows in cases.values() for c in rows]
        assert flat
        for c in flat:
            row=conn.execute('SELECT revision_no,canonical_committed_at_utc,official_tmax_c FROM calibration_station_daily_high_truth WHERE truth_id=?',(c['truth_id'],)).fetchone()
            assert row==(c['truth_revision_no'],c['truth_canonical_committed_at_utc'],c['actual_tmax_c'])
    finally:
        conn.close()
