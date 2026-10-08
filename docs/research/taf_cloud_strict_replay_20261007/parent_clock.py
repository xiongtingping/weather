"""Recover original committed parent clocks independently of replay success."""
from rebuild import fetch,utc,guard


def original_parent_clock(c,station,day,checkpoint):
    guard()
    rows=list(fetch(c,'select station_id,event_date,decision_at_utc,created_at,content_sha256 from basket_probability_envelope where checkpoint_id=? and stage=? and state=?',(checkpoint,'P0','COMMITTED')))
    if len(rows)!=1:raise ValueError('original_parent_clock_not_unique')
    r=rows[0]
    if (r['station_id'],r['event_date'])!=(station,day) or utc(r['decision_at_utc'])>utc(r['created_at']):
        raise ValueError('original_parent_clock_scope_or_order')
    return dict(station=station,event=day,anchor_checkpoint=checkpoint,
        outer_cutoff=r['created_at'],parent_decision_at=r['decision_at_utc'],
        original_envelope_content_sha=r['content_sha256'],
        clock_source='original_committed_P0_envelope_independent_of_old_replay_gate')


def validate_original_packet_clock(packet,clock):
    if (packet['station_id'],packet['event_date'])!=(clock['station'],clock['event']):
        raise ValueError('original_parent_packet_station_day_mismatch')
    if utc(packet['received_at_utc'])>utc(clock['outer_cutoff']):
        raise ValueError('original_parent_packet_arrived_after_commit')
