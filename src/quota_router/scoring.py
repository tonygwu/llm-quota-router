"""Pure scoring: turn :class:`~quota_router.types.AccountSnapshot` objects into ranks.

This module is the arithmetic half of the router. It is **pure** in the strict sense the
project uses that word:

* it imports the standard library and :mod:`quota_router.types`, nothing else,
* it never touches the filesystem, the environment, the network or the wall clock --
  every time-dependent value is derived from the ``now_s`` the caller passes in,
* the same inputs always produce the same outputs, including tie order.

The algorithm
-------------
Per window::

    remaining_fraction = 1 - used_fraction
    burn_rate prior    = 1 / length_s                      (uniform pacing)
    expected_demand    = burn_rate * time_to_reset         (still expected to burn)
    slack              = remaining_fraction - expected_demand

When the oracle publishes its own pacing baseline (``expectedPct``) the same number falls
out of ``slack = expected_used - used``, and that is what is used verbatim -- the 7-day
and model-scoped windows carry one, the 5-hour window does not.

Per account, across the windows that APPLY to the requested model class (a window whose
``applies_to`` excludes the class is skipped outright; the tightest of the rest throttles
the others)::

    min_slack     = min(slack)
    min_remaining = min(remaining_fraction)
    capacity      = tier capacity ratio (max_20x 1.0, max_5x 0.25, pro 1.0)

And then one of two mandatory regimes, decided **globally** over the whole candidate
field before anybody is scored:

``REGIME_A`` -- some candidate has ``min_slack > 0``
    ``score = provider_weight * capacity * min_slack``. Spend the quota most at risk of
    expiring unused.
``REGIME_B`` -- every candidate has ``min_slack <= 0``
    ``score = provider_weight * capacity * min_remaining``. Spend from the pool that can
    actually serve the call.

Regime B is a correctness requirement, not a refinement. Multiplying a *negative* slack
by a capacity ratio moves it toward zero, so a single scaled-slack objective ranks the
smaller, more depleted account first under scarcity: ``claude_c`` at ``-0.5 * 0.25 =
-0.125`` beats ``claude`` at ``-0.2 * 1.0 = -0.2``, which is precisely backwards. Surplus
and deficit are different objectives and get different formulas.

One trap this module deliberately does **not** paper over: :attr:`ScoreBreakdown.score`
is scaled, :attr:`ScoreBreakdown.min_slack` is not. Hysteresis in
:mod:`quota_router.select` compares the unscaled quantity, because an additive epsilon on
a capacity-scaled score is 4x stricter for a ``max_5x`` account than for a ``max_20x``
one.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any, Final

from .types import (
    DEFAULT_PROVIDER_WEIGHTS,
    REGIME_A,
    REGIME_B,
    AccountSnapshot,
    ScoreBreakdown,
    Window,
    WindowSlack,
    normalize_model_class,
    normalize_regime,
)

__all__ = [
    "remaining_fraction",
    "time_to_reset_s",
    "burn_rate_prior",
    "expected_demand",
    "window_slack",
    "applicable_windows",
    "per_window_slacks",
    "min_slack",
    "min_remaining",
    "binding_window_key",
    "fits",
    "cfg_get",
    "provider_weight",
    "decide_regime",
    "account_score",
    "ranking_sort_key",
    "rank",
]


_MISSING: Final[object] = object()


def _clamp01(value: float) -> float:
    """Clamp ``value`` into ``[0.0, 1.0]``."""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


# ======================================================================================
# Window-level arithmetic
# ======================================================================================


def remaining_fraction(window: Window) -> float:
    """Fraction of ``window``'s budget still available (``1 - used_fraction``)."""
    return window.remaining_fraction


def time_to_reset_s(window: Window, now_s: float) -> float:
    """Seconds until ``window`` rolls over, clamped at zero.

    A reset time in the past means the oracle read is stale, not that the account owes
    quota; the window has already rolled over, so nothing more is expected to burn.
    """
    return max(0.0, float(window.resets_at_s) - float(now_s))


def burn_rate_prior(window: Window) -> float:
    """Uniform pacing prior for ``window``: ``1 / length_s`` of budget per second."""
    return window.burn_rate_prior


def expected_demand(
    window: Window, now_s: float, burn_rate_per_s: float | None = None
) -> float:
    """Fraction of ``window``'s budget still expected to burn before it resets.

    Three sources, in precedence order:

    1. ``burn_rate_per_s``, when the caller measured one. An explicit rate is a
       deliberate statement that the uniform prior is wrong for this account, so it
       overrides the oracle's baseline too (the oracle's ``expectedPct`` *is* a uniform
       prior, just computed upstream). Must be finite and non-negative -- a negative or
       ``NaN`` rate is a bug in the caller and raises rather than quietly inverting a
       routing decision.
    2. :attr:`Window.expected_used_fraction` when the oracle published one, used verbatim
       as ``1 - expected_used`` so that ``slack == expected_used - used`` exactly.
    3. The uniform prior ``time_to_reset / length_s``.

    The result is clamped into ``[0, 1]``: it is a fraction of one window's budget, and
    stale data (a reset further out than the window is long) must not manufacture demand
    beyond the whole budget.
    """
    if burn_rate_per_s is not None:
        rate = float(burn_rate_per_s)
        if not math.isfinite(rate) or rate < 0.0:
            raise ValueError(
                f"burn_rate_per_s must be a finite, non-negative rate, got {burn_rate_per_s!r}"
            )
        return _clamp01(rate * time_to_reset_s(window, now_s))

    if window.expected_used_fraction is not None:
        return _clamp01(1.0 - window.expected_used_fraction)

    return _clamp01(burn_rate_prior(window) * time_to_reset_s(window, now_s))


