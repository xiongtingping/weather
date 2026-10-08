"""Pure cloud-only TAF decoder, research schema; no AWC decoded fcsts used.

Unrecognised time/cloud grammar fails closed. Coverage categories remain
intervals. A cloud-base comparison is not a measurement of optical depth.
"""
from __future__ import annotations
import calendar
import re
from datetime import datetime, timedelta, timezone

SCHEMA='raw_taf_cloud_scenarios_v1'
CLOUD=re.compile(r'^(FEW|SCT|BKN|OVC|VV)(\d{3}|///)(CB|TCU)?$')
WINDOW=re.compile(r'^(\d{2})(\d{2})/(\d{2})(\d{2})$')
FM=re.compile(r'^FM(\d{2})(\d{2})(\d{2})$')
CLEAR={'CAVOK','NSC','SKC','CLR','NCD'}
AMOUNT={'FEW':(1/8,2/8),'SCT':(3/8,4/8),'BKN':(5/8,7/8),'OVC':(1.,1.)}


class DecodeError(ValueError):pass


def utc(value):
    t=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if t.tzinfo is None:raise DecodeError('timezone_missing')
    return t.astimezone(timezone.utc)


def stamp(t):return t.isoformat().replace('+00:00','Z')


def day_time(dd,hh,mm,near):
    """Resolve calendar rollover from a complete issue/valid timestamp."""
    dd,hh,mm=int(dd),int(hh),int(mm)
    if not 1<=dd<=31 or not 0<=hh<=24 or not 0<=mm<60 or hh==24 and mm!=0:
        raise DecodeError('invalid_day_hour')
    candidates=[]
    for delta in (-1,0,1):
        month_index=near.year*12+near.month-1+delta
        year,month=divmod(month_index,12);month+=1
        if dd>calendar.monthrange(year,month)[1]:continue
        candidates.append(datetime(year,month,dd,min(hh,23),mm,tzinfo=timezone.utc)+(timedelta(hours=1) if hh==24 else timedelta()))
    if not candidates:raise DecodeError('calendar_day_invalid')
    return min(candidates,key=lambda t:abs((t-near).total_seconds()))


def window(token,near):
    m=WINDOW.fullmatch(token)
    if not m:raise DecodeError('window_token_invalid')
    d1,h1,d2,h2=m.groups();start=day_time(d1,h1,0,near);end=day_time(d2,h2,0,start)
    if end<=start or end-start>timedelta(hours=48):raise DecodeError('window_order_invalid')
    return start,end


def layers_of(tokens):
    layers=[]
    for token in tokens:
        m=CLOUD.fullmatch(token)
        if m:
            cover,base,kind=m.groups();layers.append(dict(cover=cover,base=None if base=='///' else int(base)*100,type=kind))
        elif token in CLEAR:layers.append(dict(cover=token,base=None,type=None))
        elif re.search(r'(?:FEW|SCT|BKN|OVC|VV)',token):
            raise DecodeError('unrecognised_cloud_token')
    if any(x['cover'] in CLEAR for x in layers) and len(layers)!=1:raise DecodeError('conflicting_clear_and_cloud')
    return layers or None


