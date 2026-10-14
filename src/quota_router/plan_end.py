"""Treat an account's quota as ending when its subscription does.

WHY THIS EXISTS
---------------
A cancelled subscription keeps serving until the end of the period already paid for,
then stops, and the vendor publishes nothing about that ahead of time. Observed on
2026-10-14: minutes after a Max account was cancelled, its profile still read
``subscription_status = "active"``, and its usage windows kept their usual weekly
boundary, four days past the date the plan would end.

Left alone, the router would believe that boundary. It would rank the account behind
accounts whose weeks end sooner and let most of its quota lapse unspent, then keep
routing to it after the plan had ended. Quota stranded by a plan ending is the same waste
as quota stranded at a weekly reset, so the honest fix is to tell the router the real
deadline and let its own rule -- spend what expires first -- do the rest. Pinning the
account instead (``CL_ONLY``) would be worse: once its five-hour window filled there would
be nowhere else for work to go.

THE RULE
--------
``[accounts.<id>] plan_ends`` is an instant with a zone. Before it, every window on the
account that would reset later is treated as resetting at ``plan_ends``, with any upstream
pacing baseline dropped so pace is derived from the shortened deadline. From ``plan_ends``
on, the account is unavailable, with a reason that names the instant. The reading itself is
kept, so ``status`` still shows it and nothing calls the account unreadable.

WHERE IT APPLIES
----------------
Once, to the snapshots that eligibility, scoring and ``status`` read. History records the
snapshot as read: the waste series and calibration treat recorded deadlines as the vendor's,
and a moved one would invent resets that never happened.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import datetime, timezone

from .types import AccountSnapshot

__all__ = ["apply_plan_ends", "format_instant", "describe_plan_ends"]


def format_instant(epoch_s: float) -> str:
    """``2026-10-16T00:00Z``: UTC, to the minute, the way the operator wrote it."""
    return datetime.fromtimestamp(epoch_s, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _ended_note(snapshot: AccountSnapshot, ends_s: float) -> str:
    note = (
        f"plan ended at {format_instant(ends_s)} (plan_ends in config); remove the key, or "
        f"set enabled = false, once the account is gone"
    )
    return f"{note}; {snapshot.note}" if snapshot.note else note


def apply_plan_ends(
    snapshots: Iterable[AccountSnapshot],
    plan_ends: Mapping[str, float],
    now_s: float,
) -> list[AccountSnapshot]:
    """Every snapshot, with each configured account's plan end applied."""
    adjusted: list[AccountSnapshot] = []
    for snapshot in snapshots:
        ends_s = plan_ends.get(snapshot.id)
        if ends_s is None:
            adjusted.append(snapshot)
        elif now_s >= ends_s:
            adjusted.append(
                replace(
                    snapshot,
                    available=False,
                    confidence=0.0,
                    note=_ended_note(snapshot, ends_s),
                )
            )
        else:
            windows = tuple(
                replace(window, resets_at_s=ends_s, expected_used_fraction=None)
                if window.resets_at_s > ends_s
                else window
                for window in snapshot.windows
            )
            adjusted.append(replace(snapshot, windows=windows))
    return adjusted


def describe_plan_ends(plan_ends: Mapping[str, float], now_s: float) -> list[str]:
    """One line per configured plan end, for ``status``."""
    lines: list[str] = []
    for account_id, ends_s in sorted(plan_ends.items(), key=lambda item: item[1]):
        if now_s >= ends_s:
            lines.append(
                f"plan ended: {account_id} at {format_instant(ends_s)}; excluded from routing"
            )
        else:
            lines.append(
                f"plan ends: {account_id} at {format_instant(ends_s)}; its windows count "
                f"as ending then"
            )
    return lines
