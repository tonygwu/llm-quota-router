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

    if regime == REGIME_B:
        parts.append("regime B: nobody has surplus, spending from who can serve it")
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
                f"beat {runner.account_id} on weight x capacity "
                f"({winner.score:.3f} vs {runner.score:.3f} score) despite "
                f"{format_percent(-delta)} less to spend"
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

    if decision.regime:
        blocks.append(
            f"regime {decision.regime} "
            f"({'surplus: spend what would expire' if decision.regime == REGIME_A else 'scarcity: spend what can serve'})"
        )

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
