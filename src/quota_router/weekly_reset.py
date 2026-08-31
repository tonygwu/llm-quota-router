"""When each account's weekly window rolls over -- the one fact that needs no network.

WHAT THIS IS FOR
----------------
The router decides from measured quota. This module is what is left when there is no
measurement at all: no usage endpoint, no cached payload, no readable token. A Claude
account's weekly window resets on a fixed weekday at a fixed wall time, settled when
the account is created, so it is knowable from a config file alone.

That makes it a *last* resort and never a policy input. "Measured, and the account is
empty" and "not measured" are opposite facts that look identical if you only ask
whether quota is available, and only the second one may reach this module. The caller
owns that distinction; see :func:`quota_router.launcher.measured_any`.

The heuristic itself is the router's own objective with everything unmeasurable
removed: spend the pool whose week expires soonest, because that is the quota with the
least time left to be used at all.

WHY A ZONE NAME AND NOT AN OFFSET
---------------------------------
A schedule is a wall time in a named IANA zone. "Mon 15:59 America/Los_Angeles" stays
15:59 through a daylight-saving transition, so two consecutive resets are 7 days apart
in wall terms and 7 days plus or minus an hour apart in real terms, twice a year.

Storing ``UTC-08:00`` instead is right for eight months and an hour wrong for four.
Building the instant with the machine's local timezone is worse: it passes on a UTC CI
box and fails on the operator's laptop, depending on the month. Neither shortcut is
available here, because :func:`parse_weekly_reset` refuses anything that is not an IANA
zone name and nothing in this module reads a clock or a local timezone.

Nothing here makes a routing decision, reads the environment, or has side effects.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Final, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "WEEKDAY_NAMES",
    "WeeklyReset",
    "earliest_weekly_reset",
    "parse_weekly_reset",
]

#: Monday first, matching :meth:`datetime.date.weekday`, which is the only reason the
#: indices in this module can be compared to it without conversion.
WEEKDAY_NAMES: Final[tuple[str, ...]] = (
    "Mon",
    "Tue",
    "Wed",
    "Thu",
    "Fri",
    "Sat",
    "Sun",
)

_WEEKDAY_INDEX: Final[Mapping[str, int]] = {
    **{name.lower(): index for index, name in enumerate(WEEKDAY_NAMES)},
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_FORM: Final[str] = '"Mon 15:59 America/Los_Angeles"'


def _weekday_of(text: str) -> int | None:
    return _WEEKDAY_INDEX.get(text.strip().lower())


def _time_of(text: str) -> tuple[int, int] | None:
    """``"15:59"`` -> ``(15, 59)``, or ``None`` when it is not a 24-hour wall time."""
    parts = text.split(":")
    if len(parts) != 2:
        return None
    hour_text, minute_text = parts
    if not (hour_text.isdigit() and minute_text.isdigit()):
        return None
    hour, minute = int(hour_text), int(minute_text)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _zone_of(text: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def parse_weekly_reset(text: str) -> WeeklyReset:
    """Read ``"Mon 15:59 America/Los_Angeles"``, or raise saying which part is wrong.

    Deliberately unforgiving. This value is consulted only when the router has no
    measurement to check itself against, so a schedule that quietly defaulted to
    something plausible would send an interactive session to a confidently wrong
    account with nothing on hand to contradict it. A missing timezone is an error,
    not an invitation to assume the machine's own.

    Raises:
        ValueError: The text is not three whitespace-separated parts, or one of them
            is not a weekday, a 24-hour ``HH:MM``, or an IANA timezone name.
    """
    parts = str(text).split()
    if len(parts) > 3:
        raise ValueError(
            f"weekly reset {text!r} must be three parts -- weekday, HH:MM, timezone, "
            f"as in {_FORM}"
        )
    weekday = _weekday_of(parts[0]) if parts else None
    if weekday is None:
        raise ValueError(
            f"weekly reset {text!r} does not start with a weekday "
            f"({', '.join(WEEKDAY_NAMES)}), as in {_FORM}"
        )
    clock = _time_of(parts[1]) if len(parts) > 1 else None
    if clock is None:
        raise ValueError(
            f"weekly reset {text!r} does not carry a 24-hour HH:MM time, as in {_FORM}"
        )
    if len(parts) < 3:
        raise ValueError(
            f"weekly reset {text!r} names no timezone. Give an IANA zone name, as in "
            f"{_FORM} -- an offset like UTC-08:00 is an hour wrong for a third of the "
            f"year and cannot follow daylight saving"
        )
    if _zone_of(parts[2]) is None:
        raise ValueError(
            f"weekly reset {text!r}: {parts[2]!r} is not an IANA timezone name, as in "
            f"{_FORM}. Abbreviations (PST) and offsets (UTC-08:00) are not zones: "
            f"neither one knows when daylight saving starts"
        )
    hour, minute = clock
    return WeeklyReset(weekday=weekday, hour=hour, minute=minute, zone=parts[2])


def _wall(stamp: float, zone: ZoneInfo) -> tuple[datetime.date, int, int]:
    """The (date, hour, minute) a zone's clock reads at ``stamp``."""
    moment = datetime.datetime.fromtimestamp(stamp, zone)
    return moment.date(), moment.hour, moment.minute


