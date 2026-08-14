"""Pure selection: eligibility, hysteresis and confidence, on top of the scores.

:mod:`quota_router.scoring` answers "who looks best right now". This module answers the
question the caller actually asked -- "who should serve *this* call" -- by adding the
three things a raw ``argmax`` gets wrong:

**Eligibility.** An account that is logged out, cooling off after a rate-limit rejection,
or has nothing left in an applicable window cannot serve the call *at any score*. Those
candidates are removed before ranking, not merely out-ranked, and each one keeps a reason
so the exclusion is auditable.

**Hysteresis.** Scores from a live oracle jitter. Following them literally means switching
accounts several times a minute, which destroys prompt caching and makes behaviour
impossible to reason about. Two independent brakes stop that: a **dwell counted in calls**
(the incumbent must serve ``min_dwell_calls`` before any switch is considered, with
``max_dwell_s`` as a wall-clock escape hatch for very low-frequency callers) and a
**switch margin** the challenger must clear. The margin is applied to the *unscaled*
:attr:`~quota_router.types.ScoreBreakdown.min_slack`, never to the capacity-scaled score:
an additive epsilon on a scaled score is four times stricter for a ``max_5x`` account than
for a ``max_20x`` one, which would silently pin the router to its biggest pool.

The margin is specified as ``challenger > incumbent * 1.15 + 0.02``, which is a real
margin whenever the incumbent's slack is positive -- i.e. throughout regime A, the regime
hysteresis exists to stabilize. Note the shape it takes when the incumbent's slack is
*negative*: the multiplicative term then lowers the bar rather than raising it (an
incumbent at ``-0.20`` yields a threshold of ``-0.21``), so under scarcity the additive
term is doing all the work and near-ties can hand the seat over. That is deliberate --
this is the specified rule, and the call-count dwell, not the margin, is what bounds
thrashing to one switch per ``min_dwell_calls`` calls in every regime.

**Confidence.** A snapshot the router is unsure about (stale cache, guessed tier) gets its
score *shrunk toward the field mean*, not multiplied by its confidence. Multiplying is the
wrong model twice over: it flattens an uncertain account toward zero (which in regime B --
where every score is a positive remaining fraction -- is maximal pessimism rather than
uncertainty), and it makes a confidently-bad account indistinguishable from an
unknown-but-plausible one. Shrinkage says "we do not know, so assume it is about average",
which is what uncertainty actually means.

Purity
------
Standard library, :mod:`quota_router.types` and its pure sibling
:mod:`quota_router.scoring` only. No filesystem, environment, network or clock: ``now_s``
is a parameter, and the sticky state is an ordinary in-memory object the caller owns and
may persist however it likes (:meth:`SelectionState.to_dict` round-trips through JSON).

:func:`select` never raises for an ordinary condition -- no candidates, everything
exhausted, an unusable config knob, a snapshot with no windows. It always returns a
:class:`~quota_router.types.Decision`, possibly with ``chosen=None`` and ``degraded=True``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass, field, replace
from typing import Any, Final

from .scoring import (
    account_score,
    cfg_get,
    min_remaining,
    rank,
    ranking_sort_key,
)
from .types import (
    SOURCE_ASSUMED,
    SOURCE_CACHE,
    SOURCE_UNKNOWN,
    TIER_UNKNOWN,
    AccountSnapshot,
    Decision,
    ScoreBreakdown,
    normalize_model_class,
)

__all__ = [
    "DEFAULT_MIN_DWELL_CALLS",
    "DEFAULT_MAX_DWELL_S",
    "DEFAULT_SWITCH_MARGIN_RATIO",
    "DEFAULT_SWITCH_MARGIN_ABS",
    "DEFAULT_MIN_REMAINING_FLOOR",
    "DEFAULT_MAX_STALENESS_S",
    "StickyEntry",
    "SelectionState",
    "sticky_key",
    "apply_confidence_shrinkage",
    "select",
]


# ======================================================================================
# Policy defaults
# ======================================================================================

#: Calls the incumbent must serve before a switch is even considered. Counted in CALLS,
#: not seconds: the thing being protected is the *sequence of invocations*, and a
#: time-based dwell would let a burst of thirty calls in ten seconds ping-pong freely.
DEFAULT_MIN_DWELL_CALLS: Final[int] = 3

#: Wall-clock escape hatch for the call-count dwell. Without it a caller that makes one
#: request an hour would pin its first choice forever, long after the world moved on.
#: Note this only retires the *dwell*; the switch margin still has to be cleared.
DEFAULT_MAX_DWELL_S: Final[float] = 900.0

#: Multiplicative part of the switch margin, applied to the incumbent's unscaled slack.
DEFAULT_SWITCH_MARGIN_RATIO: Final[float] = 1.15

#: Additive part of the switch margin, in units of unscaled slack.
DEFAULT_SWITCH_MARGIN_ABS: Final[float] = 0.02

#: Floor on ``min_remaining``, **exclusive**: a candidate must have strictly more than
#: this left in every applicable window. The default of 0.0 therefore still excludes an
#: account with a fully-consumed window, which cannot serve the call at all.
DEFAULT_MIN_REMAINING_FLOOR: Final[float] = 0.0

#: How old the winning snapshot may be before the decision is reported as degraded.
DEFAULT_MAX_STALENESS_S: Final[float] = 900.0

_DUBIOUS_SOURCES: Final[frozenset[str]] = frozenset(
    {SOURCE_CACHE, SOURCE_ASSUMED, SOURCE_UNKNOWN}
)


# ======================================================================================
# Sticky state
# ======================================================================================


def sticky_key(eligible_ids: Iterable[str], model_class: str | None) -> str:
    """Stable key identifying "this candidate set, for this model class".

    Stickiness is scoped to the exact field the decision was made over: if a candidate
    appears, disappears or becomes ineligible, the previous choice was made against a
    different question and its dwell must not carry over. The model class is part of the
    key for the same reason -- a Fable-scoped window can bind for one class and be skipped
    for another, so the two rankings are unrelated.

    Returned as a printable string rather than :func:`hash`, deliberately: the builtin
    ``hash`` of a string is salted per process, so a state file written by one invocation
    would not match the key computed by the next one -- and this router runs once per
    call.
    """
    ids = ",".join(sorted({str(account_id) for account_id in eligible_ids}))
    return f"{normalize_model_class(model_class) or '*'}|{ids}"


@dataclass(slots=True)
class StickyEntry:
    """The incumbent for one :func:`sticky_key`, and how long it has held the seat."""

    account_id: str
    #: Calls this incumbent has served since it took the seat (its first call counts).
    calls: int = 0
    #: When it took the seat.
    since_s: float = 0.0
    #: When it last served a call.
    last_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "calls": self.calls,
            "since_s": self.since_s,
            "last_s": self.last_s,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StickyEntry:
        return cls(
            account_id=str(data.get("account_id", "")),
            calls=int(data.get("calls", 0) or 0),
            since_s=float(data.get("since_s", 0.0) or 0.0),
            last_s=float(data.get("last_s", 0.0) or 0.0),
        )


@dataclass(slots=True)
class SelectionState:
    """Everything :func:`select` remembers between calls.

    Plain in-memory data owned by the caller. This module never reads or writes it from
    disk -- persistence (if any) belongs to a layer that is allowed to do I/O, and
    :meth:`to_dict` / :meth:`from_dict` round-trip through JSON for exactly that purpose.

    Args:
        entries: :func:`sticky_key` -> current incumbent.
        exhausted_until: account id -> epoch seconds until which that account is in
            cooldown (recorded by the caller when a real invocation came back rate
            limited). While that deadline is in the future the account is ineligible no
            matter how good its numbers look.
    """

    entries: dict[str, StickyEntry] = field(default_factory=dict)
    exhausted_until: dict[str, float] = field(default_factory=dict)

    def incumbent(self, key: str) -> StickyEntry | None:
        """The incumbent for ``key``, or ``None`` when this field has no history."""
        return self.entries.get(key)

    def record(self, key: str, account_id: str, now_s: float) -> StickyEntry:
        """Record that ``account_id`` served a call for ``key`` at ``now_s``."""
        entry = self.entries.get(key)
        if entry is None or entry.account_id != account_id:
            entry = StickyEntry(
                account_id=account_id, calls=1, since_s=now_s, last_s=now_s
            )
            self.entries[key] = entry
        else:
            entry.calls += 1
            entry.last_s = now_s
        return entry

    def mark_exhausted(self, account_id: str, until_s: float) -> None:
        """Put ``account_id`` in cooldown until ``until_s`` (the later deadline wins)."""
        previous = self.exhausted_until.get(account_id)
        self.exhausted_until[account_id] = (
            until_s if previous is None else max(previous, float(until_s))
        )

    def is_exhausted(self, account_id: str, now_s: float) -> bool:
        """Is ``account_id`` still cooling off at ``now_s``?"""
        deadline = self.exhausted_until.get(account_id)
        return deadline is not None and float(deadline) > now_s

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready mapping (plain types only)."""
        return {
            "entries": {key: entry.to_dict() for key, entry in self.entries.items()},
            "exhausted_until": dict(self.exhausted_until),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> SelectionState:
        """Rebuild a state from :meth:`to_dict` output; unusable rows are dropped."""
        if not isinstance(data, Mapping):
            return cls()
        entries: dict[str, StickyEntry] = {}
        raw_entries = data.get("entries")
        if isinstance(raw_entries, Mapping):
            for key, value in raw_entries.items():
                if isinstance(value, Mapping) and value.get("account_id"):
                    entries[str(key)] = StickyEntry.from_dict(value)
        cooldowns: dict[str, float] = {}
        raw_cooldowns = data.get("exhausted_until")
        if isinstance(raw_cooldowns, Mapping):
            for account_id, deadline in raw_cooldowns.items():
                number = _as_number(deadline)
                if number is not None:
                    cooldowns[str(account_id)] = number
        return cls(entries=entries, exhausted_until=cooldowns)


# ======================================================================================
# Small tolerant helpers
# ======================================================================================


def _as_number(value: Any) -> float | None:
    """Coerce ``value`` to a finite float, or ``None`` when it cannot be one."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _knob(
    cfg: Any, name: str, default: float, warnings: list[str], *aliases: str
) -> float:
    """Read a numeric policy knob, falling back (loudly) when it is unusable.

    A malformed knob is a config bug, but it is not a reason to refuse to route a call --
    the caller is trying to make an LLM request, not validate a settings file. The default
    is used and the substitution is surfaced in :attr:`Decision.warnings`.
    """
    raw = cfg_get(cfg, name, *aliases)
    if raw is None:
        return default
    number = _as_number(raw)
    if number is None:
        warnings.append(f"unusable config value for {name}: {raw!r}; using {default}")
        return default
    return number


def _override(
    value: Any, current: float, name: str, warnings: list[str]
) -> float:
    """Apply a direct keyword override, or keep ``current`` and say why not."""
    if value is None:
        return current
    number = _as_number(value)
    if number is None:
        warnings.append(f"unusable {name} override: {value!r}; using {current}")
        return current
    return number


class _CfgOverlay:
    """Read-only view that answers a few keys itself and delegates the rest to ``base``.

    Lets a caller override one policy value (``provider_weights``) without this module
    having to know what shape the underlying config object is, or having to import the
    config layer -- which purity forbids.
    """

    __slots__ = ("_base", "_overrides")

    def __init__(self, base: Any, overrides: Mapping[str, Any]) -> None:
        self._base = base
        self._overrides = dict(overrides)

    def __getattr__(self, name: str) -> Any:
        # Private/dunder probes are never config keys, and answering them would risk
        # recursing through the slots this class has not finished setting.
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._overrides:
            return self._overrides[name]
        value = cfg_get(self._base, name)
        if value is None:
            raise AttributeError(name)
        return value


def _sticky_hint(
    sticky: Any, now_s: float, min_dwell_calls: int
) -> StickyEntry | None:
    """Turn a caller-supplied incumbent hint into an entry whose dwell is already served.

    A bare hint (the account id another layer's TTL-based stickiness settled on) carries
    no call history, so inventing a dwell for it would be fabricating data. Treating the
    dwell as satisfied leaves the switch margin -- which the hint's owner explicitly hands
    us -- as the sole brake, which is exactly its policy.
    """
    if sticky is None:
        return None
    account_id = sticky if isinstance(sticky, str) else None
    if account_id is None:
        for attribute in ("account_id", "account"):
            candidate = getattr(sticky, attribute, None)
            if isinstance(candidate, str) and candidate:
                account_id = candidate
                break
    if not account_id:
        return None
    return StickyEntry(
        account_id=account_id,
        calls=max(min_dwell_calls, 1),
        since_s=now_s,
        last_s=now_s,
    )


def _cooldown_deadline(
    account_id: str,
    state: SelectionState,
    extra: Mapping[str, Any] | None,
) -> float | None:
    """Latest cooldown deadline for ``account_id`` across the state and the call argument."""
    deadlines: list[float] = []
    stored = _as_number(state.exhausted_until.get(account_id))
    if stored is not None:
        deadlines.append(stored)
    if isinstance(extra, Mapping):
        passed = _as_number(extra.get(account_id))
        if passed is not None:
            deadlines.append(passed)
    return max(deadlines) if deadlines else None


# ======================================================================================
# Confidence
# ======================================================================================


def apply_confidence_shrinkage(
    rows: list[ScoreBreakdown], confidence_by_id: Mapping[str, float]
) -> list[ScoreBreakdown]:
    """Shrink each score toward the field mean in proportion to its uncertainty.

    ``score' = c * score + (1 - c) * mean_score``. A fully-trusted row (``c == 1``) is
    untouched; a completely untrusted row (``c == 0``) collapses onto the mean, i.e. "we
    have no information, treat it as unremarkable". Nothing else on the row is rewritten:
    :attr:`~quota_router.types.ScoreBreakdown.min_slack` stays raw because the hysteresis
    margin is defined on the unscaled quantity.
    """
    if not rows:
        return []
    mean_score = math.fsum(row.score for row in rows) / len(rows)
    shrunk: list[ScoreBreakdown] = []
    for row in rows:
        confidence = _as_number(confidence_by_id.get(row.account_id, 1.0))
        confidence = 1.0 if confidence is None else min(1.0, max(0.0, confidence))
        if confidence >= 1.0:
            shrunk.append(row)
            continue
        adjusted = confidence * row.score + (1.0 - confidence) * mean_score
        shrunk.append(
            replace(
                row,
                score=adjusted,
                reason=(
                    f"{row.reason}; confidence {confidence:.2f} shrank score "
                    f"{row.score:+.4f} -> {adjusted:+.4f} toward field mean {mean_score:+.4f}"
                ),
            )
        )
    shrunk.sort(key=ranking_sort_key)
    return shrunk


# ======================================================================================
# Eligibility
# ======================================================================================


def _partition(
    snaps: Iterable[AccountSnapshot],
    now_s: float,
    cfg: Any,
    model_class: str | None,
    state: SelectionState,
    exhausted_until: Mapping[str, Any] | None,
    floor: float,
    warnings: list[str],
) -> tuple[list[AccountSnapshot], list[ScoreBreakdown]]:
    """Split candidates into (eligible snapshots, excluded breakdowns with reasons).

    Excluded rows keep the score they *would* have had, so the ``--json`` audit trail can
    show that a rejected account was in fact the highest scorer and was still, correctly,
    not chosen.
    """
    eligible: list[AccountSnapshot] = []
    excluded: list[ScoreBreakdown] = []

    for snap in snaps:
        if snap is None:  # defensive: a source layer that produced a gap
            continue
        # Scored with regime=None so the row is self-consistent even though this account
        # never takes part in the field's regime decision.
        row = account_score(snap, now_s, cfg, model_class, None)

        if not snap.windows:
            warnings.append(f"{snap.id}: snapshot carries no usage windows")

        reason: str | None = None
        if not snap.available:
            detail = snap.note or "marked not available by its source"
            reason = f"account unavailable ({detail})"
        else:
            deadline = _cooldown_deadline(snap.id, state, exhausted_until)
            if deadline is not None and deadline > now_s:
                reason = (
                    f"exhausted: cooling off for another {deadline - now_s:.0f}s "
                    f"(until epoch {deadline:.0f})"
                )
            elif not row.eligible:
                reason = row.reason  # no windows at all / none apply to this model class
            else:
                remaining = min_remaining(snap, model_class)
                if remaining is None or remaining <= floor:
                    reason = (
                        f"min_remaining {0.0 if remaining is None else remaining:.4f} "
                        f"at or below floor {floor:.4f}"
                    )

        if reason is None:
            eligible.append(snap)
        else:
            excluded.append(replace(row, eligible=False, reason=reason))

    excluded.sort(key=ranking_sort_key)
    return eligible, excluded


# ======================================================================================
# The decision
# ======================================================================================


def select(
    snapshots: Iterable[AccountSnapshot],
    now_s: float,
    cfg: Any = None,
    model_class: str | None = None,
    state: SelectionState | MutableMapping[str, Any] | None = None,
    exhausted_until: Mapping[str, Any] | None = None,
    *,
    provider_weights: Mapping[str, Any] | None = None,
    min_remaining: float | None = None,
    switch_margin: float | None = None,
    sticky: Any = None,
    record: bool = True,
) -> Decision:
    """Choose which account should serve this call.

    Args:
        snapshots: Candidate snapshots, in any order. (Named so that a caller which
            dispatches on the first parameter's name -- the CLI does -- hands this
            function account snapshots rather than already-ranked breakdowns.)
        now_s: Epoch seconds. Nothing here reads a clock.
        cfg: Optional policy mapping/object. Recognized keys (all optional):
            ``provider_weights``, ``min_remaining_floor``, ``min_dwell_calls``,
            ``max_dwell_s``, ``switch_margin_ratio``, ``switch_margin_abs``,
            ``max_staleness_s``.
        model_class: Which model class the call targets. Gates ``applies_to`` windows and
            takes part in the sticky key.
        state: Sticky state across calls. ``None`` means "no memory", which disables
            hysteresis for this call. A plain mutable mapping is accepted (and written
            back) so a caller can persist it as JSON.
        exhausted_until: Extra ``{account_id: epoch_seconds}`` cooldowns for this call
            only, merged with (and never weaker than) the ones in ``state``.
        provider_weights: Direct override of ``cfg["provider_weights"]``, for a caller
            that keeps its policy somewhere this module must not import.
        min_remaining: Direct override of the eligibility floor.
        switch_margin: Direct override of the additive part of the switch margin. In units
            of **unscaled** ``min_slack``; it must never arrive pre-multiplied by capacity.
        sticky: Incumbent hint from a caller that keeps its own stickiness (an account id,
            or any object with ``.account_id`` / ``.account``). Used only when ``state``
            has no entry for this candidate set. Such a hint carries no call count, so the
            call-count dwell is treated as already served and the switch margin alone
            decides -- the hint's owner has its own freshness policy.
        record: When ``False`` the decision is computed without updating ``state`` -- for
            previews, dry runs and tests.

    Returns:
        A :class:`~quota_router.types.Decision`. Always. ``chosen is None`` means nothing
        was eligible, which is information, not an error.
    """
    warnings: list[str] = []

    resolved_state, writeback = _resolve_state(state)
    floor = _knob(
        cfg, "min_remaining_floor", DEFAULT_MIN_REMAINING_FLOOR, warnings, "min_remaining"
    )
    min_dwell_calls = int(
        _knob(cfg, "min_dwell_calls", DEFAULT_MIN_DWELL_CALLS, warnings)
    )
    max_dwell_s = _knob(cfg, "max_dwell_s", DEFAULT_MAX_DWELL_S, warnings)
    margin_ratio = _knob(
        cfg,
        "switch_margin_ratio",
        DEFAULT_SWITCH_MARGIN_RATIO,
        warnings,
        "switch_margin_multiplier",
    )
    margin_abs = _knob(
        cfg,
        "switch_margin_abs",
        DEFAULT_SWITCH_MARGIN_ABS,
        warnings,
        "switch_margin",
        "switch_margin_epsilon",
    )
    max_staleness_s = _knob(cfg, "max_staleness_s", DEFAULT_MAX_STALENESS_S, warnings)

    # Direct keyword overrides, for callers whose policy lives somewhere this module is
    # not allowed to import from. Each one beats the equivalent cfg key.
    floor = _override(min_remaining, floor, "min_remaining", warnings)
    margin_abs = _override(switch_margin, margin_abs, "switch_margin", warnings)
    scoring_cfg: Any = cfg
    if provider_weights is not None:
        scoring_cfg = _CfgOverlay(cfg, {"provider_weights": provider_weights})

    candidates = [snap for snap in snapshots if snap is not None]
    eligible, excluded = _partition(
        candidates,
        now_s,
        scoring_cfg,
        model_class,
        resolved_state,
        exhausted_until,
        floor,
        warnings,
    )

    if not eligible:
        warnings.append(
            "no eligible account: "
            + (
                "; ".join(f"{row.account_id}: {row.reason}" for row in excluded)
                or "no candidates were supplied"
            )
        )
        decision = Decision(
            chosen=None,
            ranked=(),
            excluded=tuple(excluded),
            reason="no eligible account could serve this call",
            regime=None,
            sticky_applied=False,
            degraded=True,
            warnings=tuple(warnings),
        )
        _writeback(writeback, resolved_state, record)
        return decision

    ranked = rank(eligible, now_s, scoring_cfg, model_class)
    ranked = apply_confidence_shrinkage(
        ranked, {snap.id: snap.confidence for snap in eligible}
    )

    key = sticky_key((snap.id for snap in eligible), model_class)
    incumbent = resolved_state.incumbent(key) or _sticky_hint(
        sticky, now_s, min_dwell_calls
    )
    chosen_row, sticky_applied, sticky_note = _apply_hysteresis(
        ranked,
        incumbent,
        now_s,
        min_dwell_calls=min_dwell_calls,
        max_dwell_s=max_dwell_s,
        margin_ratio=margin_ratio,
        margin_abs=margin_abs,
    )

    if ranked[0].account_id != chosen_row.account_id:
        ranked = [chosen_row] + [row for row in ranked if row is not chosen_row]

    chosen_snap = next(snap for snap in eligible if snap.id == chosen_row.account_id)
    warnings.extend(_quality_warnings(chosen_snap, now_s, max_staleness_s))

    if record:
        resolved_state.record(key, chosen_row.account_id, now_s)
    _writeback(writeback, resolved_state, record)

    reason = sticky_note or (
        f"{chosen_row.account_id} wins regime {chosen_row.regime}: {chosen_row.reason}"
    )
    return Decision(
        chosen=chosen_row.account_id,
        ranked=tuple(ranked),
        excluded=tuple(excluded),
        reason=reason,
        regime=chosen_row.regime,
        sticky_applied=sticky_applied,
        degraded=bool(warnings),
        warnings=tuple(warnings),
    )


def _apply_hysteresis(
    ranked: list[ScoreBreakdown],
    incumbent: StickyEntry | None,
    now_s: float,
    *,
    min_dwell_calls: int,
    max_dwell_s: float,
    margin_ratio: float,
    margin_abs: float,
) -> tuple[ScoreBreakdown, bool, str]:
    """Decide whether the top-ranked challenger may take the seat from the incumbent.

    Returns ``(chosen_row, sticky_applied, reason_or_empty)``.

    Two brakes, both of which must release before a switch happens:

    * the **dwell** -- the incumbent must have served ``min_dwell_calls`` calls, or have
      held the seat for ``max_dwell_s`` seconds. This is what bounds thrashing to one
      switch per ``min_dwell_calls`` calls no matter how the scores jitter;
    * the **margin** -- the challenger's *unscaled* ``min_slack`` must beat
      ``incumbent_min_slack * margin_ratio + margin_abs``.

    An incumbent that is no longer eligible is not in ``ranked`` at all (its key changed
    with the candidate set), so nothing here can pin the router to an account that cannot
    serve the call.
    """
    leader = ranked[0]
    if incumbent is None:
        return leader, False, ""

    seated = next((row for row in ranked if row.account_id == incumbent.account_id), None)
    if seated is None or seated.account_id == leader.account_id:
        return leader, False, ""

    dwell_calls_met = incumbent.calls >= min_dwell_calls
    dwell_time_met = (now_s - incumbent.since_s) >= max_dwell_s
    threshold = seated.min_slack * margin_ratio + margin_abs
    margin_met = leader.min_slack > threshold

    if dwell_calls_met or dwell_time_met:
        if margin_met:
            return leader, False, ""
        return (
            seated,
            True,
            (
                f"kept {seated.account_id}: challenger {leader.account_id} slack "
                f"{leader.min_slack:+.4f} did not clear the switch margin "
                f"{threshold:+.4f} (incumbent slack {seated.min_slack:+.4f} x "
                f"{margin_ratio:.2f} + {margin_abs:.2f})"
            ),
        )

    return (
        seated,
        True,
        (
            f"kept {seated.account_id}: dwell not met ({incumbent.calls} of "
            f"{min_dwell_calls} calls, {now_s - incumbent.since_s:.0f}s of "
            f"{max_dwell_s:.0f}s), challenger {leader.account_id} slack "
            f"{leader.min_slack:+.4f}"
        ),
    )


def _quality_warnings(
    snap: AccountSnapshot, now_s: float, max_staleness_s: float
) -> list[str]:
    """Notes about anything that makes the winning snapshot less than fully trustworthy."""
    notes: list[str] = []
    if snap.confidence < 1.0:
        notes.append(
            f"{snap.id}: confidence {snap.confidence:.2f}; score was shrunk "
            f"toward the field mean"
        )
    if snap.tier == TIER_UNKNOWN:
        notes.append(f"{snap.id}: tier unknown, capacity assumed neutral")
    if snap.source in _DUBIOUS_SOURCES:
        notes.append(f"{snap.id}: snapshot source is {snap.source!r}")
    staleness = snap.staleness_s(now_s)
    if staleness is not None and staleness > max_staleness_s:
        notes.append(
            f"{snap.id}: snapshot is stale ({staleness:.0f}s old, "
            f"limit {max_staleness_s:.0f}s)"
        )
    return notes


def _resolve_state(
    state: SelectionState | MutableMapping[str, Any] | None,
) -> tuple[SelectionState, MutableMapping[str, Any] | None]:
    """Accept a :class:`SelectionState`, a plain mutable mapping, or nothing."""
    if isinstance(state, SelectionState):
        return state, None
    if isinstance(state, MutableMapping):
        return SelectionState.from_dict(state), state
    return SelectionState(), None


def _writeback(
    target: MutableMapping[str, Any] | None, state: SelectionState, record: bool
) -> None:
    """Push a mapping-backed state back to the caller's object."""
    if target is None or not record:
        return
    target.clear()
    target.update(state.to_dict())
