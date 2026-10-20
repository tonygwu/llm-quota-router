"""Integrate binding pool rates over raw stretches and historical belief intervals."""
from __future__ import annotations

from itertools import product
from types import SimpleNamespace

from . import calibrations
from .waste_report import _effective

KEYS=('weekly_pool','weekly_to_session','capacity','tier','tier_scale')


def _encoding(ledger, account, key, at_s, scope='account'):
    entries=[e for e in ledger if e['kind']=='encoding' and e['key']==key and
             e['scope']==scope and (scope!='account' or e['account']==account) and e['annotates'] is None]
    uncertain=[e for e in entries if e['effective_from'] is not None and e['effective_to'] is not None
               and calibrations.parse_time(e['effective_from'])<=at_s<calibrations.parse_time(e['effective_to'])]
    if uncertain:
        return None, uncertain
    applicable=[e for e in entries if e['effective_to'] is None and e['effective_from'] is None or
                e['effective_to'] is not None and calibrations.parse_time(e['effective_to'])<=at_s]
    entry=max(enumerate(applicable),key=lambda p:(float('-inf') if p[1]['effective_to'] is None else
              calibrations.parse_time(p[1]['effective_to']),p[0]))[1] if applicable else None
    return entry, []


def _conversion(row, ledger, at_s):
    raw=row['start_evidence']
    k,k_uncertain=_encoding(ledger,row['account'],'weekly_to_session',at_s)
    if k is None and not k_uncertain:
        k,k_uncertain=_encoding(ledger,row['account'],'weekly_to_session',at_s,scope='default')
    tier,tier_uncertain=_encoding(ledger,row['account'],'tier',at_s)
    tier_value=None if tier_uncertain else tier['value'] if tier else raw['tier']
    scale,scale_uncertain=(None,[]) if tier_value is None else _encoding(
        ledger,row['account'],'tier_scale',at_s,scope='tier:'+tier_value)
    capacity,capacity_uncertain=_encoding(ledger,row['account'],'capacity',at_s)
    size=None if capacity_uncertain else capacity['value'] if capacity else raw['capacity']
    # Unknown explicit capacity must not quietly become a tier fallback.
    mode='capacity' if capacity_uncertain or size is not None else 'tier'
    basis=dict(k=k['value'] if k else None, tier=tier_value,
               window_size=size if mode=='capacity' else scale['value'] if scale else None,
               window_size_source=mode, fable_fraction=None)
    beliefs=[e for key in KEYS if (e:=calibrations.resolve_belief(ledger,row['account'],key,at_s)) is not None]
    order={e['id']:i for i,e in enumerate(ledger)}
    beliefs.sort(key=lambda e:(calibrations.parse_time(e['recorded_at']),order[e['id']]))
    proxy=SimpleNamespace(account=row['account'],scoped=False,reset_at_s=at_s)
    values,active,missing,extra=_effective(proxy,basis,beliefs,ledger)
    suppliers={}
    for entry in active:
        if entry['key']=='weekly_pool':
            suppliers.update(k=entry,window_size=entry)
        elif entry['key']=='weekly_to_session':
            suppliers['k']=entry
        elif entry['key'] in {'capacity','tier_scale'}:
            suppliers['window_size']=entry
        elif entry['key']=='tier':
            suppliers['tier']=entry
    size_supplier=suppliers.get('window_size')
    uses_scale=size_supplier is None and mode=='tier'
    uncertainty=[]
    if k_uncertain and 'k' not in suppliers:
        uncertainty.append('uncertain historical weekly_to_session encoding')
    if capacity_uncertain and size_supplier is None:
        uncertainty.append('uncertain historical capacity encoding')
    if uses_scale:
        if tier_uncertain and 'tier' not in suppliers:
            uncertainty.append('uncertain historical tier encoding')
        if scale_uncertain and not extra:
            uncertainty.append('uncertain historical tier_scale encoding')
    if uncertainty:
        missing='; '.join(uncertainty)
    provenance={
        'k': {'source':'belief' if 'k' in suppliers else 'uncertain' if k_uncertain else k['source'] if k else 'missing',
              'ledger_id':suppliers['k']['id'] if 'k' in suppliers else k['id'] if k else None},
        'window_size': {'source':'tier_fallback' if uses_scale else 'belief' if size_supplier else
                        'uncertain' if capacity_uncertain else capacity['source'] if capacity else
                        'observed_capacity' if size is not None else 'tier_fallback',
                        'ledger_id':scale['id'] if uses_scale and scale else size_supplier['id'] if size_supplier else
                        capacity['id'] if capacity else scale['id'] if scale else None}}
    for component,detail in provenance.items():
        supplier=suppliers.get(component)
        detail['measurement_ids']=supplier['measurement_ids'] if supplier and not (component=='window_size' and uses_scale) else []
        detail['unmeasured']=not bool(detail['measurement_ids'])
    ids=[e['id'] for e in active]+extra+[d['ledger_id'] for d in provenance.values() if d['ledger_id'] is not None]
    if uses_scale and tier is not None:
        ids.append(tier['id'])
    ids.extend(e['id'] for group in (k_uncertain,capacity_uncertain,tier_uncertain,scale_uncertain) for e in group)
    return proxy,basis,beliefs,values,active,missing,list(dict.fromkeys(ids)),provenance


