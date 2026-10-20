"""Append-only raw waiting stretches; PSE is derived only when reporting."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from . import calibrations
from .state import state_path
from .window_state import GAP_S, classify


def forgone_path(env=None):
    return state_path(env).with_name('forgone.jsonl')


def _evidence(read):
    return {k: read[k] for k in ('at_s','provider','windows','tier','capacity','window_states')}


def detect(records):
    rows = []
    for account, reads in classify(records).items():
        if reads[0]['provider'] not in {'claude', 'codex', 'antigravity'}:
            continue
        active = None
        for read in reads:
            at = read['at_s']
            if read['state'] == 'stale':
                if active is not None:
                    active['stale_reads'].append(_evidence(read))
                continue
            if read['state'] == 'unknown':
                if active is not None:
                    active['gaps'].append(dict(start_s=active['end_s'],end_s=at,
                        counted=False,reason='incomplete_observation',evidence=_evidence(read)))
                # A partial observation cannot establish an end. If waiting
                # resumes, its original start still belongs to this stretch.
                continue
            if read['state'] == 'waiting':
                windows = [w for w in read['windows'] if not w['applies_to']]
                if active is None:
                    start = max(read['window_states'][w['key']]['waiting_since_s'] for w in windows)
                    active = dict(version=1, account=account, pool=account,
                        window_keys=sorted(w['key'] for w in windows), start_s=start, end_s=at,
                        start_evidence=_evidence(read), last_waiting_evidence=_evidence(read),
                        end_evidence=None, status='accruing', gaps=[], stale_reads=[])
                else:
                    for gap in active['gaps']:
                        if gap['reason']=='incomplete_observation' and gap['start_s']==active['end_s']:
                            gap.update(end_s=at,counted=True,reason='waiting_brackets_incomplete_observation')
                    if at-active['end_s'] > GAP_S:
                        active['gaps'].append(dict(start_s=active['end_s'],end_s=at,counted=True,
                            reason='waiting_on_both_sides'))
                    active['end_s'] = at
                    active['last_waiting_evidence'] = _evidence(read)
                continue
            if active is None:
                continue
            active['end_evidence'] = _evidence(read)
            openings = [w['resets_at_s']-w['length_s'] for w in read['windows']
                        if not w['applies_to'] and 'resets_at_s' in w and
                        read['window_states'][w['key']]['state']=='running' and
                        w['resets_at_s']-w['length_s'] >= active['start_s']]
            # A missing or ambiguous read cannot attest to a first-use instant.
            first_use = min(openings) if openings else None
            if (first_use is not None and first_use >= active['end_s'] and
                    at-active['end_s'] <= GAP_S):
                active.update(end_s=first_use,status='finished')
            else:
                active['status'] = 'bounded'
                active['gaps'].append(dict(start_s=active['end_s'],end_s=at,counted=False,
                    possible_end_s=first_use,reason='unobserved_or_ambiguous_end'))
            rows.append(active)
            active = None
        if active is not None:
            rows.append(active)
    for row in rows:
        identity = json.dumps([row['account'],row['pool'],row['start_s'],row['end_s']],separators=(',',':'))
        row['id'] = hashlib.sha256(identity.encode()).hexdigest()
    return sorted(rows,key=lambda r:(r['start_s'],r['account']))


def _validate(row):
    required={'version','id','account','pool','window_keys','start_s','end_s','start_evidence',
              'last_waiting_evidence','end_evidence','status','gaps','stale_reads'}
    if (not isinstance(row,dict) or set(row)!=required or
            type(row['version']) is not int or row['version']!=1):
        raise ValueError('invalid forgone record schema')
    if row['status'] not in {'finished','bounded','accruing'}:
        raise ValueError('invalid forgone status')
    for key in ('start_s','end_s'):
        calibrations._number(row[key],key)
    if row['end_s']<row['start_s']:
        raise ValueError('forgone end precedes start')
    for key in ('id','account','pool'):
        calibrations._text(row[key],key)
    calibrations._strings(row['window_keys'],'window_keys')
    if not row['window_keys']:
        raise ValueError('forgone record has no windows')
    for key in ('start_evidence','last_waiting_evidence','end_evidence'):
        evidence=row[key]
        if key=='end_evidence' and evidence is None and row['status']=='accruing':
            continue
        if not isinstance(evidence,dict) or set(evidence)!={'at_s','provider','windows','tier','capacity','window_states'}:
            raise ValueError('invalid forgone evidence schema')
        calibrations._number(evidence['at_s'],'evidence time')
        if evidence['capacity'] is not None:
            calibrations._number(evidence['capacity'],'evidence capacity')
        calibrations._text(evidence['provider'],'evidence provider')
        calibrations._text(evidence['tier'],'evidence tier')
        if not isinstance(evidence['windows'],list) or not evidence['windows']:
            raise ValueError('invalid forgone evidence windows')
        for window in evidence['windows']:
            required_window={'key','length_s','observed_at_s','applies_to'}
            fields=required_window|({'not_running'} if window.get('not_running') else {'used_fraction','resets_at_s'})
            if set(window)!=fields:
                raise ValueError('invalid forgone window fields')
            calibrations._text(window['key'],'window key')
            calibrations._strings(window['applies_to'],'applies_to')
            for name in fields-{'key','applies_to','not_running'}:
                calibrations._number(window[name],name)
            if window['length_s']<=0:
                raise ValueError('invalid forgone window length')
        for state in evidence['window_states'].values():
            if set(state)!={'state','reason','waiting_since_s'} or state['state'] not in {'waiting','running','unknown','stale'}:
                raise ValueError('invalid forgone window state')
            calibrations._text(state['reason'],'waiting reason')
            if state['waiting_since_s'] is not None:
                calibrations._number(state['waiting_since_s'],'waiting start')
    if row['end_s']>row['last_waiting_evidence']['at_s'] and row['end_evidence'] is None:
        raise ValueError('end lacks first-use evidence')
    if not isinstance(row['gaps'],list) or not isinstance(row['stale_reads'],list):
        raise ValueError('invalid forgone gap evidence')
    for gap in row['gaps']:
        if not {'start_s','end_s','counted','reason'}<=gap.keys() or type(gap['counted']) is not bool:
            raise ValueError('invalid forgone gap')
        for key in ('start_s','end_s'):
            calibrations._number(gap[key],key)
        calibrations._text(gap['reason'],'gap reason')
    json.dumps(row,allow_nan=False)


def _loads(text):
    if text and not text.endswith('\n'):
        raise ValueError('partial final forgone record')
    rows=[]
    identities=set()
    for line in text.splitlines():
        row=json.loads(line)
        _validate(row)
        if row['status']=='accruing':
            raise ValueError('durable forgone record must be closed')
        if row['id'] in identities:
            raise ValueError('duplicate forgone record identity')
        identities.add(row['id'])
        rows.append(row)
    return rows


def read_records(path=None,*,env=None):
    target=Path(path) if path is not None else forgone_path(env)
    try:
        with calibrations._locked(target,write=False) as handle:
            return _loads(handle.read().decode())
    except FileNotFoundError:
        return []


def _later_waiting_only(durable, proposed, *, states=False):
    """True when proposed differs from durable only by later window waiting starts.

    Retention deletes a window's earliest reads, so its first retained read
    gives a later waiting start while the stretch identity is unchanged. That
    is the only accepted difference; any other one is a real conflict.
    """
    if states:
        if not isinstance(durable,dict) or not isinstance(proposed,dict) or durable.keys()!=proposed.keys():
            return False
        for key,old in durable.items():
            new=proposed[key]
            if not isinstance(old,dict) or not isinstance(new,dict) or old.keys()!=new.keys():
                return False
            if any(old[k]!=new[k] for k in old if k!='waiting_since_s'):
                return False
            since,later=old.get('waiting_since_s'),new.get('waiting_since_s')
            if since!=later and (since is None or later is None or later<since):
                return False
        return True
    if isinstance(durable,dict) and isinstance(proposed,dict):
        return durable.keys()==proposed.keys() and all(
            _later_waiting_only(durable[k],proposed[k],states=k=='window_states') for k in durable)
    if isinstance(durable,list) and isinstance(proposed,list):
        return len(durable)==len(proposed) and all(
            _later_waiting_only(a,b) for a,b in zip(durable,proposed))
    return durable==proposed


def append_records(rows, path=None,*,env=None,counts=None):
    """Lock validation and append together. Only closed stretches are durable.

    A re-detected durable stretch whose only difference is a later window
    waiting start keeps the durable row; ``counts['retention_shifted']``
    reports how many, when a dict is passed.
    """
    target=Path(path) if path is not None else forgone_path(env)
    proposed=[deepcopy(row) for row in rows if row['status']!='accruing']
    for row in proposed:
        _validate(row)
    target.parent.mkdir(parents=True,exist_ok=True)
    with calibrations._locked(target,write=True) as handle:
        # _locked locks this file, not a separate sentinel.
        existing=_loads(handle.read().decode())
        known={r['id']:r for r in existing}
        appended=0
        shifted=0
        for row in proposed:
            if row['id'] in known:
                if known[row['id']]!=row:
                    if not _later_waiting_only(known[row['id']],row):
                        raise ValueError('conflicting forgone record identity')
                    shifted+=1
                continue
            # A prefix reconstructed after retention starts later must not be
            # appended over an already durable stretch from the complete history.
            if any(r['account']==row['account'] and r['start_s']<=row['start_s'] and
                   row['end_s']<=r['end_s'] for r in known.values()):
                continue
            if any(r['account']==row['account'] and
                   max(r['start_s'],row['start_s'])<min(r['end_s'],row['end_s']) for r in known.values()):
                raise ValueError('overlapping forgone stretches need explicit reconciliation')
            known[row['id']]=row
            appended+=1
        # Validate the entire transaction before its first write.
        old_ids={r['id'] for r in existing}
        for ident,row in known.items():
            if ident not in old_ids:
                handle.write((json.dumps(row,allow_nan=False)+'\n').encode())
        handle.flush()
        import os
        os.fsync(handle.fileno())
    if counts is not None:
        counts['retention_shifted']=shifted
    return appended


def merge(durable, detected):
    rows=list(durable)
    for row in detected:
        if any(r['account']==row['account'] and r['start_s']<=row['start_s'] and
               row['end_s']<=r['end_s'] for r in rows):
            continue
        if any(r['account']==row['account'] and
               max(r['start_s'],row['start_s'])<min(r['end_s'],row['end_s']) for r in rows):
            raise ValueError('overlapping durable and detected stretches need reconciliation')
        rows.append(row)
    return sorted(rows,key=lambda r:(r['start_s'],r['account']))


def summarize(rows, *, ledger=(), beliefs_as_of=None, ping_events=()):
    from .forgone_report import summarize as derive
    return derive(rows,ledger=ledger,beliefs_as_of=beliefs_as_of,ping_events=ping_events)
