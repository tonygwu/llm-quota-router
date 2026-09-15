"""Hold back part of an account's weekly pool for a person working by hand.

WHY THIS EXISTS
---------------
Some accounts serve two consumers at once: this router's automated work, and a person
using the same subscription interactively. The case that motivated it is a desktop app
that can only ever be signed in to one account, while headless runs are routed across
several. Routing that spends such an account to zero leaves the person with nothing until
the weekly reset, and nothing in the waste objective prevents that: a spent pool wastes
nothing.

THE RULE
--------
::

    reserve   = manual_rate_per_day x days_to_weekly_reset
    spendable = max(0, remaining - reserve)

``manual_rate_per_day`` is a fraction of the weekly pool per day: the share the person is
expected to use each day. The reserve shrinks as the reset approaches, because quota the
person cannot use before the reset is quota nobody should hold back. At the reset it is
zero, so a reserve can never be the reason quota expires unused.

It is configured per account (``[accounts.<id>] manual_rate_per_day``) and has no default.
An account without the key routes exactly as it did before.

WHERE IT APPLIES
----------------
Once, to the account's 7-day window, before eligibility, scoring and ``status`` read the
snapshot, so all three see the same spendable figure. History records the snapshot as
read, never as reserved: calibration and the waste series treat recorded values as usage,
and a reserve that shrinks over time would read as usage running backwards.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Final

from .types import WINDOW_KEY_7D, AccountSnapshot

__all__ = [
    "ManualReserve",
    "accounts_without_weekly_window",
    "apply_manual_reserve",
    "manual_reserve",
]

DAY_S: Final[float] = 86400.0


@dataclass(frozen=True, slots=True)
class ManualReserve:
    """One account's reserve at one instant, with the inputs that produced it.

    ``reserve`` is reported uncapped, so an operator can see that a rate is holding back
    more than the account has left; ``spendable`` is what routing actually uses.
    """

    account_id: str
    rate_per_day: float
    days_to_reset: float
    remaining: float
    reserve: float
    spendable: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "rate_per_day": self.rate_per_day,
            "days_to_reset": self.days_to_reset,
            "remaining": self.remaining,
            "reserve": self.reserve,
            "spendable": self.spendable,
        }


def _finite_non_negative(value: float, name: str) -> float:
    out = float(value)
    if not math.isfinite(out) or out < 0.0:
        raise ValueError(f"{name} must be finite and >= 0, got {value!r}")
    return out


def manual_reserve(
    *, account_id: str, remaining: float, days_to_reset: float, rate_per_day: float
) -> ManualReserve:
    """Compute the reserve and the spendable remainder for one weekly window."""
    rate = _finite_non_negative(rate_per_day, "rate_per_day")
    days = _finite_non_negative(days_to_reset, "days_to_reset")
    left = _finite_non_negative(remaining, "remaining")
    if left > 1.0:
        raise ValueError(f"remaining is a fraction of the weekly pool, got {remaining!r}")
    held = rate * days
    return ManualReserve(
        account_id=account_id,
        rate_per_day=rate,
        days_to_reset=days,
        remaining=left,
        reserve=held,
        spendable=max(0.0, left - held),
    )


def apply_manual_reserve(
    snapshots: Iterable[AccountSnapshot],
    rates: Mapping[str, float],
    now_s: float,
) -> tuple[tuple[AccountSnapshot, ...], tuple[ManualReserve, ...]]:
    """Return the snapshots with each reserve taken out of its 7-day window.

    An account with no rate, or with no 7-day window, is returned unchanged; the second
    case is reported by :func:`accounts_without_weekly_window` so a configured reserve
    never silently does nothing.
    """
    adjusted: list[AccountSnapshot] = []
    reserves: list[ManualReserve] = []
    for snapshot in snapshots:
        rate = rates.get(snapshot.id)
        weekly = snapshot.window(WINDOW_KEY_7D) if rate is not None else None
        if rate is None or weekly is None:
            adjusted.append(snapshot)
            continue
        held = manual_reserve(
            account_id=snapshot.id,
            remaining=weekly.remaining_fraction,
            days_to_reset=weekly.time_to_reset_s(now_s) / DAY_S,
            rate_per_day=rate,
        )
        reserves.append(held)
        spent_or_held = min(1.0, max(0.0, 1.0 - held.spendable))
        windows = tuple(
            replace(window, used_fraction=spent_or_held) if window is weekly else window
            for window in snapshot.windows
        )
        note = (snapshot.note + "; " if snapshot.note else "") + (
            f"{held.reserve:.4f} of the weekly pool held for manual use"
        )
        adjusted.append(replace(snapshot, windows=windows, note=note))
    return tuple(adjusted), tuple(reserves)


def accounts_without_weekly_window(
    snapshots: Iterable[AccountSnapshot], rates: Mapping[str, float]
) -> list[str]:
    """Accounts that set a rate and were read, but publish no 7-day window to hold it in.

    An account that was not read at all is not listed: it is already reported as
    unreadable, and naming it twice sends the reader after two problems.
    """
    return [
        snapshot.id
        for snapshot in snapshots
        if snapshot.id in rates and snapshot.windows and snapshot.window(WINDOW_KEY_7D) is None
    ]