def window_slack(
    window: Window, now_s: float, burn_rate_per_s: float | None = None
) -> float:
    """``remaining_fraction - expected_demand``: surplus (> 0) or deficit (<= 0).

    Positive slack is budget that will expire unused unless something spends it. The sign
    is always computed here and never read from the oracle's ``aheadOfPace`` boolean,
    which is self-inconsistent upstream (it reports ``false`` for ``60.0 > 54.6`` on one
    window and ``true`` for ``80.0 > 54.6`` on the next).
    """
    return remaining_fraction(window) - expected_demand(window, now_s, burn_rate_per_s)


# ======================================================================================
# Account-level aggregation
# ======================================================================================


def applicable_windows(
    snap: AccountSnapshot, model_class: str | None = None
) -> tuple[Window, ...]:
    """The windows of ``snap`` that constrain a call for ``model_class``.

    A window whose ``applies_to`` excludes the class is skipped entirely. A window with
    ``applies_to is None`` is account-wide and always applies, and a caller that names no
    model class keeps every window: an unspecified request could land anywhere, so no
    constraint may be dropped.
    """
    return snap.applicable_windows(model_class)


def per_window_slacks(
    snap: AccountSnapshot, now_s: float, model_class: str | None = None
) -> tuple[WindowSlack, ...]:
    """Explanation rows for every window of ``snap``, binding one flagged.

    Non-applicable windows are included (with ``applicable=False`` and a reason) so a
    human can see *why* a window was skipped; they never influence the score.
    """
    return snap.slacks(now_s, model_class)


def min_slack(
    snap: AccountSnapshot, now_s: float, model_class: str | None = None
) -> float | None:
    """Minimum slack over applicable windows, or ``None`` when none apply."""
    return snap.min_slack(now_s, model_class)


def min_remaining(
    snap: AccountSnapshot, model_class: str | None = None
) -> float | None:
    """Minimum remaining budget over applicable windows, or ``None`` when none apply."""
    return snap.min_remaining_fraction(model_class)


def binding_window_key(
    snap: AccountSnapshot, now_s: float, model_class: str | None = None
) -> str | None:
    """Key of the window that produced :func:`min_slack` (first wins on ties)."""
    return snap.binding_window_key(now_s, model_class)


def fits(
    snap: AccountSnapshot, model_class: str | None = None
) -> bool:
    """Can ``snap`` serve the call at all -- is there budget left in every applicable window?

    Independent of slack: an account can be far behind pace (deeply negative slack) and
    still have plenty of budget left, and an account can be exactly on pace with nothing
    left at all.
    """
    remaining = min_remaining(snap, model_class)
    return remaining is not None and remaining > 0.0


# ======================================================================================
# Configuration access (duck-typed on purpose)
# ======================================================================================


def cfg_get(cfg: Any, *names: str, default: Any = None) -> Any:
    """Read the first present, non-``None`` setting named in ``names`` out of ``cfg``.

    Configuration is *policy* and lives in another layer; this module must not import it
    (purity), so settings are read structurally. ``cfg`` may be ``None``, a mapping
    (including a JSON blob straight off disk) or any object with attributes -- a
    dataclass, a ``SimpleNamespace``, a settings singleton. A key that is present but
    ``None`` counts as "not configured" and falls through to ``default``.
    """
    if cfg is None:
        return default
    if isinstance(cfg, Mapping):
        for name in names:
            value = cfg.get(name, _MISSING)
            if value is not _MISSING and value is not None:
                return value
        return default
    for name in names:
        value = getattr(cfg, name, _MISSING)
        if value is not _MISSING and value is not None:
            return value
    return default


def provider_weight(cfg: Any, provider: str) -> float:
    """Policy weight for ``provider``, defaulting to a neutral ``1.0``.

    Read from ``cfg.provider_weights`` / ``cfg["provider_weights"]`` (alias: ``weights``).
    An unconfigured provider is neutral. A weight that is negative or not a real number is
    a configuration bug that would silently invert or poison every ``argmax``, so it fails
    loud instead.
    """
    weights = cfg_get(cfg, "provider_weights", "weights")
    fallback = DEFAULT_PROVIDER_WEIGHTS.get(provider, 1.0)
    if weights is None:
        raw: Any = fallback
    elif isinstance(weights, Mapping):
        raw = weights.get(provider, fallback)
        if raw is None:
            raw = fallback
    else:
        raise TypeError(
            f"provider weights must be a mapping of provider -> weight, got {weights!r}"
        )

    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"provider weight for {provider!r} must be a real number, got {raw!r}"
        ) from exc
    if not math.isfinite(value) or value < 0.0:
        raise ValueError(
            f"provider weight for {provider!r} must be finite and >= 0, got {value!r}"
        )
    return value


