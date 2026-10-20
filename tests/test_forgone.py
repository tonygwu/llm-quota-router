"""Waiting capacity uses temporal evidence, never the ping eligibility heuristic."""
import importlib.util
import json
from pathlib import Path

import pytest

from quota_router import forgone, waste
from quota_router.window_state import classify

WEEK = 604800
HOUR = 3600


def reading(t, *, provider='antigravity', used=0, start=None, keys=('5h', '7d'), available=True, scoped=False):
    windows = []
    for key in keys:
        length = 18000 if key == '5h' else WEEK
        windows.append(dict(key=key, used_fraction=used, length_s=length,
                            resets_at_s=(t if start is None else start) + length,
                            observed_at_s=t, applies_to=['model'] if scoped else []))
    return {'t': t, 'event': 'status', 'accounts': [dict(id='a', provider=provider,
            tier='pro', capacity=2, source='live', available=available, confidence=1,
            windows=windows)]}


def belief(*, status='resolved', start=None, end=None, identifier='b', k=4):
    return dict(version=1, id=identifier, kind='belief', account='a', scope='account',
                key='weekly_pool', recorded_at='2026-10-19T00:00:00Z', reconstructed=False,
                evidence_at=None, valid_from=start, valid_to=end, status=status,
                value={'weekly_to_session': k, 'window_size': 2} if status=='resolved' else None,
                candidates=[] if status=='resolved' else [
                    {'label':'weekly unchanged','value':{'weekly_to_session':4,'window_size':2}},
                    {'label':'session unchanged','value':{'weekly_to_session':15,'window_size':2}}],
                reason='test evidence', measurement_ids=[], disputed_measurement_ids=[], decided_by='operator')


def report(records, **kwargs):
    return forgone.summarize(forgone.detect(records), ledger=[belief()], **kwargs)


def test_both_waiting_ends_at_first_use_and_weekly_rate():
    records=[reading(1000), reading(1400), reading(4600), reading(4800, used=.001, start=4700)]
    row, = forgone.detect(records)
    assert (row['start_s'],row['end_s'],row['status']) == (1000,4700,'finished')
    assert row['end_evidence']['windows'][0]['used_fraction']==.001
    assert report(records)['totals']['forgone_pse'] == pytest.approx(3700*8/WEEK)


@pytest.mark.parametrize('provider',['claude','antigravity'])
def test_only_session_waiting_with_running_weekly_is_zero(provider):
    records=[reading(t,provider=provider) for t in (1000,1400,2000)]
    for r in records:
        r['accounts'][0]['windows'][1].update(used_fraction=.1,resets_at_s=WEEK)
    assert forgone.detect(records)==[]


def test_fresh_claude_clock_weekly_never_waits():
    assert forgone.detect([reading(t,provider='claude') for t in (1000,1400,2000)])==[]


def test_codex_weekly_and_model_scoped_exclusion():
    records=[reading(t,provider='codex',keys=('7d',)) for t in (1000,1400,2000)]
    assert report(records)['totals']['forgone_pse']==pytest.approx(1000*8/WEEK)
    assert forgone.detect([reading(t,scoped=True) for t in (1000,1400,2000)])==[]


def test_rounded_zero_with_fixed_deadline_runs_even_at_first_poll():
    records=[reading(t,start=1000) for t in (1000,1400,2000)]
    assert forgone.detect(records)==[]
    assert all(s['state']=='running' for s in classify(records)['a'])


def test_old_replica_after_early_reset_does_not_end_stretch():
    records=[reading(500,used=.2,start=-1000),reading(1000),reading(1400),
             reading(1500,used=.2,start=-1000),reading(1800),reading(2000,used=.001,start=1900)]
    row,=forgone.detect(records)
    assert (row['start_s'],row['end_s'])==(1000,1900)
    assert row['stale_reads']


