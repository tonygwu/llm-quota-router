"""Turn a :class:`~quota_router.types.Decision` into something a human can argue with.

A routing decision that cannot be explained is indistinguishable from a coin flip, and
this router makes a *counter-intuitive* choice on purpose -- it deliberately spends from
the account that looks "most used" when that account's quota is about to expire anyway.
The operator will ask "why did it pick that one?", and the answer has to name:

* **which window bound the decision** (the tightest applicable one throttles the rest),
* **the arithmetic of that window's slack** (``remaining - expected demand``),
* **which regime was in force** -- surplus (A) or scarcity (B), because the two optimize
  different quantities and the same numbers mean different things under each,
* **by how much it beat the runner-up**, against the switch margin.

The canonical one-liner::

    claude_b won: 7d binds (0.44 slack = 0.61 remaining - 0.17 expected over 4.1h to
    reset); beat claude by 48% > 15% margin

Pure rendering: no decisions are made here, and nothing in this module is read back by
the selection layer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Final

from .types import (
    REGIME_A,
    REGIME_B,
    SOURCE_LIVE,
    AccountSnapshot,
    Decision,
    ScoreBreakdown,
    WindowSlack,
)

__all__ = [
    "WINDOW_LABELS",
    "format_duration",
    "format_fraction",
    "format_percent",
    "window_label",
    "explain_window",
    "explain_account",
    "explain_decision",
    "format_ranked_table",
    "explain_verbose",
    "format_pace",
    "format_status_table",
    "format_unmeasurable",
]

#: Window key -> short label. Unknown keys fall through to a readable derivation, so a
#: window this table has never heard of still renders as something meaningful.
WINDOW_LABELS: Final[Mapping[str, str]] = {
    "five_hour": "5h",
    "fivehour": "5h",
    "5h": "5h",
    "seven_day": "7d",
    "sevenday": "7d",
    "7d": "7d",
}


# ======================================================================================
# Formatting primitives
# ======================================================================================


def format_duration(seconds: float) -> str:
    """Compact human duration: ``45s``, ``12m``, ``4.1h``, ``3.2d``."""
    value = max(0.0, float(seconds))
    if value < 90.0:
        return f"{value:.0f}s"
    minutes = value / 60.0
    if minutes < 90.0:
        return f"{minutes:.0f}m"
    hours = minutes / 60.0
    if hours < 48.0:
        return f"{hours:.1f}h"
    return f"{hours / 24.0:.1f}d"


def format_fraction(value: float) -> str:
    """A budget fraction as ``0.44``."""
    return f"{value:.2f}"


def format_percent(value: float, *, digits: int = 0, signed: bool = False) -> str:
    """A budget fraction as a percentage: ``0.48`` -> ``48%``."""
    scaled = value * 100.0
    sign = "+" if signed and scaled > 0 else ""
    return f"{sign}{scaled:.{digits}f}%"


def window_label(key: str | None) -> str:
    """Short label for a window key (``"scoped:fable"`` -> ``"fable"``)."""
    if not key:
        return "no window"
    normalized = key.strip().casefold()
    if normalized in WINDOW_LABELS:
        return WINDOW_LABELS[normalized]
    if normalized.startswith("scoped:"):
        return normalized.split(":", 1)[1] or "scoped"
    return key


# ======================================================================================
# Window / account fragments
# ======================================================================================


def explain_window(row: WindowSlack, *, regime: str | None = None) -> str:
    """The parenthesised arithmetic for one window.

    Regime A shows the slack identity (that is the objective being maximized). Regime B
    leads with what is actually left, because under scarcity "who has surplus" is
    answered "nobody" for every candidate and the deficit is only context.
    """
    expected_demand = max(0.0, 1.0 - row.expected_used_fraction)
    reset = f"over {format_duration(row.time_to_reset_s)} to reset"
    if regime == REGIME_B:
        return (
            f"{format_fraction(row.remaining_fraction)} remaining, "
            f"{format_fraction(abs(row.slack))} behind pace {reset}"
        )
    return (
        f"{format_fraction(row.slack)} slack = "
        f"{format_fraction(row.remaining_fraction)} remaining - "
        f"{format_fraction(expected_demand)} expected {reset}"
    )


def _binding_row(breakdown: ScoreBreakdown) -> WindowSlack | None:
    """The binding window row, falling back to a lookup by key."""
    row = breakdown.binding
    if row is not None:
        return row
    for candidate in breakdown.per_window:
        if candidate.key == breakdown.binding_window:
            return candidate
    applicable = [candidate for candidate in breakdown.per_window if candidate.applicable]
    if not applicable:
        return None
    return min(applicable, key=lambda item: item.slack)


def explain_account(breakdown: ScoreBreakdown, *, regime: str | None = None) -> str:
    """``7d binds (0.44 slack = 0.61 remaining - 0.17 expected over 4.1h to reset)``."""
    effective_regime = regime or breakdown.regime
    row = _binding_row(breakdown)
    label = window_label(breakdown.binding_window or (row.key if row else None))
    if row is None:
        if effective_regime == REGIME_B:
            return f"{label} binds ({format_fraction(breakdown.min_remaining or 0.0)} remaining)"
        return f"{label} binds ({format_fraction(breakdown.min_slack)} slack)"
    return f"{label} binds ({explain_window(row, regime=effective_regime)})"


# ======================================================================================
# The one-liner
# ======================================================================================


def _objective(breakdown: ScoreBreakdown, regime: str | None) -> float:
    """The unscaled quantity the regime ranks on -- what a margin is measured against."""
    if regime == REGIME_B:
        return breakdown.min_remaining if breakdown.min_remaining is not None else 0.0
    return breakdown.min_slack


def _runner_up(
    decision: Decision, winner: ScoreBreakdown | None
) -> ScoreBreakdown | None:
    for row in decision.ranked:
        if winner is None or row.account_id != winner.account_id:
            return row
    return None


def explain_decision(
    decision: Decision,
    *,
    margin: float | None = None,
    include_warnings: bool = False,
) -> str:
    """One line saying who won, which window bound it, and by how much.

    Args:
        decision: The decision to render.
        margin: The configured switch margin, so the line can show the comparison that
            was actually made. It is a margin on the **unscaled** objective; rendering it
            against ``score`` would misreport the test that ran.
        include_warnings: Append degradation notes.
    """
    winner = decision.chosen_breakdown
    if winner is None:
        if decision.chosen:
            head = f"{decision.chosen} chosen"
        else:
            head = "no account could serve this call"
        reason = decision.reason or "no eligible candidates"
        line = f"{head}: {reason}"
        return _with_warnings(line, decision, include_warnings)

    regime = decision.regime or winner.regime
    parts: list[str] = []

    verb = "kept (sticky)" if decision.sticky_applied else "won"
    parts.append(f"{winner.account_id} {verb}: {explain_account(winner, regime=regime)}")

    # The objective is expiring quota, measured in PSE. The regime split it used to
    # report is gone: it existed only because the old slack term could go negative,
    # and every term in the PSE objective is non-negative by construction.
    if winner.score > 0.0:
        parts.append("objective: spend the pool with the most quota about to expire")
    else:
        parts.append("no account will waste quota at the assumed rate; earliest deadline first")
    if not winner.fits:
        parts.append("WARNING: no candidate fits; this is the earliest to reset")

    runner = _runner_up(decision, winner)
    if runner is not None:
        delta = _objective(winner, regime) - _objective(runner, regime)
        if decision.sticky_applied:
            # The incumbent won *despite* trailing: report the challenger's lead and why
            # it was not enough.
            comparison = f"{runner.account_id} led by {format_percent(-delta)}"
            if margin is not None:
                comparison += f" < {format_percent(margin)} margin"
            parts.append(comparison)
        elif delta > 0.0:
            comparison = f"beat {runner.account_id} by {format_percent(delta)}"
            if margin is not None:
                comparison += f" > {format_percent(margin)} margin"
            parts.append(comparison)
        else:
            # The winner is *behind* on the raw objective and won on provider weight or
            # capacity. Saying "beat X by -1%" would be true and useless; the margin is
            # not what decided this, so quoting it would misreport the test that ran.
            parts.append(
                f"beat {runner.account_id} on the tier-normalized objective "
                f"({winner.score:.3f} vs {runner.score:.3f} PSE at risk) despite "
                f"{format_percent(-delta)} less by raw percentage -- percentages across "
                f"tiers and windows are not comparable, PSE are"
            )

    line = "; ".join(parts)
    return _with_warnings(line, decision, include_warnings)


def _with_warnings(line: str, decision: Decision, include: bool) -> str:
    if not include or not decision.warnings:
        return line
    return line + "".join(f"\n  ! {warning}" for warning in decision.warnings)


# ======================================================================================
# Tables
# ======================================================================================


def _rows(breakdowns: Iterable[ScoreBreakdown], regime: str | None) -> list[list[str]]:
    rows: list[list[str]] = []
    for index, row in enumerate(breakdowns, start=1):
        rows.append(
            [
                str(index),
                row.account_id,
                f"{row.score:.4f}",
                format_fraction(row.min_slack),
                (
                    "-"
                    if row.min_remaining is None
                    else format_fraction(row.min_remaining)
                ),
                window_label(row.binding_window),
                "yes" if row.fits else "NO",
                row.reason or "",
            ]
        )
    return rows


def _render_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    if not rows:
        return ""
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = [
        "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)).rstrip(),
        "  ".join("-" * widths[index] for index in range(len(headers))),
    ]
    for row in rows:
        lines.append(
            "  ".join(str(cell).ljust(widths[index]) for index, cell in enumerate(row)).rstrip()
        )
    return "\n".join(lines)


def format_ranked_table(decision: Decision) -> str:
    """The ranked candidates as a fixed-width table.

    The ranking is the payload, not just the winner: the TypeScript consumer maps it onto
    a retry order, so a human reading the table is reading the same fallback sequence the
    machine will follow.
    """
    headers = ("#", "account", "score", "min_slack", "min_remain", "binds", "fits", "note")
    return _render_table(headers, _rows(decision.ranked, decision.regime))


def format_excluded(decision: Decision) -> str:
    """Excluded candidates and why they could not win."""
    if not decision.excluded:
        return ""
    rows = [[row.account_id, row.reason or "excluded"] for row in decision.excluded]
    return _render_table(("account", "reason"), rows)


def explain_verbose(
    decision: Decision,
    *,
    margin: float | None = None,
    show_windows: bool = True,
) -> str:
    """Full rendering: headline, ranked table, per-window arithmetic, exclusions."""
    blocks: list[str] = [explain_decision(decision, margin=margin)]

    blocks.append("objective: PSE at risk of expiring before reset (tier-normalized)")

    table = format_ranked_table(decision)
    if table:
        blocks.append(table)

    if show_windows:
        for row in decision.ranked:
            lines = [f"{row.account_id}:"]
            for window in row.per_window:
                mark = "*" if window.binding else " "
                if not window.applicable:
                    lines.append(
                        f"  {mark} {window_label(window.key):<8} {window.reason or 'not applicable'}"
                    )
                    continue
                lines.append(
                    f"  {mark} {window_label(window.key):<8} "
                    f"{explain_window(window, regime=decision.regime)}"
                )
            blocks.append("\n".join(lines))

    excluded = format_excluded(decision)
    if excluded:
        blocks.append("excluded:\n" + excluded)

    if decision.warnings:
        blocks.append("\n".join(f"! {warning}" for warning in decision.warnings))

    return "\n\n".join(block for block in blocks if block)


# ======================================================================================
# The compact status view
# ======================================================================================
#
# `quotapick status` answers one question -- "how much is left where?" -- and it answers
# it in one screen. The slack identity that the decision layer ranks on is exact and
# unreadable at a glance, so this view states the same number as pace: a window with
# positive slack has quota that will expire unless something spends it ("to spare"), and
# a window with negative slack is burning faster than an even burn to reset ("over
# pace"). Nothing is dropped -- `status --verbose` still prints the arithmetic.

#: Sort priority for a window column. Anything unlisted sorts after these, then by label,
#: so a window this table has never seen still lands in a stable place.
_WINDOW_COLUMN_ORDER: Final[Mapping[str, int]] = {"5h": 0, "7d": 1}

#: Cell for a window the account does not publish at all, distinct from ``n/a``, which
#: means the window exists and does not constrain the model class being asked about.
_ABSENT: Final[str] = "-"


def format_pace(slack: float) -> str:
    """One window's slack as pace: ``24% to spare``, ``35% over pace``, ``on pace``.

    ``slack`` is ``remaining - expected demand``, so a positive value is budget that the
    reset will take back unless it is spent, and a negative value is the amount by which
    this window is already ahead of an even burn.
    """
    rounded = round(slack * 100.0)
    if rounded == 0:
        return "on pace"
    if rounded > 0:
        return f"{rounded}% to spare"
    return f"{-rounded}% over pace"


def _pace_cell(slack: float, label: str, label_width: int) -> str:
    """``fable  61% over pace`` -- window, magnitude, direction, columns aligned."""
    rounded = round(slack * 100.0)
    if rounded == 0:
        # Blank where the magnitude would go, so the direction words stay in one column.
        return f"{label:<{label_width}}      on pace"
    direction = "to spare" if rounded > 0 else "over pace"
    return f"{label:<{label_width}} {abs(rounded):>3}% {direction}"


def _source_cell(snapshot: AccountSnapshot, now_s: float) -> str:
    """Where these numbers came from, and how old they are.

    A fresh live read is the boring case and says only ``live``. Everything else names
    the source and its age, because a cached or assumed reading is exactly the kind of
    thing that must not be mistaken for a measurement.
    """
    if not snapshot.available:
        return "UNAVAILABLE"
    age = snapshot.staleness_s(now_s)
    if snapshot.source == SOURCE_LIVE and (age is None or age < 60.0):
        return SOURCE_LIVE
    if age is None:
        return snapshot.source
    return f"{snapshot.source} {format_duration(age)}"


def _window_columns(
    rows_by_account: Mapping[str, Mapping[str, WindowSlack]],
) -> list[str]:
    """Every window label any account publishes, in a stable, readable order."""
    labels = {label for rows in rows_by_account.values() for label in rows}
    return sorted(labels, key=lambda label: (_WINDOW_COLUMN_ORDER.get(label, 2), label))


def _aligned_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    right: Sequence[bool],
) -> str:
    """Fixed-width table with per-column alignment and no separator rule.

    Distinct from :func:`_render_table`, which left-justifies everything and underlines
    the header. This view has numeric columns, and a rule under a six-row table costs a
    line for nothing.
    """
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines: list[str] = []
    for source in (headers, *rows):
        cells = [
            cell.rjust(widths[index]) if right[index] else cell.ljust(widths[index])
            for index, cell in enumerate(source)
        ]
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines)


def format_status_table(
    snapshots: Iterable[AccountSnapshot],
    now_s: float,
    model_class: str | None = None,
) -> str:
    """One line per measurable account: what is left in each window, and its pace.

    An account with no windows at all is not in this table -- there is nothing to put in
    the cells, and a row of dashes would read as "measured, and empty". Those accounts
    are named by :func:`format_unmeasurable` instead.
    """
    measurable = [snapshot for snapshot in snapshots if snapshot.windows]
    if not measurable:
        return ""

    per_account: dict[str, dict[str, WindowSlack]] = {}
    for snapshot in measurable:
        per_account[snapshot.id] = {
            window_label(row.key): row for row in snapshot.slacks(now_s, model_class)
        }
    columns = _window_columns(per_account)
    label_width = max((len(label) for label in columns), default=0)

    rows: list[list[str]] = []
    for snapshot in measurable:
        rows_by_label = per_account[snapshot.id]
        cells = [snapshot.id, snapshot.tier]
        for label in columns:
            row = rows_by_label.get(label)
            if row is None:
                cells.append(f"{_ABSENT:>3}")
            elif not row.applicable:
                cells.append("n/a")
            else:
                reset = (
                    "now"
                    if row.time_to_reset_s <= 0.0
                    else format_duration(row.time_to_reset_s)
                )
                cells.append(f"{round(row.remaining_fraction * 100.0):>3}% {reset}")
        binding = next(
            (row for row in rows_by_label.values() if row.binding and row.applicable),
            None,
        )
        cells.append(
            ""
            if binding is None
            else _pace_cell(binding.slack, window_label(binding.key), label_width)
        )
        cells.append(_source_cell(snapshot, now_s))
        rows.append(cells)

    headers = ["ACCOUNT", "TIER", *columns, "TIGHTEST", "SOURCE"]
    right = [False, False, *(False for _ in columns), False, False]
    return _aligned_table(headers, rows, right)


def format_unmeasurable(snapshots: Iterable[AccountSnapshot]) -> str:
    """The accounts that published no windows, named rather than silently omitted.

    Leaving them out entirely would make a provider with no usage API indistinguishable
    from one that is not configured. Each is listed with the reason its adapter gave.
    """
    lines: list[str] = []
    for snapshot in snapshots:
        if snapshot.windows:
            continue
        reason = snapshot.note or "no usage windows published"
        lines.append(f"{snapshot.id} ({reason})")
    if not lines:
        return ""
    return "not measurable: " + "\n                ".join(lines)