# ======================================================================================
# Regime selection and scoring
# ======================================================================================


def decide_regime(
    snapshots: Iterable[AccountSnapshot],
    now_s: float,
    model_class: str | None = None,
) -> str:
    """:data:`REGIME_A` if *any* candidate has ``min_slack > 0``, else :data:`REGIME_B`.

    The regime is a property of the field, not of an account: mixing objectives inside one
    ranking would compare a surplus number against a remaining-budget number. Candidates
    with no applicable window have no opinion and are ignored here (they cannot be scored
    at all -- see :func:`account_score`).
    """
    for snap in snapshots:
        slack = min_slack(snap, now_s, model_class)
        if slack is not None and slack > 0.0:
            return REGIME_A
    return REGIME_B


def account_score(
    snap: AccountSnapshot,
    now_s: float,
    cfg: Any = None,
    model_class: str | None = None,
    regime: str | None = REGIME_A,
) -> ScoreBreakdown:
    """Score one account under one regime and explain the result.

    Args:
        snap: The candidate.
        now_s: Epoch seconds. Nothing here reads a clock.
        cfg: Optional policy object/mapping; only ``provider_weights`` is consulted.
        model_class: Which model class the call targets, gating ``applies_to`` windows.
        regime: :data:`REGIME_A` or :data:`REGIME_B`. Pass the regime :func:`decide_regime`
            chose for the whole field. ``None`` is a single-account convenience that
            derives the regime from this account alone -- never use it to score a field.

    Returns:
        A :class:`ScoreBreakdown` whose ``score`` is scaled and whose ``min_slack`` is
        not. An account with no applicable window comes back ``eligible=False`` with a
        zero score: there is no honest number to give it, and inventing one would let it
        outrank accounts that were actually measured.
    """
    rows = per_window_slacks(snap, now_s, model_class)
    capacity = snap.capacity
    weight = provider_weight(cfg, snap.provider)
    slack = min_slack(snap, now_s, model_class)
    remaining = min_remaining(snap, model_class)

    if slack is None or remaining is None:
        normalized_class = normalize_model_class(model_class)
        reason = (
            "no usage windows in snapshot"
            if not snap.windows
            else f"no window applies to model class {normalized_class!r}"
        )
        return ScoreBreakdown(
            account_id=snap.id,
            score=0.0,
            min_slack=0.0,
            binding_window=None,
            regime=normalize_regime(regime),
            capacity=capacity,
            provider_weight=weight,
            fits=False,
            per_window=rows,
            eligible=False,
            reason=reason,
            min_remaining=None,
        )

    resolved = normalize_regime(regime) or (REGIME_A if slack > 0.0 else REGIME_B)
    objective = slack if resolved == REGIME_A else remaining
    label = "min_slack" if resolved == REGIME_A else "min_remaining"
    score = weight * capacity * objective
    binding = binding_window_key(snap, now_s, model_class)

    return ScoreBreakdown(
        account_id=snap.id,
        score=score,
        min_slack=slack,
        binding_window=binding,
        regime=resolved,
        capacity=capacity,
        provider_weight=weight,
        fits=remaining > 0.0,
        per_window=rows,
        eligible=True,
        reason=(
            f"regime {resolved}: {weight:.2f} weight x {capacity:.2f} capacity x "
            f"{objective:+.4f} {label} = {score:+.4f} (binding {binding})"
        ),
        min_remaining=remaining,
    )


def ranking_sort_key(row: ScoreBreakdown) -> tuple[int, float, str]:
    """Total order over :class:`ScoreBreakdown` rows: eligible first, score desc, id asc.

    Ineligible rows sort below every eligible one *regardless of score* -- their score is
    a placeholder, and a zero placeholder would otherwise outrank a legitimately negative
    regime-A score. The account id breaks ties so that two accounts with identical numbers
    always come back in the same order.
    """
    return (0 if row.eligible else 1, -row.score, row.account_id)


def rank(
    snapshots: Iterable[AccountSnapshot],
    now_s: float,
    cfg: Any = None,
    model_class: str | None = None,
) -> list[ScoreBreakdown]:
    """Score every candidate under one globally-chosen regime and sort them, best first.

    The regime is decided once, over the whole field, *before* anything is scored (see
    :func:`decide_regime`); every row then carries that same regime so the scores are
    mutually comparable.
    """
    candidates = tuple(snapshots)
    regime = decide_regime(candidates, now_s, model_class)
    rows = [account_score(snap, now_s, cfg, model_class, regime) for snap in candidates]
    rows.sort(key=ranking_sort_key)
    return rows