def _rate(row,values):
    size,k=values['window_size'],values['k']
    rates=[]
    windows={w['key']:w for w in row['start_evidence']['windows'] if not w['applies_to']}
    for key in row['window_keys']:
        if key not in {'5h','7d'}:
            return None,'no conversion for account-wide window '+key
        amount=size if key=='5h' else None if size is None or k is None else size*k
        if amount is None:
            return None,'historical conversion lacks k or window size'
        rates.append(amount/windows[key]['length_s'])
    return min(rates),None


def summarize(rows,*,ledger=(),beliefs_as_of=None,ping_events=()):
    selected=list(ledger)
    if beliefs_as_of is not None:
        cutoff=calibrations.parse_time(beliefs_as_of)
        selected=[e for e in selected if calibrations.parse_time(e['recorded_at'])<cutoff]
    first={}
    for event in ping_events:
        if event['event']=='result':
            at=event['started_at_s']
            calibrations._number(at,'ping start')
            pool=event['pool']
            first[pool]=min(first[pool],at) if pool in first else at
    totals=dict(forgone_pse=0.0,before_ping_pse=0.0,after_ping_pse=0.0,
                stretches=0,accruing=0,bounded=0,unresolved=0,unrecomputable=0)
    accounts={}
    reports=[]
    for row in rows:
        reports_for_row=[]
        boundaries={row['start_s'],row['end_s']}
        for e in selected:
            for key in ('valid_from','valid_to') if e['kind']=='belief' else ('effective_from','effective_to') if e['kind']=='encoding' else ():
                if e[key] is not None:
                    at=calibrations.parse_time(e[key])
                    if row['start_s']<at<row['end_s']:
                        boundaries.add(at)
        ping=first.get(row['pool'])
        if ping is not None and row['start_s']<ping<row['end_s']:
            boundaries.add(ping)
        for start,end in zip(sorted(boundaries),sorted(boundaries)[1:]):
            proxy,basis,beliefs,values,active,reason,ids,provenance=_conversion(row,selected,start)
            unresolved=[e for e in active if e['status']=='unresolved']
            rate,arithmetic=_rate(row,values)
            candidates=[]
            if unresolved:
                for choices in product(*(e['candidates'] for e in unresolved)):
                    v,_,why,_=_effective(proxy,basis,beliefs,selected,
                        {e['id']:c for e,c in zip(unresolved,choices)})
                    candidate_rate,why_rate=_rate(row,v)
                    candidates.append(dict(label='; '.join(c['label'] for c in choices),values=v,
                        forgone_pse=(end-start)*candidate_rate if candidate_rate is not None and why is None else None,
                        reason=why or why_rate))
                value,status,reason=None,'unresolved','operator belief remains unresolved'
            else:
                reason=reason or arithmetic
                value=(end-start)*rate if rate is not None and reason is None else None
                status='belief' if value is not None and active else 'historical_encoding' if value is not None else 'unrecomputable'
            reports_for_row.append(dict(start_s=start,end_s=end,current=dict(status=status,forgone_pse=value,
                values=values,ledger_ids=ids,reason=reason,candidates=candidates),
                binding_rate_pse_per_s=rate,assumption_provenance=provenance,
                unmeasured_components=[k for k,v in provenance.items() if v['unmeasured']] ))
        value=sum(s['current']['forgone_pse'] or 0 for s in reports_for_row)
        statuses={s['current']['status'] for s in reports_for_row}
        current=dict(status='unresolved' if 'unresolved' in statuses else 'unrecomputable' if 'unrecomputable' in statuses else 'derived',
            forgone_pse=None if statuses & {'unresolved','unrecomputable'} else value,
            ledger_ids=list(dict.fromkeys(i for s in reports_for_row for i in s['current']['ledger_ids'])),candidates=[])
        # Full-stretch candidates are meaningful only when one unresolved interval
        # covers it. Mixed intervals remain explicit in segments, with resolved
        # segments included and unresolved segments excluded from totals.
        if len(reports_for_row)==1:
            current['candidates']=reports_for_row[0]['current']['candidates']
        reports.append(dict(row,current=current,segments=reports_for_row))
        bucket=accounts.setdefault(row['account'],{}).setdefault(row['pool'],{k:0 for k in totals})
        for target in (totals,bucket):
            target['stretches']+=1
            target['accruing']+=row['status']=='accruing'
            target['bounded']+=row['status']=='bounded'
            target['unresolved']+='unresolved' in statuses
            target['unrecomputable']+='unrecomputable' in statuses
            target['forgone_pse']+=value
            for segment in reports_for_row:
                amount=segment['current']['forgone_pse']
                if amount is not None:
                    target['after_ping_pse' if ping is not None and segment['start_s']>=ping else 'before_ping_pse']+=amount
    return dict(rows=reports,totals=totals,accounts=accounts,first_live_ping_at_s=first,
                retention_note='Stretches before retained history cannot be recovered; unknown gaps are lower bounds. Open stretches accrue only through the last waiting observation.')