def test_waiting_on_both_sides_of_gap_counts_but_use_bounds_it():
    records=[reading(1000),reading(1400),reading(10000)]
    row,=forgone.detect(records)
    assert row['end_s']==10000 and row['status']=='accruing'
    assert row['gaps'][-1]['counted'] is True
    bounded,=forgone.detect(records[:2]+[reading(10000,used=.1,start=9900)])
    assert bounded['end_s']==1400 and bounded['status']=='bounded'
    assert bounded['gaps'][-1]['counted'] is False
    assert bounded['gaps'][-1]['possible_end_s']==9900


def test_failed_read_never_extends_open_stretch():
    records=[reading(1000),reading(1400),reading(10000,available=False)]
    assert forgone.detect(records)[0]['end_s']==1400


def test_scheduled_reset_includes_fixed_waiting_prefix():
    records=[reading(900,used=.1),reading(1100,start=1000),reading(1600),reading(2000)]
    for w in records[0]['accounts'][0]['windows']:
        w['resets_at_s']=1000
    row,=forgone.detect(records)
    assert row['start_s']==1000


def test_unresolved_stretch_lists_both_candidates_and_counts_zero():
    rows=forgone.detect([reading(1000),reading(1400),reading(2000)])
    result=forgone.summarize(rows,ledger=[belief(status='unresolved')])
    assert result['totals']['forgone_pse']==0
    row,=result['rows']
    assert row['current']['status']=='unresolved'
    assert [c['forgone_pse'] for c in row['current']['candidates']]==pytest.approx([1000*8/WEEK,1000*30/WEEK])
    assert row['current']['ledger_ids']==['b']


def test_finished_raw_survives_pruning_and_duplicate_append(tmp_path):
    rows=forgone.detect([reading(1000),reading(1400),reading(1600,used=.01,start=1500)])
    path=tmp_path/'forgone.jsonl'
    assert forgone.append_records(rows,path=path)==1
    original=path.read_bytes()
    assert forgone.append_records(rows,path=path)==0
    assert path.read_bytes()==original
    retained=forgone.read_records(path=path)
    assert retained==rows
    assert forgone.summarize(retained,ledger=[belief()])['totals']['forgone_pse']==pytest.approx(500*8/WEEK)
    raw=json.loads(original)
    assert 'forgone_pse' not in raw and 'basis' not in raw


def test_sparse_sliding_reads_do_not_fabricate_reset_or_forfeit():
    assert waste.detect_resets([reading(1000),reading(311000)]).records==()


def test_first_live_ping_splits_comparable_totals():
    rows=forgone.detect([reading(1000),reading(1400),reading(2000)])
    events=[dict(event='result',pool='a',at_s=1600,started_at_s=1500,outcome='succeeded')]
    result=forgone.summarize(rows,ledger=[belief()],ping_events=events)
    assert result['first_live_ping_at_s']=={'a':1500}
    assert result['totals']['before_ping_pse']==pytest.approx(500*8/WEEK)
    assert result['totals']['after_ping_pse']==pytest.approx(500*8/WEEK)


def test_mixed_window_clocks_cannot_establish_pool_waiting():
    records=[reading(t) for t in (1000,1400,2000)]
    for r in records:
        r['accounts'][0]['windows'][0]['observed_at_s']=900
    assert forgone.detect(records)==[]


def test_first_rounded_zero_is_resolved_by_use_of_the_same_new_window():
    records=[reading(1100,start=1000),reading(1500,used=.1,start=1000),
             reading(2000),reading(2400)]
    row,=forgone.detect(records)
    assert row['start_s']==2000


def test_early_reset_inside_waiting_adds_no_forfeit():
    records=[reading(1000),reading(1400),reading(5000),reading(5400)]
    assert waste.detect_resets(records).records==()
    row,=forgone.detect(records)
    assert (row['start_s'],row['end_s'])==(1000,5400)


