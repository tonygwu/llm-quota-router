"""Tests for :mod:`quota_router.weekly_reset` -- the last-resort routing heuristic.

WHY THIS EXISTS
---------------
On 2026-08-24 ``cl`` put an interactive session on the one account whose weekly
window was 100% spent. The router had not chosen it. The pick failed inside its
three-second cap, and the launcher's give-up branch was hardcoded to the default
account, which happened to be the exhausted one. A give-up branch that always lands
on the same account is a coin flip the operator cannot see.

A Claude account's weekly window resets on a fixed weekday and wall time, fixed when
the account is created. That fact needs no network, no Keychain and no token, so it
is still available in exactly the situation where live usage is not. It is a far
weaker signal than measured quota, and it is only ever consulted when there is no
measured quota at all.

THE TIMEZONE RULE THIS MODULE EXISTS TO OBEY
--------------------------------------------
The schedule is a WALL TIME IN A NAMED ZONE, never an offset and never the local
clock. "Monday 15:59 America/Los_Angeles" stays 15:59 across a daylight-saving
transition, so the interval between two consecutive resets is 7 days plus or minus
an hour twice a year. Storing an offset, or building the instant with the machine's
local timezone, produces a schedule that is correct for half the year and an hour
wrong for the other half -- and it passes on a UTC CI box either way.
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

import pytest

from quota_router.weekly_reset import (
    WeeklyReset,
    earliest_weekly_reset,
    parse_weekly_reset,
)

LA = ZoneInfo("America/Los_Angeles")
UTC = datetime.timezone.utc


def at(year, month, day, hour, minute, zone=LA) -> float:
    """An epoch second, built in a named zone so no test reads the local clock."""
    return datetime.datetime(year, month, day, hour, minute, tzinfo=zone).timestamp()


# ======================================================================================
# Parsing -- the operator types this into a config file, so it must fail loudly
# ======================================================================================


def test_the_documented_form_parses() -> None:
    schedule = parse_weekly_reset("Mon 15:59 America/Los_Angeles")

    assert schedule == WeeklyReset(weekday=0, hour=15, minute=59, zone="America/Los_Angeles")


@pytest.mark.parametrize(
    "text",
    [
        "Mon 15:59 America/Los_Angeles",
        "mon 15:59 America/Los_Angeles",
        "MONDAY 15:59 America/Los_Angeles",
        "Monday 15:59 America/Los_Angeles",
        "  Mon   15:59   America/Los_Angeles  ",
    ],
)
def test_the_weekday_is_read_case_insensitively_and_in_either_length(text) -> None:
    assert parse_weekly_reset(text).weekday == 0


def test_every_weekday_is_understood_and_monday_is_zero() -> None:
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    got = [parse_weekly_reset(f"{n} 00:00 UTC").weekday for n in names]

    assert got == [0, 1, 2, 3, 4, 5, 6]


def test_midnight_and_the_last_minute_of_the_day_are_both_legal() -> None:
    assert parse_weekly_reset("Sun 00:00 UTC").hour == 0
    assert parse_weekly_reset("Sun 23:59 UTC").minute == 59


@pytest.mark.parametrize(
    "text, needle",
    [
        ("Mon 15:59", "timezone"),
        ("15:59 America/Los_Angeles", "weekday"),
        ("Frunday 15:59 UTC", "weekday"),
        ("Mon 25:00 UTC", "time"),
        ("Mon 15:60 UTC", "time"),
        ("Mon 1559 UTC", "time"),
        ("Mon 15:59 Not/AZone", "timezone"),
        ("Mon 15:59 PST", "timezone"),
        ("", "weekday"),
        ("Mon 15:59 UTC extra", "three parts"),
    ],
)
def test_a_malformed_schedule_raises_and_names_what_is_wrong(text, needle) -> None:
    """No permissive parsing here.

    A schedule that silently defaults is worse than no schedule at all: it would send
    an interactive session to a confidently wrong account, in the one situation where
    the operator has no measurement to check it against.
    """
    with pytest.raises(ValueError) as caught:
        parse_weekly_reset(text)

    assert needle in str(caught.value)


def test_a_fixed_offset_is_refused_because_it_cannot_track_daylight_saving() -> None:
    """``UTC-08:00`` is right for half the year. A zone name is right all year."""
    with pytest.raises(ValueError) as caught:
        parse_weekly_reset("Mon 15:59 UTC-08:00")

    assert "timezone" in str(caught.value)


def test_the_text_form_round_trips() -> None:
    text = "Mon 15:59 America/Los_Angeles"

    assert parse_weekly_reset(parse_weekly_reset(text).to_text()) == parse_weekly_reset(text)


# ======================================================================================
# next_after -- which instant this schedule lands on
# ======================================================================================


def test_the_next_reset_is_later_today_when_the_hour_has_not_passed() -> None:
    schedule = parse_weekly_reset("Mon 15:59 America/Los_Angeles")
    now = at(2026, 8, 24, 9, 53)  # Monday morning

    assert schedule.next_after(now) == at(2026, 8, 24, 15, 59)


def test_the_next_reset_rolls_a_full_week_when_the_hour_has_passed() -> None:
    schedule = parse_weekly_reset("Mon 15:59 America/Los_Angeles")
    now = at(2026, 8, 24, 16, 0)  # one minute after this week's reset

    assert schedule.next_after(now) == at(2026, 8, 31, 15, 59)


def test_standing_exactly_on_the_reset_returns_the_following_week() -> None:
    """Strictly after. A window that resets *now* has nothing left to expire."""
    schedule = parse_weekly_reset("Mon 15:59 America/Los_Angeles")
    now = at(2026, 8, 24, 15, 59)

    assert schedule.next_after(now) == at(2026, 8, 31, 15, 59)


def test_a_reset_later_in_the_week_is_found_from_any_earlier_day() -> None:
    schedule = parse_weekly_reset("Fri 09:00 America/Los_Angeles")
    now = at(2026, 8, 24, 9, 53)  # Monday

    assert schedule.next_after(now) == at(2026, 8, 28, 9, 0)


def test_the_zone_is_the_schedules_own_not_the_machines() -> None:
    """Two schedules with identical wall times in different zones are different instants."""
    la = parse_weekly_reset("Mon 15:59 America/Los_Angeles")
    utc = parse_weekly_reset("Mon 15:59 UTC")
    now = at(2026, 8, 24, 0, 0, zone=UTC)

    assert utc.next_after(now) == at(2026, 8, 24, 15, 59, zone=UTC)
    assert la.next_after(now) == at(2026, 8, 24, 15, 59, zone=LA)
    assert la.next_after(now) - utc.next_after(now) == 7 * 3600  # PDT is UTC-7


# ======================================================================================
# Daylight saving -- the bug class that passes on a UTC CI box and fails on this Mac
# ======================================================================================


def test_the_wall_time_survives_a_spring_forward() -> None:
    """15:59 stays 15:59. The interval, not the wall time, is what moves.

    2026-03-08 is when America/Los_Angeles goes to PDT, so the Monday reset either
    side of it is 7 days minus one hour apart. Building the instant with a stored
    offset, or with the machine's local zone, gets this an hour wrong.
    """
    schedule = parse_weekly_reset("Mon 15:59 America/Los_Angeles")

    before = schedule.next_after(at(2026, 3, 2, 0, 0))
    after = schedule.next_after(before)

    assert before == at(2026, 3, 2, 15, 59)
    assert after == at(2026, 3, 9, 15, 59)
    assert after - before == 7 * 86400 - 3600


def test_the_wall_time_survives_a_fall_back() -> None:
    """2026-11-01 returns America/Los_Angeles to PST: 7 days plus an hour."""
    schedule = parse_weekly_reset("Mon 15:59 America/Los_Angeles")

    before = schedule.next_after(at(2026, 10, 26, 0, 0))
    after = schedule.next_after(before)

    assert after - before == 7 * 86400 + 3600


def test_a_reset_inside_the_spring_forward_gap_still_yields_an_instant() -> None:
    """02:30 does not exist on 2026-03-08 in Los Angeles. It must not raise.

    A schedule the operator can legally write must always name a real instant, and
    the honest one is the moment the wall clock jumps past it.
    """
    schedule = parse_weekly_reset("Sun 02:30 America/Los_Angeles")

    landed = schedule.next_after(at(2026, 3, 7, 0, 0))

    assert landed == at(2026, 3, 8, 3, 0)


# ======================================================================================
# earliest_weekly_reset -- the choice itself
# ======================================================================================


def test_the_account_whose_week_expires_soonest_wins() -> None:
    """The router's whole objective: spend what is about to expire.

    With no measurement available this is the only part of that objective still
    computable, which is exactly why it is the fallback and not the policy.
    """
    now = at(2026, 8, 24, 9, 53)  # Monday morning
    schedules = {
        "claude_b": parse_weekly_reset("Wed 15:59 America/Los_Angeles"),
        "claude_c": parse_weekly_reset("Mon 15:59 America/Los_Angeles"),
        "claude_d": parse_weekly_reset("Fri 15:59 America/Los_Angeles"),
    }

    assert earliest_weekly_reset(schedules, now) == ("claude_c", at(2026, 8, 24, 15, 59))


def test_an_account_whose_reset_just_passed_sorts_last_not_first() -> None:
    """Its window just refilled, so it has a whole week before anything expires."""
    now = at(2026, 8, 24, 16, 0)
    schedules = {
        "claude_b": parse_weekly_reset("Mon 15:59 America/Los_Angeles"),  # +7d
        "claude_c": parse_weekly_reset("Tue 15:59 America/Los_Angeles"),  # +1d
    }

    assert earliest_weekly_reset(schedules, now)[0] == "claude_c"


def test_a_tie_breaks_on_the_account_id_so_the_answer_is_reproducible() -> None:
    now = at(2026, 8, 24, 9, 53)
    schedules = {
        "claude_d": parse_weekly_reset("Mon 15:59 America/Los_Angeles"),
        "claude_b": parse_weekly_reset("Mon 15:59 America/Los_Angeles"),
    }

    assert earliest_weekly_reset(schedules, now)[0] == "claude_b"


def test_no_schedules_at_all_is_no_answer_rather_than_a_guess() -> None:
    assert earliest_weekly_reset({}, at(2026, 8, 24, 9, 53)) is None


# ======================================================================================
# same_window -- is a cached reading still describing the week we are in?
# ======================================================================================
#
# The fallback may skip an account whose last cached weekly reading was ~100% spent.
# That is only sound if the reading describes the CURRENT week. A reading taken before
# the account's last rollover describes a window that no longer exists, and acting on
# it would skip an account whose week has since refilled completely -- the exact
# inversion the fallback is there to avoid. The schedule already knows where the
# boundaries are, so this needs no age threshold and no guesswork.


def test_two_instants_inside_one_week_are_the_same_window() -> None:
    schedule = parse_weekly_reset("Mon 15:59 America/Los_Angeles")

    assert schedule.same_window(at(2026, 8, 24, 9, 53), at(2026, 8, 24, 12, 30)) is True


def test_an_instant_either_side_of_the_reset_is_not() -> None:
    """08:00 Monday and 17:00 Monday are hours apart and a whole window apart."""
    schedule = parse_weekly_reset("Mon 15:59 America/Los_Angeles")

    assert schedule.same_window(at(2026, 8, 24, 8, 0), at(2026, 8, 24, 17, 0)) is False


def test_a_reading_taken_exactly_at_the_reset_belongs_to_the_new_week() -> None:
    schedule = parse_weekly_reset("Mon 15:59 America/Los_Angeles")

    assert schedule.same_window(at(2026, 8, 24, 15, 59), at(2026, 8, 24, 17, 0)) is True
    assert schedule.same_window(at(2026, 8, 24, 15, 58), at(2026, 8, 24, 17, 0)) is False


def test_a_reading_six_days_old_can_still_be_this_week() -> None:
    """Age is not the test. Which side of the boundary it sits on is."""
    schedule = parse_weekly_reset("Mon 15:59 America/Los_Angeles")

    assert schedule.same_window(at(2026, 8, 24, 16, 5), at(2026, 8, 30, 23, 0)) is True


def test_a_reading_ten_minutes_old_can_be_last_week() -> None:
    schedule = parse_weekly_reset("Mon 15:59 America/Los_Angeles")

    assert schedule.same_window(at(2026, 8, 24, 15, 55), at(2026, 8, 24, 16, 5)) is False


def test_the_comparison_holds_across_a_daylight_saving_change() -> None:
    """The week containing the spring-forward is one window, an hour short or not."""
    schedule = parse_weekly_reset("Mon 15:59 America/Los_Angeles")

    assert schedule.same_window(at(2026, 3, 3, 12, 0), at(2026, 3, 9, 12, 0)) is True
    assert schedule.same_window(at(2026, 3, 9, 12, 0), at(2026, 3, 9, 16, 0)) is False


def test_a_reading_from_the_future_is_not_treated_as_this_week_by_accident() -> None:
    """A cache written by a machine whose clock ran ahead. Symmetric, by construction."""
    schedule = parse_weekly_reset("Mon 15:59 America/Los_Angeles")

    assert schedule.same_window(at(2026, 8, 31, 12, 0), at(2026, 8, 24, 12, 0)) is False