@dataclass(frozen=True, slots=True)
class WeeklyReset:
    """One account's weekly rollover: a wall time on a weekday, in a named zone.

    Args:
        weekday: ``0`` is Monday, matching :meth:`datetime.date.weekday`.
        hour / minute: 24-hour wall time in :attr:`zone`, not in UTC and not local.
        zone: IANA zone name. Validated on construction, because a schedule that
            cannot name an instant is worse than no schedule at all.
    """

    weekday: int
    hour: int
    minute: int
    zone: str

    def __post_init__(self) -> None:
        if not 0 <= self.weekday <= 6:
            raise ValueError(f"weekday must be 0 (Monday) to 6 (Sunday), got {self.weekday}")
        if not 0 <= self.hour <= 23 or not 0 <= self.minute <= 59:
            raise ValueError(f"not a wall time: {self.hour:02d}:{self.minute:02d}")
        if _zone_of(self.zone) is None:
            raise ValueError(f"{self.zone!r} is not an IANA timezone name")

    def to_text(self) -> str:
        """The config form, round-tripping through :func:`parse_weekly_reset`."""
        return f"{WEEKDAY_NAMES[self.weekday]} {self.hour:02d}:{self.minute:02d} {self.zone}"

    def next_after(self, now_s: float) -> float:
        """The next reset strictly after ``now_s``, as an epoch second.

        Strictly after on purpose: a window resetting at this very instant has nothing
        left in it to expire, so it is the *following* week that is at risk.
        """
        zone = ZoneInfo(self.zone)
        here = datetime.datetime.fromtimestamp(now_s, zone)
        day = here.date() + datetime.timedelta(days=(self.weekday - here.weekday()) % 7)
        for _ in range(2):
            stamp = self._on(day, zone)
            if stamp > now_s:
                return stamp
            day += datetime.timedelta(days=7)
        # Unreachable: the first candidate is within seven days ahead of `now_s`, so
        # the second is at least seven days past it. Raising beats returning a wrong
        # instant; every caller of this module already treats it as best-effort.
        raise RuntimeError(f"{self.to_text()} named no instant after {now_s}")

    def same_window(self, observed_at_s: float, now_s: float) -> bool:
        """Do these two instants fall inside the same weekly window?

        Asked of a *cached* usage reading: does it still describe the week we are in?
        Age cannot answer that. A reading six days old is current if the rollover has
        not happened since, and a reading ten minutes old is worthless if it has --
        the window it measured no longer exists, and the account it said was spent may
        have refilled completely.

        Both instants are mapped to the deadline they are counting down to, so the
        comparison is exact, needs no tolerance, and is symmetric: a cache written by
        a machine whose clock ran ahead is judged the same way as one that ran behind.
        """
        return self.next_after(observed_at_s) == self.next_after(now_s)

    # -- the two days a year when a wall time is not a single instant ------------------

    def _on(self, day: datetime.date, zone: ZoneInfo) -> float:
        """This schedule's instant on ``day``.

        Two calendar days a year do not map wall times one-to-one:

        * **Fall back.** The wall time happens twice. ``fold=0`` takes the first, which
          is the earlier deadline and therefore the safer one to route against.
        * **Spring forward.** The wall time does not happen at all. Python would
          resolve it with the pre-transition offset, landing *after* the jump by
          however far into the gap it sat. The honest answer is the moment the clock
          passes the scheduled time, which is the transition itself, so the gap is
          detected and searched for rather than papered over.
        """
        naive = datetime.datetime(day.year, day.month, day.day, self.hour, self.minute)
        stamp = naive.replace(tzinfo=zone).timestamp()
        target = (day, self.hour, self.minute)
        if _wall(stamp, zone) == target:
            return stamp

        # The wall time was skipped. `fold=1` reads it with the other side's offset, so
        # the two candidates straddle the transition; the first second whose clock has
        # reached `target` is the instant we want, and the clock is monotonic across
        # that span, so a bisection finds it exactly.
        other = naive.replace(tzinfo=zone, fold=1).timestamp()
        low, high = int(min(stamp, other)), int(max(stamp, other))
        while low < high:
            middle = (low + high) // 2
            if _wall(middle, zone) >= target:
                high = middle
            else:
                low = middle + 1
        return float(low)


def earliest_weekly_reset(
    schedules: Mapping[str, WeeklyReset], now_s: float
) -> tuple[str, float] | None:
    """The account whose week expires soonest, as ``(account_id, epoch_seconds)``.

    Args:
        schedules: Already-filtered candidates. This function does not know which
            accounts are enabled, which the caller asked for, or which the router
            could not read -- deciding that here would put half the policy in the
            wrong layer.
        now_s: Epoch seconds. Nothing here reads a clock.

    Returns:
        ``None`` when no candidate has a schedule, which is a real answer: it means
        the caller must degrade further rather than invent a deadline.
    """
    ranked = sorted(
        (schedule.next_after(now_s), account_id)
        for account_id, schedule in schedules.items()
    )
    if not ranked:
        return None
    stamp, account_id = ranked[0]
    return account_id, stamp
