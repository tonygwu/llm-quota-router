#!/usr/bin/env python3
"""Report retained waiting stretches without writing live state.

All inputs are explicit private copies. Optional output is restricted to a fresh
scratch state directory under temporary storage. Live adoption belongs to #32 and
validates against the calibration ledger already adopted under #26;
--beliefs-as-of reproduces the report before a later belief append.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import tempfile

from quota_router import calibrations, forgone


def reconstruct(history_home, ledger, ping_events=(), beliefs_as_of=None):
    records=[]
    inputs=[]
    paths=sorted(history_home.glob('history*.jsonl'))
    if not paths:
        raise ValueError('no retained history files')
    for path in paths:
        item=dict(name=path.name,attempted=0,succeeded=0,failed=0,failed_by_kind={})
        text=path.read_text()
        if text and not text.endswith('\n'):
            raise ValueError(f'{path.name}: partial final history line')
        for line_no,line in enumerate(text.splitlines(),1):
            item['attempted']+=1
            try:
                row=json.loads(line)
                if not isinstance(row,dict) or not isinstance(row['t'],(int,float)):
                    raise ValueError('invalid record clock')
                records.append(row)
            except (ValueError,KeyError,TypeError) as exc:
                raise ValueError(f'{path.name}:{line_no}: invalid_history: {exc}') from exc
            item['succeeded']+=1
        inputs.append(item)
    rows=forgone.detect(records)
    summary=forgone.summarize(rows,ledger=ledger,beliefs_as_of=beliefs_as_of,ping_events=ping_events)
    return dict(inputs=inputs,progress={key:sum(i[key] for i in inputs) for key in ('attempted','succeeded','failed')},
        failed_by_kind={},history_first_record_s=min(r['t'] for r in records),
        history_last_record_s=max(r['t'] for r in records),
        finished_records=[r for r in rows if r['status']!='accruing'],report=summary,beliefs_as_of=beliefs_as_of,
        operator_gates=['Approve the backup and live raw-stretch adoption under #32; the calibration ledger adopted under #26 is not rewritten',
                        'Validate the adopted beliefs: a cutoff before their append keeps both historical Gemini candidates unresolved and excluded'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history-home',type=Path,required=True)
    parser.add_argument('--ledger-file',type=Path,required=True)
    parser.add_argument('--ping-journal',type=Path)
    parser.add_argument('--beliefs-as-of',help='UTC instant; report only ledger records recorded before it')
    parser.add_argument('--output-state-home',type=Path)
    args=parser.parse_args()
    # The existing strict ledger loader validates references and every raw record.
    ledger=calibrations._loads(args.ledger_file.read_bytes())
    events=[]
    if args.ping_journal is not None:
        from quota_router.window_ping import _journal
        events=_journal(args.ping_journal)
    result=reconstruct(args.history_home,ledger,events,args.beliefs_as_of)
    if args.output_state_home is not None:
        from importlib.util import spec_from_file_location,module_from_spec
        spec=spec_from_file_location('reconstruction',Path(__file__).with_name('reconstruct-calibrations.py'))
        module=module_from_spec(spec)
        spec.loader.exec_module(module)
        destination=module.check_scratch_destination(args.output_state_home)
        destination.mkdir(parents=True,exist_ok=False)
        env={'XDG_STATE_HOME':str(destination)}
        appended=forgone.append_records(result['finished_records'],env=env)
        result['scratch_write']=dict(attempted=len(result['finished_records']),succeeded=appended,
                                     failed=0,failed_by_kind={})
        result['scratch_state_home']=str(destination)
    print(json.dumps(result,indent=2,allow_nan=False))


if __name__=='__main__':
    main()
