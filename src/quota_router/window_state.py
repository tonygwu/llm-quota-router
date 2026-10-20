"""Temporal waiting evidence shared by loss accounting and stock classification.

A zero near observation + length is only a candidate. Two advancing deadlines,
separated by more than the provider tolerance, establish sliding. A fixed deadline
or positive use establishes running, even if the first use rounded to zero.
Claude's clock-driven weekly window always runs. No ping heuristic is imported.
"""
from __future__ import annotations

from collections import defaultdict
import math

WAIT_TOLERANCE_S = 240.0  # Poll cadence plus measured provider offsets, as in window-ping.
FIXED_JITTER_S = 2.0  # Provider deadlines have whole-second precision.
GAP_S = 600.0  # Beyond five poll intervals, an unbracketed end is a lower bound.


def _number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f'invalid {name}: expected finite number')
    return value


def observations(records):
    """Strip private identities; retain only successful vendor observations.

    Legacy history fixtures predate available/confidence. Their absent flags mean
    readable, as in the reset reader. Explicit failures never supply observations.
    Cache duplicates use the vendor clock and collapse without extending time.
    """
    accounts = defaultdict(dict)
    for record in records:
        if record.get('event', 'status') != 'status':
            continue
        for raw in record['accounts']:
            if not raw.get('available', True) or raw.get('confidence', 1) <= 0:
                continue
            if raw['source'] not in {'live', 'cache', 'claude_json'}:
                continue
            windows = []
            for window in raw['windows']:
                w = {k: window[k] for k in ('key', 'length_s', 'used_fraction', 'resets_at_s', 'observed_at_s')}
                for key in ('length_s', 'used_fraction', 'resets_at_s', 'observed_at_s'):
                    _number(w[key], key)
                if w['length_s'] <= 0 or not 0 <= w['used_fraction'] <= 1:
                    raise ValueError('invalid window length or use')
                w['applies_to'] = window.get('applies_to') or []
                windows.append(w)
            for window in raw.get('not_running', []):
                w = {k: window[k] for k in ('key', 'length_s', 'observed_at_s')}
                _number(w['length_s'], 'length_s')
                _number(w['observed_at_s'], 'observed_at_s')
                if w['length_s'] <= 0:
                    raise ValueError('invalid window length')
                windows.append(dict(w, not_running=True, applies_to=[]))
            if not windows:
                continue
            times = {w['observed_at_s'] for w in windows}
            # Claude can combine a fresh weekly response with its last local 5h
            # reading. Preserve both clocks and refuse to infer pool waiting from
            # such a mixed observation. A known running weekly still rules it out.
            at = max(times)
            observation = dict(at_s=at, provider=raw['provider'], windows=windows,
                               tier=raw['tier'], capacity=raw.get('capacity'))
            recorded = record.get('t', at)  # Legacy in-memory reset fixtures lack t.
            _number(recorded, 'history record time')
            old = accounts[raw['id']].get(at)
            # Re-publication may apply newer config to an old vendor response.
            # Keep its earliest recorded basis, independent of input file order.
            if old is None or recorded < old[0]:
                accounts[raw['id']][at] = (recorded, observation)
    return {a: sorted((r for _, r in reads.values()), key=lambda r: r['at_s'])
            for a, reads in accounts.items()}


def _trace(provider, trace):
    """Classify a window retrospectively; confirmation can resolve earlier zeros."""
    fixed = defaultdict(list)
    for i, w in trace:
        if not w.get('not_running'):
            fixed[w['resets_at_s']].append(w)
    running = {reset for reset, ws in fixed.items()
               if any(w['used_fraction'] > 0 for w in ws)
               or max(w['observed_at_s'] for w in ws)-min(w['observed_at_s'] for w in ws) > WAIT_TOLERANCE_S}
    result, pending = {}, []
    waiting_start, last_running = None, None
    for i, w in trace:
        at, length = w['observed_at_s'], w['length_s']
        state, reason, start = 'unknown', 'insufficient_temporal_evidence', None
        explicit = w.get('not_running', False)
        clock = provider == 'claude' and w['key'] == '7d' and not w['applies_to']
        opening = None if explicit else w['resets_at_s'] - length
        tiled = (not explicit and last_running is not None and
                 last_running['resets_at_s'] <= at and
                 abs(opening-last_running['resets_at_s']) <= FIXED_JITTER_S and w['used_fraction'] == 0)
        anchor = waiting_start if waiting_start is not None else (pending[0][2] if pending else None)
        if clock:
            state, reason = 'running', 'claude_weekly_runs_on_clock'
        elif anchor is not None and not explicit and opening < anchor-FIXED_JITTER_S:
            state, reason = 'stale', 'old_window_after_reset'
        elif explicit:
            state, reason = 'waiting', 'provider_says_not_running'
            start = anchor if anchor is not None else at
            waiting_start = start
        elif w['used_fraction'] > 0 or (w['resets_at_s'] in running and not tiled):
            state, reason = 'running', 'positive_use_or_fixed_deadline'
            waiting_start, pending = None, []
            last_running = w
        elif abs(w['resets_at_s']-at-length) <= WAIT_TOLERANCE_S or tiled:
            start = anchor if anchor is not None else (
                last_running['resets_at_s'] if last_running is not None and
                last_running['resets_at_s'] <= at else at)
            pending.append((i, w, start))
            sliding = any(not prev.get('not_running') and
                          w['resets_at_s']-prev['resets_at_s'] > WAIT_TOLERANCE_S and
                          abs(w['resets_at_s']-at-length) <= WAIT_TOLERANCE_S
                          for _, prev, _ in pending[:-1])
            if waiting_start is not None or sliding:
                state, reason = 'waiting', 'confirmed_sliding_deadline'
                waiting_start = start
                for j, _, since in pending:
                    result[j] = dict(state='waiting', reason=reason, waiting_since_s=since)
                # Only the original anchor is needed for subsequent confirmation.
                pending = pending[:1]
        else:
            state, reason = 'running', 'fixed_deadline'
            waiting_start, pending = None, []
            last_running = w
        result[i] = dict(state=state, reason=reason, waiting_since_s=start if state=='waiting' else None)
    return result


def classify(records):
    """Per-account readings with window states and the pool-wide classification.

    Account ids already identify separate Antigravity pools. A missing companion
    window is unknown, never evidence that every account-wide window waits.
    Scoped constraints are classified too, but cannot enter pool loss accounting.
    """
    accounts = observations(records)
    for reads in accounts.values():
        traces = defaultdict(list)
        for i, read in enumerate(reads):
            for w in read['windows']:
                traces[w['key']].append((i, w))
        states = {key: _trace(reads[0]['provider'], trace) for key, trace in traces.items()}
        known = set()
        if reads[0]['provider']=='antigravity':
            known |= {'5h','7d'}
        elif reads[0]['provider'] in {'claude','codex'}:
            known.add('7d')
        for i, read in enumerate(reads):
            read['window_states'] = {w['key']: states[w['key']][i] for w in read['windows']}
            unscoped = {w['key'] for w in read['windows'] if not w['applies_to']}
            known.update(unscoped)
            values = [read['window_states'][key]['state'] for key in unscoped]
            if 'running' in values:
                read['state'] = 'running'
            elif 'stale' in values:
                read['state'] = 'stale'
            elif (known and known <= unscoped and all(v=='waiting' for v in values)
                  and all(w['observed_at_s']==read['at_s'] for w in read['windows'] if not w['applies_to'])):
                read['state'] = 'waiting'
            else:
                read['state'] = 'unknown'
    return accounts