def test_explicit_not_running_markers_are_sufficient():
    records=[reading(t) for t in (1000,1400)]
    for r in records:
        a=r['accounts'][0]
        a['not_running']=[{k:w[k] for k in ('key','length_s','observed_at_s')} for w in a['windows']]
        a['windows']=[]
    assert report(records)['totals']['forgone_pse']==pytest.approx(400*8/WEEK)


def test_belief_change_splits_stretch_and_as_of_reproduces_old_rate():
    rows=forgone.detect([reading(1000),reading(1400),reading(2000)])
    ledger=[belief(),dict(belief(identifier='b2',k=8,start='1970-01-01T00:25:00Z'),
                        recorded_at='2026-10-20T00:00:00Z')]
    new=forgone.summarize(rows,ledger=ledger)
    old=forgone.summarize(rows,ledger=ledger,beliefs_as_of='2026-10-20T00:00:00Z')
    assert new['totals']['forgone_pse']==pytest.approx(500*8/WEEK+500*16/WEEK)
    assert old['totals']['forgone_pse']==pytest.approx(1000*8/WEEK)


def test_reconstruction_tool_reproduces_report_before_a_later_belief(tmp_path,monkeypatch,capsys):
    history=tmp_path/'history'
    history.mkdir()
    (history/'history.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in
        [reading(1000),reading(1400),reading(2000),reading(2200,used=.01,start=2100)]))
    ledger=tmp_path/'calibrations.jsonl'
    ledger.write_text(''.join(json.dumps(r)+'\n' for r in
        [belief(),dict(belief(identifier='b2',k=8,start='1970-01-01T00:25:00Z'),recorded_at='2026-10-20T00:00:00Z')]))
    spec=importlib.util.spec_from_file_location('forgone_reconstruction',
        Path(__file__).resolve().parents[1]/'ops/reconstruct-forgone.py')
    tool=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    def run(*extra):
        monkeypatch.setattr('sys.argv',['reconstruct-forgone.py','--history-home',str(history),
                                        '--ledger-file',str(ledger),*extra])
        tool.main()
        return json.loads(capsys.readouterr().out)
    current=run()
    old=run('--beliefs-as-of','2026-10-20T00:00:00Z')
    assert current['report']['totals']['forgone_pse']==pytest.approx(500*8/WEEK+600*16/WEEK)
    assert old['report']['totals']['forgone_pse']==pytest.approx(1100*8/WEEK)
    assert (current['beliefs_as_of'],old['beliefs_as_of'])==(None,'2026-10-20T00:00:00Z')
    assert old['finished_records']==current['finished_records'] and len(old['finished_records'])==1
    assert any('#32' in gate for gate in old['operator_gates'])
    assert not any('together with the #26' in gate for gate in old['operator_gates'])


def test_missing_conversion_is_exposed_and_never_guessed():
    rows=forgone.detect([reading(1000),reading(1400)])
    result=forgone.summarize(rows)
    assert result['totals']['forgone_pse']==0
    assert result['totals']['unrecomputable']==1
    assert result['rows'][0]['segments'][0]['current']['reason']=='historical conversion lacks k or window size'


def test_corrupt_raw_record_stops_read_and_append(tmp_path):
    path=tmp_path/'forgone.jsonl'
    path.write_text('{"version":1}\n')
    with pytest.raises(ValueError,match='schema'):
        forgone.read_records(path)
    original=path.read_bytes()
    with pytest.raises(ValueError,match='schema'):
        forgone.append_records([],path)
    assert path.read_bytes()==original