def decode(raw, *,station,issue_time):
    if len(raw.encode())>16384:raise DecodeError('raw_byte_limit')
    tokens=raw.replace('=','').split()
    if not tokens or tokens.pop(0)!='TAF':raise DecodeError('taf_header_missing')
    modifier=None
    if tokens and tokens[0] in ('AMD','COR'):modifier=tokens.pop(0)
    if not tokens or tokens.pop(0)!=station:raise DecodeError('station_mismatch')
    if not tokens:raise DecodeError('issue_missing')
    m=re.fullmatch(r'(\d{2})(\d{2})(\d{2})Z',tokens.pop(0))
    issue=utc(issue_time)
    if not m or day_time(*m.groups(),issue)!=issue.replace(microsecond=0):raise DecodeError('issue_mismatch')
    if not tokens:raise DecodeError('validity_missing')
    start,end=window(tokens.pop(0),issue)
    if any(t in ('NIL','CNL') for t in tokens):raise DecodeError('nil_or_cancelled')
    if 'RMK' in tokens:tokens=tokens[:tokens.index('RMK')]
    # TAF weather/time grammar not covered here must not be treated as cloud
    # omission. TX/TN temperature valid times are not cloud change headers.
    if any(t in ('INTER','AT','TL','TIL') for t in tokens):raise DecodeError('unsupported_change_grammar')
    groups=[];current=dict(kind='BASE',start=stamp(start),end=stamp(end),tokens=[])
    i=0
    while i<len(tokens):
        t=tokens[i];f=FM.fullmatch(t)
        if f or t in ('BECMG','TEMPO') or t.startswith('PROB'):
            groups.append(current)
            if len(groups)>64:raise DecodeError('group_limit')
            if f:
                when=day_time(*f.groups(),start)
                if not start<=when<end:raise DecodeError('fm_outside_validity')
                current=dict(kind='FM',start=stamp(when),end=stamp(end),tokens=[]);i+=1;continue
            kind=t;chance=None;i+=1
            if t.startswith('PROB'):
                if t not in ('PROB30','PROB40'):raise DecodeError('prob_token_invalid')
                kind='PROB';chance=int(t[4:])
                if i<len(tokens) and tokens[i]=='TEMPO':kind='PROB_TEMPO';i+=1
            if i>=len(tokens):raise DecodeError('change_window_missing')
            lo,hi=window(tokens[i],start);i+=1
            if hi<=start or lo>=end:raise DecodeError('change_outside_validity')
            # Explicitly clip a described overlay to the whole TAF validity;
            # never extend whole-bulletin availability to the overlay's end.
            current=dict(kind=kind,start=stamp(max(lo,start)),end=stamp(min(hi,end)),tokens=[])
            if chance is not None:current['probability']=chance
            continue
        if t.startswith('FM'):raise DecodeError('fm_token_invalid')
        current['tokens'].append(t);i+=1
    groups.append(current)
    for g in groups:g['layers']=layers_of(g.pop('tokens'))
    if groups[0]['layers'] is None:raise DecodeError('initial_cloud_missing')
    prevailing=[g for g in groups if g['kind'] in ('BASE','FM','BECMG')]
    if any(utc(b['start'])<utc(a['start']) for a,b in zip(prevailing,prevailing[1:])):
        raise DecodeError('prevailing_order_invalid')
    for j,g in enumerate(prevailing):
        if g['kind']=='FM' and g['layers'] is None:raise DecodeError('fm_cloud_missing')
        if g['kind']=='BECMG' and j+1<len(prevailing) and utc(prevailing[j+1]['start'])<utc(g['end']):
            raise DecodeError('overlapping_prevailing_change')
    return dict(schema=SCHEMA,station=station,issue=stamp(issue),modifier=modifier,valid_from=stamp(start),valid_to=stamp(end),groups=groups)


def scenarios(decoded,at):
    if isinstance(at,str):at=utc(at)
    if not utc(decoded['valid_from'])<=at<utc(decoded['valid_to']):return None
    prev=[g for g in decoded['groups'] if g['kind'] in ('BASE','FM','BECMG')]
    current=prev[0]['layers'];poss=[current]
    for g in prev[1:]:
        if utc(g['start'])>at:break
        new=g['layers'] if g['layers'] is not None else current
        if g['kind']=='BECMG' and at<utc(g['end']):poss=[current,new]
        else:poss=[new]
        current=new
    for g in decoded['groups']:
        if g['kind'] in ('TEMPO','PROB','PROB_TEMPO') and utc(g['start'])<=at<utc(g['end']) and g['layers'] is not None:poss.append(g['layers'])
    # Exact duplicate cloud states have no extra likelihood weight.
    unique=[]
    for layers in poss:
        if layers not in unique:unique.append(layers)
    return unique


def layer_interval(layers,height):
    if not layers:return None
    vals=[]
    for x in layers:
        k=x['cover'];base=x['base']
        if k in ('VV','OVX'):return None
        if k in ('CAVOK','NSC','NCD'):
            if height>5000:return None
            vals.append((0.,0.))
        elif k in ('CLR','SKC'):vals.append((0.,0.))
        elif k in AMOUNT:
            if base is None:return None
            vals.append(AMOUNT[k] if base<height else (0.,0.))
        else:return None
    return max(x[0] for x in vals),max(x[1] for x in vals)


def interval(decoded,at,height=6500):
    poss=scenarios(decoded,at)
    if poss is None:return None
    vals=[layer_interval(layers,height) for layers in poss]
    if any(x is None for x in vals):return None
    return min(x[0] for x in vals),max(x[1] for x in vals)


def cloud_increment(obs,forecast):
    # Only non-overlapping coverage intervals count; a forecasted possibility
    # supplies no surprising-cloud evidence even if its probability was low.
    if obs is None or forecast is None:return None
    if obs[0]>forecast[1]:return obs[0]-forecast[1]
    if obs[1]<forecast[0]:return obs[1]-forecast[0]
    return 0.


def base_increment(obs_layers,poss):
    """km cloud-base lowering, only uniquely matched cover/type in all scenarios.

    No physical probability direction is imposed on this diagnostic feature.
    Clear skies, unknown heights and ambiguous multi-layer matches return None.
    """
    observed=[x for x in obs_layers if x['cover'] in AMOUNT and x['base'] is not None]
    if len(observed)!=1 or poss is None:return None
    o=observed[0];diff=[]
    for layers in poss:
        same=[x for x in layers if x['cover']==o['cover'] and x.get('type')==o.get('type') and x['base'] is not None]
        if len(same)!=1:return None
        diff.append((same[0]['base']-o['base'])*0.0003048)
    if min(diff)!=max(diff):return None
    return diff[0]