def test_waste_cli_includes_forgone_even_without_reset_rows(tmp_path):
    import io
    from quota_router import cli, calibrations
    root=tmp_path/'state'/'quota-router'
    root.mkdir(parents=True)
    env={'HOME':str(tmp_path),'XDG_STATE_HOME':str(tmp_path/'state'),
         'XDG_CONFIG_HOME':str(tmp_path/'config'),'PATH':'/usr/bin:/bin'}
    records=[reading(1000),reading(1400),reading(1600,used=.01,start=1500)]
    (root/'history.jsonl').write_text(''.join(json.dumps(dict(r,v=1))+'\n' for r in records))
    calibrations.append_records([belief()],env=env)
    out,err=io.StringIO(),io.StringIO()
    assert cli.main(['waste','--json'],env=env,stdout=out,stderr=err,cwd=str(tmp_path))==0,err.getvalue()
    result=json.loads(out.getvalue())
    assert result['total_wasted_pse']==pytest.approx(500*8/WEEK)
    assert result['forgone_totals']['stretches']==1
    out,err=io.StringIO(),io.StringIO()
    assert cli.main(['waste','--backfill'],env=env,stdout=out,stderr=err,cwd=str(tmp_path))==0,err.getvalue()
    assert 'including 0.006614 PSE forgone' in out.getvalue()
    assert (root/'forgone.jsonl').exists()
    (root/'history.jsonl').unlink()
    out,err=io.StringIO(),io.StringIO()
    assert cli.main(['waste','--json'],env=env,stdout=out,stderr=err,cwd=str(tmp_path))==0,err.getvalue()
    assert json.loads(out.getvalue())['total_wasted_pse']==pytest.approx(500*8/WEEK)


def test_later_companion_window_does_not_erase_earlier_weekly_only_waiting():
    records=[reading(t,provider='codex',keys=('7d',)) for t in (1000,1400,2000)]
    records.append(reading(2300,provider='codex',used=.1,start=2200))
    row,=forgone.detect(records)
    assert (row['start_s'],row['end_s'])==(1000,2200)


def test_cache_republication_keeps_earliest_observed_basis():
    records=[reading(1000),reading(1400)]
    duplicate=json.loads(json.dumps(records[0]))
    duplicate['t']=3000
    duplicate['accounts'][0]['capacity']=99
    row,=forgone.detect([*records,duplicate])
    assert row['start_evidence']['capacity']==2


def test_waste_report_refuses_malformed_history_instead_of_shortening_it(tmp_path):
    import io
    from quota_router import cli
    root=tmp_path/'state'/'quota-router'
    root.mkdir(parents=True)
    (root/'history.jsonl').write_text('{not json}\n')
    env={'HOME':str(tmp_path),'XDG_STATE_HOME':str(tmp_path/'state'),
         'XDG_CONFIG_HOME':str(tmp_path/'config'),'PATH':'/usr/bin:/bin'}
    out,err=io.StringIO(),io.StringIO()
    code=cli.main(['waste','--json'],env=env,stdout=out,stderr=err,cwd=str(tmp_path))
    assert code!=0
    assert not out.getvalue()


def encoding(identifier, key, value, *, scope='account', start=None, end='1970-01-01T00:00:00Z'):
    return dict(version=1,id=identifier,kind='encoding',account='a' if scope=='account' else None,
                scope=scope,key=key,value=value,recorded_at='2026-10-19T00:00:00Z',
                reconstructed=True,evidence_at='2026-10-12T00:00:00Z',source='config',
                effective_from=start,effective_to=end,status='explained',evidence=['fixture'],
                reason='historical fixture',annotates=None)


def valid_ledger(ledger):
    from quota_router import calibrations
    for i,row in enumerate(ledger):
        calibrations.validate_record(row,ledger[:i])
    return ledger


def test_incomplete_companion_is_bracketed_without_overlapping_stretches(tmp_path):
    records=[reading(1000),reading(1400),reading(1500),reading(1800),
             reading(2000,used=.01,start=1900)]
    records[2]['accounts'][0]['windows']=records[2]['accounts'][0]['windows'][1:]
    rows=forgone.detect(records)
    assert [(r['start_s'],r['end_s']) for r in rows]==[(1000,1900)]
    assert forgone.merge([],rows)==rows
    assert forgone.append_records(rows,tmp_path/'forgone.jsonl')==1


@pytest.mark.parametrize('key,scope,values',[
    ('weekly_to_session','account',(4,8)),
    ('capacity','account',(2,4)),
    ('tier_scale','tier:pro',(1,2)),
])
def test_uncertain_encoding_is_excluded_without_falling_back(key,scope,values):
    records=[reading(t) for t in (1000,1400,2000)]
    if key=='tier_scale':
        for r in records:r['accounts'][0]['capacity']=None
    ledger=[encoding('default','weekly_to_session',6.25,scope='default'),
            encoding('old',key,values[0],scope=scope),
            encoding('change',key,values[1],scope=scope,start='1970-01-01T00:20:00Z',end='1970-01-01T00:30:00Z')]
    result=forgone.summarize(forgone.detect(records),ledger=valid_ledger(ledger))
    segment=result['rows'][0]['segments'][1]
    assert (segment['start_s'],segment['end_s'])==(1200,1800)
    assert segment['current']['forgone_pse'] is None
    assert 'uncertain' in segment['current']['reason']
    assert result['totals']['unrecomputable']==1
    # A belief can resolve a conversion whose historical encoding is uncertain.
    resolved=forgone.summarize(forgone.detect(records),ledger=valid_ledger([*ledger,belief()]))
    assert resolved['totals']['unrecomputable']==0
    assert resolved['totals']['forgone_pse']==pytest.approx(1000*8/WEEK)


def test_historical_tier_changes_the_rate_at_its_boundary():
    records=[reading(t) for t in (1000,1400,2000)]
    for r in records:
        r['accounts'][0].update(capacity=None,tier='max_5x' if r['t']<1500 else 'max_20x')
    ledger=[encoding('k','weekly_to_session',6.25,scope='default'),
            encoding('low','tier','max_5x'),
            encoding('high','tier','max_20x',start='1970-01-01T00:25:00Z',end='1970-01-01T00:25:00Z'),
            encoding('scale-low','tier_scale',.25,scope='tier:max_5x'),
            encoding('scale-high','tier_scale',1,scope='tier:max_20x')]
    result=forgone.summarize(forgone.detect(records),ledger=valid_ledger(ledger))
    assert result['totals']['forgone_pse']==pytest.approx(500*6.25*.25/WEEK+500*6.25/WEEK)
    assert result['rows'][0]['segments'][1]['current']['values']['tier']=='max_20x'


def test_newer_scoped_clock_cannot_change_account_wide_waiting():
    records=[reading(t) for t in (1000,1400,2000)]
    for r in records:
        a=r['accounts'][0]
        a['windows'].append(dict(a['windows'][1],key='fable',applies_to=['fable'],
                                 observed_at_s=r['t']+1))
    row,=forgone.detect(records)
    assert (row['start_s'],row['end_s'])==(1000,2000)



def test_uncertain_tier_encoding_never_selects_a_scale_without_a_belief():
    records=[reading(t) for t in (1000,1400,2000)]
    for r in records:r['accounts'][0]['capacity']=None
    ledger=valid_ledger([encoding('k','weekly_to_session',6.25,scope='default'),
        encoding('tier-old','tier','pro'),
        encoding('tier-new','tier','max_20x',start='1970-01-01T00:20:00Z',end='1970-01-01T00:30:00Z'),
        encoding('scale-pro','tier_scale',1,scope='tier:pro'),
        encoding('scale-max','tier_scale',1,scope='tier:max_20x')])
    rows=forgone.detect(records)
    result=forgone.summarize(rows,ledger=ledger)
    middle=result['rows'][0]['segments'][1]['current']
    assert middle['forgone_pse'] is None
    assert 'uncertain historical tier encoding' in middle['reason']
    resolved=forgone.summarize(rows,ledger=valid_ledger([*ledger,belief()]))
    assert resolved['totals']['unrecomputable']==0
