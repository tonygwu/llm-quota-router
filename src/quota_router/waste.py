"""The measurement this project is judged on: quota that expired unspent at a reset.

The tool exists to reduce **wasted quota** -- subscription budget that evaporates when a
window rolls over, because it does not carry forward. Until this module existed that
number had never once been computed, and every change had been justified mechanically
("the scorer compared incommensurable units") rather than by evidence that the router
wastes less than the dial it replaced.

WHAT IS RECORDED
----------------
Version 3 retains the history's unrounded readings before and after a reset,
including the simultaneous weekly reading for a Fable sub-cap. The immutable
``basis`` records the first writer's conversion assumptions and logged PSE.
When upgrading an occurrence, its earliest row supplies that basis; missing
legacy fields stay unknown. No schema upgrade adopts today's configuration.

The append-only assumption ledger in :mod:`quota_router.calibrations` records
measurements, encodings and beliefs separately. :mod:`quota_router.waste_report`
derives current figures at the reset time, alongside the logged figures. It
never writes a derived report back into either durable file.

``observation_gap_s`` -- how long before the reset the last reading was taken -- is
recorded rather than thresholded. A remainder measured eight minutes out is solid; one
measured three hours out is a guess. Which of those is good enough is a question for
whoever reads the series, and picking a cutoff now would bake one reader's answer in.

THIS FILE IS NEVER ROTATED AND NEVER PRUNED
-------------------------------------------
That is the requirement, not an oversight. Roughly 400 rows a year is a few hundred KB
for the life of the project.

The scar: history pruning trusted a caller-supplied clock, and one ad-hoc probe with a
synthetic ``--now`` five months in the future put the retention cutoff five months in
the future too, so every real file fell behind it and 38 hours of the calibration data
the whole ``k`` measurement rested on was deleted by a single debugging invocation.
**A measurement series that can be silently shortened is worse than none, because it
still renders -- it just answers a smaller question than the one that was asked.**

So there is no retention policy here at all: no rotation, no pruning, no rewriting, and
the file is opened for append or for read and never for anything else. A test enforces
that structurally, because the behavioural tests only cover the paths we thought of.

A MISSED RESET IS NOT A ZERO
----------------------------
The machine sleeps, the poller stops, ``resets_at`` comes back several windows on. Those
resets happened and their remainders are unrecoverable. They are written as rows with
``observed: false`` and no remainder, so a report can say "12 resets observed, 2 missed"
instead of quietly averaging over a hole. Interpolating them would produce exactly the
silent gap this whole measurement exists to avoid.

AN EARLY RESET IS NOT WASTE
---------------------------
Providers sometimes roll every window over ahead of schedule as a one-off. OpenAI did
it to Codex on 2026-09-26, 10-02, 10-07 and 10-11, and both Codex accounts reset inside
the same two-minute poll gap each time. The remainder at that moment is forfeited, but
a full fresh window replaces it days early, and routing cannot plan for a boundary the
provider never published. So every row carries ``kind``:

* ``scheduled`` -- the window ran to the boundary the provider had published. Its
  remainder is the waste this project is judged on.
* ``early`` -- the window rolled over while its published boundary was still more
  than :data:`EARLY_RESET_MIN_S` away. ``reset_at`` is when it actually happened,
  bounded by the readings either side; ``scheduled_reset_at`` is the boundary that
  never came. Its remainder is reported as *forfeited*, outside the waste total.

Version 1 of this file had no ``kind`` and keyed an early reset on the boundary that
never came, so a row could carry a reset time days in the future and an observation
gap of three days for a remainder read two minutes before the reset. It also missed
early resets in the first half of a week entirely, because the boundary then moves by
less than half a window.

VERSIONS, AND CORRECTING A FILE THAT MAY NOT BE REWRITTEN
---------------------------------------------------------
A row's identity is the window that ended: the account, the window key, and the
boundary that window had published (``scheduled_reset_at``, which equals ``reset_at``
for a scheduled reset). A version-1 row has the same identity as the version-2 row
for the same window, even when version 1 misdated it.

The correction is therefore appended, never written over. The writer appends a
version-3 row for every reset that history still covers, once, and
:func:`current_records` keeps the highest version of each identity, preserving the earliest basis. A version-1 row
that history no longer reaches keeps ``kind: None`` and is reported as
*unclassified*: still in the total, and counted, so a reader can see how much of the
total predates early-reset detection.
"""

from __future__ import annotations

import json
import fcntl
import math
import os
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from . import pse
from .pse import Plan, SESSION_LENGTH_S, Stocks
from .types import (
    MODEL_CLASS_FABLE,
    WINDOW_KEY_5H,
    WINDOW_KEY_7D,
    capacity_for_tier,
    normalize_tier,
)

__all__ = [
    "WASTE_VERSION",
    "KIND_SCHEDULED",
    "KIND_EARLY",
    "EARLY_RESET_MIN_S",
    "ResetRecord",
    "Detection",
    "WasteWrite",
    "waste_path",
    "detect_resets",
    "read_records",
    "current_records",
    "append_records",
    "update_from_history",
    "summarize",
]

#: Record schema version (``v`` on every line). Version 2 added ``kind``,
#: ``scheduled_reset_at``, ``first_observed_after_at`` and ``provider``. Version 3
#: separates unrounded raw readings from the immutable original conversion basis.
WASTE_VERSION: Final[int] = 3
LOCK_TIMEOUT_S: Final[float] = 2.0

#: The window ran to the boundary the provider had published.
KIND_SCHEDULED: Final[str] = "scheduled"

#: The window rolled over before its published boundary. See the module docstring.
KIND_EARLY: Final[str] = "early"

#: How far ahead of its published boundary a new window must already be visible for
#: the rollover to count as early. A poll sees a scheduled rollover only after the
#: boundary, so the margin only has to clear clock skew between the provider and this
#: machine, which is seconds. Measured on the operator's fleet from 2026-09-12 to
#: 10-12: no scheduled rollover was seen before its boundary, and every early one was
#: seen at least 19.8 hours before it. One hour sits far from both.
EARLY_RESET_MIN_S: Final[float] = 3600.0

#: A reset instant that moves by less than this is serialization jitter, not movement.
#: Live, the scoped and account-wide weekly rows of one payload disagree by microseconds
#: to about a second around what is plainly a single instant.
RESET_JITTER_S: Final[float] = 60.0

#: How far the boundary must move before it can be a rollover, as a fraction of the
#: window. A replacement window ends one full length after it starts, and it cannot start
#: before the old one ended, so a genuine reset moves the boundary by AT LEAST one window
#: length -- more if the account sat idle before the new window opened.
#:
#: The cheap threshold ("moved forward by more than jitter") is therefore wrong in a way
#: that would be silent and enormous. If any published window turns out to slide rather
#: than tile -- a boundary that advances a little on every poll -- that reading mints a
#: fresh reset on EVERY poll, at ~35,000 rows a year into a file with no retention
#: policy, each one carrying a remainder that never expired. Half a window is the
#: unambiguous line: below it no rollover can have occurred, at or above it nothing else
#: can explain the move.
MIN_RESET_JUMP_FRACTION: Final[float] = 0.5

#: Both bars are published as integer percentages, so a "drop" smaller than half a point
#: is rounding, not consumption running backwards.
USED_QUANTUM: Final[float] = 0.005

#: Enumerating unobserved resets is bounded so that one corrupt ``resets_at`` cannot
#: write an unbounded number of rows into a file that is never pruned. Hitting it is
#: reported with the count that was cut, never silently absorbed.
MAX_MISSED_ENUMERATED: Final[int] = 64

#: Compatibility precision for the derived remaining_fraction field. Version 3
#: separately preserves full-precision used_fraction in raw.before; no new analysis
#: should recover its input by undoing this display rounding.
_ROUND_PLACES: Final[int] = 6


# ======================================================================================
# Location
# ======================================================================================


def waste_path(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the waste-series path -- a sibling of ``history.jsonl``.

    ``$QUOTA_ROUTER_WASTE`` wins (a directory means "put ``waste.jsonl`` in here"), then
    ``$XDG_STATE_HOME/quota-router``, then ``~/.local/state/quota-router``. This is the
    third hand-written copy of one resolver (``state_path``, ``history_path``, here);
    a test asserts the three land in the same directory for the same environment,
    because the requirement is "beside history.jsonl" and drift between copies is the
    only way that stops being true.
    """
    environ = os.environ if env is None else env

    override = (environ.get("QUOTA_ROUTER_WASTE") or "").strip()
    if override:
        path = Path(_expand(override, environ))
        return path / "waste.jsonl" if path.is_dir() else path

    xdg = (environ.get("XDG_STATE_HOME") or "").strip()
    if xdg:
        base = Path(_expand(xdg, environ))
    else:
        home = environ.get("HOME") or str(Path.home())
        base = Path(home) / ".local" / "state"
    return base / "quota-router" / "waste.jsonl"


def _expand(raw: str, env: Mapping[str, str]) -> str:
    """``~`` expansion against the *given* environment (never ``os.environ``)."""
    text = raw.strip()
    if text == "~" or text.startswith("~/"):
        home = env.get("HOME") or str(Path.home())
        return home + text[1:]
    return text


def _iso(epoch_s: float) -> str:
    """RFC 3339 in UTC. UTC on purpose: the series must not shift with the operator.

    Spelled with ``strftime`` rather than ``isoformat().replace("+00:00", "Z")`` -- the
    idiom the rest of the package uses -- so that the append-only guard in
    ``tests/test_waste.py`` can ban the name ``replace`` outright. A guard with an
    exception list is a guard someone edits; this file simply contains no spelling of
    anything that shortens a file.
    """
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ======================================================================================
# The record
# ======================================================================================


@dataclass(frozen=True, slots=True)
class ResetRecord:
    """One window reset of one account, and what it cost.

    Args:
        reset_at_s: The instant the window rolled over. For an early reset this is the
            best bound the readings allow, between ``last_observed_at_s`` and
            ``first_observed_after_s``.
        scheduled_reset_at_s: The boundary the ending window had published. This --
            with the account and window -- is the identity of the occurrence, which is
            what makes appending idempotent: re-running over the same history can never
            double-count. ``None`` only on a version-1 row, whose ``reset_at_s`` was
            always that boundary.
        kind: :data:`KIND_SCHEDULED` or :data:`KIND_EARLY`. ``None`` only on a
            version-1 row, written before early resets were told apart.
        observed: ``True`` when a reading exists from before this reset, so the
            remainder is measured. ``False`` when the reset happened while nothing was
            looking; then every remainder field is ``None``, because there is no honest
            number and a guessed one would be indistinguishable from a measured one.
            A missed reset is inferred at a published boundary, so it is ``scheduled``.
        scoped: ``True`` for a model-scoped sub-cap (Fable). Its PSE is a *slice of the
            same weekly pool* as the account-wide row, so it is reported separately and
            never added into a fleet total.
        observation_gap_s: Seconds between the last reading and the reset. Recorded, not
            thresholded -- see the module docstring.
        k: The weekly:session ratio the PSE figure was computed with, so a later
            recalibration cannot silently reinterpret an older row.
        first_observed_after_s: The first reading that showed the new window.
        provider: The account's provider, so a report can find early resets that
            several accounts of one provider shared.
        version: The schema version the row was written under.
    """

    account: str
    window: str
    reset_at_s: float
    observed: bool
    scoped: bool = False
    remaining_fraction: float | None = None
    wasted_pse: float | None = None
    last_observed_at_s: float | None = None
    observation_gap_s: float | None = None
    tier: str | None = None
    tier_scale: float | None = None
    k: float | None = None
    source: str | None = None
    signals: tuple[str, ...] = ()
    reason: str | None = None
    kind: str | None = KIND_SCHEDULED
    scheduled_reset_at_s: float | None = None
    first_observed_after_s: float | None = None
    provider: str | None = None
    version: int = WASTE_VERSION
    raw: Mapping[str, Any] | None = None
    basis: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "signals", tuple(self.signals))

    @property
    def boundary_s(self) -> float:
        """The boundary the ending window had published."""
        if self.scheduled_reset_at_s is not None:
            return self.scheduled_reset_at_s
        return self.reset_at_s

    @property
    def key(self) -> tuple[str, str, str]:
        """Identity of the occurrence, used for idempotent appends and supersession.

        It names the window that ended, by the boundary that window published, and not
        the instant the rollover happened. The boundary is a value the provider wrote
        down. The instant of an early reset is a bound derived from two polls. And a
        version-1 row, which misdated an early reset at its boundary, must share an
        identity with the version-2 row that corrects it.

        The boundary is rendered to a whole-second ISO string rather than compared as a
        float: the vendor's timestamps arrive with sub-second jitter across payloads,
        and two floats that differ in the seventh decimal are the same reset.
        """
        return (self.account, self.window, _iso(self.boundary_s))

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready mapping (plain types only)."""
        data = {
            "v": self.version,
            "account": self.account,
            "provider": self.provider,
            "window": self.window,
            "scoped": self.scoped,
            "kind": self.kind,
            "reset_at": _iso(self.reset_at_s),
            "reset_at_s": self.reset_at_s,
            "scheduled_reset_at": (
                None if self.scheduled_reset_at_s is None else _iso(self.scheduled_reset_at_s)
            ),
            "scheduled_reset_at_s": self.scheduled_reset_at_s,
            "observed": self.observed,
            "remaining_fraction": self.remaining_fraction,
            "wasted_pse": self.wasted_pse,
            "last_observed_at": (
                None if self.last_observed_at_s is None else _iso(self.last_observed_at_s)
            ),
            "last_observed_at_s": self.last_observed_at_s,
            "first_observed_after_at": (
                None
                if self.first_observed_after_s is None
                else _iso(self.first_observed_after_s)
            ),
            "first_observed_after_s": self.first_observed_after_s,
            "observation_gap_s": self.observation_gap_s,
            "tier": self.tier,
            "tier_scale": self.tier_scale,
            "k": self.k,
            "source": self.source,
            "signals": list(self.signals),
            "reason": self.reason,
        }
        if self.version >= 3:
            for name in ("k", "tier", "tier_scale", "wasted_pse"):
                del data[name]
            data["raw"] = self.raw
            data["basis"] = self.logged_basis()
        return data

    def logged_basis(self) -> dict[str, Any]:
        """The original writer's conversion, never filled from today's config."""
        if self.basis is not None:
            return dict(self.basis)
        return {
            "k": self.k, "tier": self.tier, "window_size": self.tier_scale,
            "window_size_source": None, "fable_fraction": None,
            "wasted_pse": self.wasted_pse, "ledger_ids": None,
            "missing_reasons": {
                "window_size_source": "not recorded by the original writer",
                "fable_fraction": "not recorded by the original writer",
                "ledger_ids": "not recorded by the original writer",
            },
        }


def record_from_dict(data: Mapping[str, Any]) -> ResetRecord | None:
    """Rebuild a record from one line, or ``None`` if the line is not one.

    Unreadable rows are skipped rather than raised on, for the same reason history
    tolerates a truncated final line: a crash mid-append must not hide every good row
    written before it.
    """
    if not isinstance(data, Mapping):
        return None
    account = data.get("account")
    window = data.get("window")
    reset_at_s = data.get("reset_at_s")
    if not isinstance(account, str) or not isinstance(window, str):
        return None
    if not isinstance(reset_at_s, (int, float)) or isinstance(reset_at_s, bool):
        return None

    def _optional_float(name: str) -> float | None:
        value = data.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    def _optional_str(name: str) -> str | None:
        value = data.get(name)
        return value if isinstance(value, str) else None

    # A line with no ``v`` predates the field, and only version 1 ever lacked it.
    raw_version = data.get("v")
    version = (
        raw_version
        if isinstance(raw_version, int) and not isinstance(raw_version, bool)
        else 1
    )
    basis = data.get("basis") if version >= 3 else None
    raw = data.get("raw") if version >= 3 else None
    if version >= 3:
        if version != 3 or not isinstance(basis, Mapping):
            return None
        required = {"k", "tier", "window_size", "window_size_source", "fable_fraction",
                    "wasted_pse", "ledger_ids", "missing_reasons"}
        if not required.issubset(basis):
            return None
        for name in ("k", "window_size", "fable_fraction", "wasted_pse"):
            value = basis[name]
            if value is not None and (isinstance(value, bool) or
                    not isinstance(value, (float, int)) or not math.isfinite(value)):
                return None
            if value is not None and (value < 0 or (name != "wasted_pse" and value == 0)):
                return None
        if basis["k"] is not None and basis["k"] < 1:
            return None
        if basis["fable_fraction"] is not None and basis["fable_fraction"] > 1:
            return None
        if basis["window_size_source"] not in (None, "capacity", "tier"):
            return None
        if basis["tier"] is not None and not isinstance(basis["tier"], str):
            return None
        if basis["ledger_ids"] is not None and not isinstance(basis["ledger_ids"], Mapping):
            return None
        if not isinstance(basis["missing_reasons"], Mapping):
            return None
        if raw is not None and not _valid_raw(raw):
            return None
    signals = data.get("signals")
    return ResetRecord(
        account=account,
        window=window,
        reset_at_s=float(reset_at_s),
        observed=bool(data.get("observed")),
        scoped=bool(data.get("scoped")),
        remaining_fraction=_optional_float("remaining_fraction"),
        wasted_pse=basis["wasted_pse"] if basis is not None else _optional_float("wasted_pse"),
        last_observed_at_s=_optional_float("last_observed_at_s"),
        observation_gap_s=_optional_float("observation_gap_s"),
        tier=basis["tier"] if basis is not None else _optional_str("tier"),
        tier_scale=basis["window_size"] if basis is not None else _optional_float("tier_scale"),
        k=basis["k"] if basis is not None else _optional_float("k"),
        source=_optional_str("source"),
        signals=tuple(s for s in signals if isinstance(s, str))
        if isinstance(signals, Sequence) and not isinstance(signals, str)
        else (),
        reason=_optional_str("reason"),
        kind=_optional_str("kind"),
        scheduled_reset_at_s=_optional_float("scheduled_reset_at_s"),
        first_observed_after_s=_optional_float("first_observed_after_s"),
        provider=_optional_str("provider"),
        version=version,
        basis=basis,
        raw=raw,
    )


def _valid_raw(raw: Any) -> bool:
    if not isinstance(raw, Mapping) or set(raw) != {"before", "after", "weekly_before"}:
        return False
    for reading in raw.values():
        if reading is None:
            continue
        if not isinstance(reading, Mapping) or set(reading) != {
                "key", "used_fraction", "resets_at_s", "observed_at_s", "length_s", "applies_to"}:
            return False
        if not isinstance(reading["key"], str) or not isinstance(reading["applies_to"], list):
            return False
        if any(not isinstance(value, str) for value in reading["applies_to"]):
            return False
        for key in ("used_fraction", "resets_at_s", "observed_at_s", "length_s"):
            value = reading[key]
            if key == "observed_at_s" and value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                return False
        if not 0 <= reading["used_fraction"] <= 1 or reading["length_s"] <= 0:
            return False
    return True


# ======================================================================================
# Reading the history into a per-account series
# ======================================================================================


@dataclass(frozen=True, slots=True)
class _WindowReading:
    key: str
    used: float
    length_s: float
    resets_at_s: float
    applies_to: frozenset[str]
    observed_at_s: float | None = None

    @property
    def scoped(self) -> bool:
        return bool(self.applies_to)

    @property
    def is_stock(self) -> bool:
        """Is this window a pile that expires, or a flow that is replaced?

        The five-hour window refills every five hours, so its rollover loses nothing --
        :mod:`quota_router.pse` is explicit that valuing it as a stock is what makes a
        router chase windows it cannot fill. Recording its rollovers as waste would add
        ~1,700 rows per account per year of a quantity this project does not call waste,
        and would inflate every total by roughly that much noise.
        """
        return self.length_s > SESSION_LENGTH_S


@dataclass(frozen=True, slots=True)
class _Reading:
    observed_at_s: float
    tier: str
    tier_scale: float
    source: str
    provider: str | None
    windows: Mapping[str, _WindowReading]
    window_size_source: str = "tier"


def _window_reading(data: Mapping[str, Any]) -> _WindowReading | None:
    key = data.get("key")
    used = data.get("used_fraction")
    length_s = data.get("length_s")
    resets_at_s = data.get("resets_at_s")
    if not isinstance(key, str):
        return None
    for value in (used, length_s, resets_at_s):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
    applies_to = data.get("applies_to")
    return _WindowReading(
        key=key,
        used=float(used),  # type: ignore[arg-type]
        length_s=float(length_s),  # type: ignore[arg-type]
        resets_at_s=float(resets_at_s),  # type: ignore[arg-type]
        applies_to=frozenset(s for s in applies_to if isinstance(s, str))
        if isinstance(applies_to, Sequence) and not isinstance(applies_to, str)
        else frozenset(),
        observed_at_s=data.get("observed_at_s"),
    )


def _series(records: Iterable[Mapping[str, Any]]) -> dict[str, list[_Reading]]:
    """Per-account readings, oldest first, keyed on **observation** time.

    THE CLOCK IS ``observed_at_s``, NOT ``t``. Every history row carries two: ``t``, when
    the router wrote the row, and ``observed_at_s``, when the vendor actually reported
    those numbers. The cache source republishes its last reading on every poll, so a bar
    last truly read hours ago is re-emitted under a fresh ``t``.

    Keyed on ``t``, a republished pre-reset reading sorts *after* the post-reset one, and
    the pair then reads as the window jumping backwards -- a contradiction the detector
    reports and refuses, so a real reset is exchanged for a spurious complaint about data
    that was fine.

    A reading with no observation time at all is DROPPED rather than falling back to
    ``t``, which is where :func:`quota_router.history._account_series` lands. The two
    modules want different things from that row: the k estimator wants every usable
    reading, while this one is pairing consecutive sightings, and the only rows without
    an observation time are failed usage reads -- windowless snapshots of an account that
    could not be read. Admitting one as a sighting inserts a reading that saw nothing
    between the two that did, and the reset between them stops being detectable at all.
    """
    by_account: dict[str, list[_Reading]] = {}
    for record in records:
        accounts = record.get("accounts")
        if not isinstance(accounts, list):
            continue
        for entry in accounts:
            if not isinstance(entry, Mapping):
                continue
            account_id = entry.get("id")
            if not isinstance(account_id, str) or not account_id:
                continue
            raw_windows = entry.get("windows")
            if not isinstance(raw_windows, list):
                continue
            windows = {}
            for item in raw_windows:
                if not isinstance(item, Mapping):
                    continue
                reading = _window_reading(item)
                if reading is not None:
                    windows[reading.key] = reading
            # A failed usage read is windowless (``available=False`` with a note naming
            # the cause). It is not a reset and not a zero -- it is an absence, and it
            # must not break the chain between the readings either side of it. There is
            # no ``t`` fallback for the same reason: a snapshot that observed nothing has
            # no observation time, and inventing one inserts a blind reading between two
            # sighted ones, which is enough to make the reset between them undetectable.
            observed_candidates = [
                float(item["observed_at_s"])
                for item in raw_windows
                if isinstance(item, Mapping)
                and isinstance(item.get("observed_at_s"), (int, float))
                and not isinstance(item.get("observed_at_s"), bool)
            ]
            if not windows or not observed_candidates:
                continue
            observed_at_s = max(observed_candidates)

            tier = normalize_tier(entry.get("tier"))
            capacity = entry.get("capacity")
            tier_scale = (
                float(capacity)
                if isinstance(capacity, (int, float)) and not isinstance(capacity, bool)
                else capacity_for_tier(tier)
            )
            by_account.setdefault(account_id, []).append(
                _Reading(
                    observed_at_s=observed_at_s,
                    tier=tier,
                    tier_scale=tier_scale,
                    source=str(entry.get("source") or "unknown"),
                    provider=(
                        entry.get("provider")
                        if isinstance(entry.get("provider"), str)
                        else None
                    ),
                    windows=windows,
                    window_size_source="capacity" if capacity is not None else "tier",
                )
            )

    # Sorted, but NOT de-duplicated on the observation time -- which is where the k
    # estimator in ``history`` has to drop republished rows, so the difference is worth
    # stating. That estimator sums increments, so counting one reading twice fabricates
    # sampling density it never had. This one compares neighbours for a change of window,
    # and a reading compared against a copy of itself shows no change; the occurrence is
    # then keyed on the reset instant, so even a duplicated detection collapses to one
    # row. There is no drop here because there is nothing for it to prevent, and an
    # unexercised guard is one nobody notices going wrong.
    for readings in by_account.values():
        readings.sort(key=lambda reading: reading.observed_at_s)
    return by_account


# ======================================================================================
# Detection
# ======================================================================================


@dataclass(frozen=True, slots=True)
class Detection:
    """Resets found in a history, plus the pairs refused and why, folded per window
    (see :func:`_fold_pair_notes`)."""

    records: tuple[ResetRecord, ...] = ()
    warnings: tuple[str, ...] = ()


def _stocks(reading: _Reading, plan: Plan) -> Stocks | None:
    """Absolute stocks for one reading, via the shared PSE conversion.

    ``None`` when the account publishes no weekly window: without it there is no
    denominator to normalize against, and any PSE figure would be invented.
    """
    weekly = reading.windows.get(WINDOW_KEY_7D)
    if weekly is None:
        return None
    session = reading.windows.get(WINDOW_KEY_5H)
    fable = next(
        (w for w in reading.windows.values() if MODEL_CLASS_FABLE in w.applies_to), None
    )
    return Stocks.from_fractions(
        session_used=session.used if session else 0.0,
        weekly_used=weekly.used,
        fable_used=fable.used if fable else 0.0,
        tier_scale=reading.tier_scale,
        session_reset_s=session.resets_at_s if session else weekly.resets_at_s,
        weekly_reset_s=weekly.resets_at_s,
        plan=plan,
    )


def _wasted_for(
    window: _WindowReading, stocks: Stocks | None, plan: Plan, tier_scale: float
) -> tuple[float | None, str | None]:
    """PSE that expired with this window, and why it could not be computed if it could not.

    At the reset instant the horizon is zero, so nothing can be absorbed and the whole
    remainder is lost. That is exactly what :func:`quota_router.pse.wasted_pse` returns
    when ``now_s`` is the reset -- the working rate cannot matter across zero seconds --
    so the objective the router optimizes and the number it is scored on are the same
    function, not two copies that can drift.
    """
    if stocks is None:
        return None, "account published no weekly window; nothing to normalize against"
    if window.key == WINDOW_KEY_7D:
        return (
            pse.wasted_pse(stocks, now_s=stocks.weekly_reset_s, rate_pse_per_hour=0.0),
            None,
        )
    if MODEL_CLASS_FABLE in window.applies_to:
        # Already the coupled minimum: Fable work spends the shared weekly pool too, so
        # the sub-cap can never deliver more than the weekly pool has left.
        #
        # When that minimum binds, ``remaining_fraction`` and ``wasted_pse`` on this row
        # stop being related by ``fraction x k x tier_scale``, and a reader months from
        # now has no way to tell that from an arithmetic bug. So the row says which of
        # the two ceilings produced its number, in the row itself.
        fable_capacity = plan.fable_fraction * plan.weekly_to_session * tier_scale
        uncoupled = max(0.0, 1.0 - window.used) * fable_capacity
        if stocks.fable_remaining < uncoupled - 1e-9:
            return stocks.fable_remaining, (
                f"weekly-coupled: the sub-cap itself had "
                f"{uncoupled:.3f} PSE left but the shared weekly pool it also draws on "
                f"had only {stocks.weekly_remaining:.3f}, so remaining_fraction and "
                f"wasted_pse do not agree on this row and the smaller ceiling is the "
                f"real one"
            )
        return stocks.fable_remaining, None
    return None, (
        f"no calibrated sub-cap fraction for window {window.key!r}; the remainder is "
        f"recorded as a fraction but cannot be converted to PSE"
    )


def _observed_record(
    account_id: str,
    key: str,
    window: _WindowReading,
    before: _Reading,
    after: _Reading,
    stocks: Stocks | None,
    plan: Plan,
    *,
    reset_at_s: float,
    kind: str,
    signals: Sequence[str],
) -> ResetRecord:
    """A reset with a reading on each side, so its remainder is measured."""
    wasted, reason = _wasted_for(window, stocks, plan, before.tier_scale)
    return ResetRecord(
        account=account_id,
        window=key,
        reset_at_s=reset_at_s,
        scheduled_reset_at_s=window.resets_at_s,
        observed=True,
        scoped=window.scoped,
        remaining_fraction=round(max(0.0, 1.0 - window.used), _ROUND_PLACES),
        wasted_pse=wasted,
        last_observed_at_s=before.observed_at_s,
        first_observed_after_s=after.observed_at_s,
        observation_gap_s=max(0.0, reset_at_s - before.observed_at_s),
        tier=before.tier,
        tier_scale=before.tier_scale,
        k=plan.weekly_to_session,
        source=before.source,
        signals=tuple(signals),
        reason=reason,
        kind=kind,
        provider=before.provider,
        raw={
            "before": _raw_window(window),
            "after": _raw_window(after.windows[key]),
            "weekly_before": _raw_window(before.windows.get(WINDOW_KEY_7D)),
        },
        basis=_basis(before, window, plan, wasted),
    )


def _raw_window(window: _WindowReading | None) -> dict[str, Any] | None:
    if window is None:
        return None
    return {"key": window.key, "used_fraction": window.used,
            "resets_at_s": window.resets_at_s, "observed_at_s": window.observed_at_s,
            "length_s": window.length_s, "applies_to": sorted(window.applies_to)}


def _basis(before: _Reading, window: _WindowReading, plan: Plan,
           wasted: float | None) -> dict[str, Any]:
    return {
        "k": plan.weekly_to_session, "tier": before.tier,
        "window_size": before.tier_scale, "window_size_source": before.window_size_source,
        "fable_fraction": plan.fable_fraction if MODEL_CLASS_FABLE in window.applies_to else None,
        "wasted_pse": wasted, "ledger_ids": None,
        "missing_reasons": {"ledger_ids": "no ledger supplied to the writer"},
    }


def readings_by_account(records: Iterable[Mapping[str, Any]]) -> dict[str, list[_Reading]]:
    """Per-account readings, oldest observation first. See :func:`_series`."""
    return _series(records)


#: What happened to one window between two readings.
TRANSITION_BACKWARDS: Final[str] = "backwards"
TRANSITION_SAME: Final[str] = "same"
TRANSITION_ROLLED: Final[str] = "rolled"


def window_transition(
    window: _WindowReading,
    later: _WindowReading,
    before_observed_s: float,
    after_observed_s: float,
) -> str:
    """Did ``window`` roll over before ``later`` was read? The one rule for that.

    :func:`detect_resets` decides resets with it, and the demand forecast decides
    whether a rise is measured from the old value or from zero, so the two can never
    disagree about whether a reset happened.
    """
    jump = later.resets_at_s - window.resets_at_s
    if jump < -RESET_JITTER_S:
        return TRANSITION_BACKWARDS
    dropped = later.used < window.used - USED_QUANTUM
    fresh_start_s = later.resets_at_s - later.length_s
    opened_between = (
        before_observed_s - RESET_JITTER_S
        <= fresh_start_s
        <= after_observed_s + RESET_JITTER_S
    )
    rolled_early_in_week = jump > RESET_JITTER_S and dropped and opened_between
    if jump < MIN_RESET_JUMP_FRACTION * window.length_s and not rolled_early_in_week:
        return TRANSITION_SAME
    return TRANSITION_ROLLED


def detect_resets(
    records: Iterable[Mapping[str, Any]], *, plans: Mapping[str, Plan] | None = None
) -> Detection:
    """Find every window reset the history can attest to.

    A reset shows up two ways -- the used fraction dropping, and ``resets_at`` moving
    forward -- and ``resets_at`` is the one that carries the *instant*, so it is the
    trigger. The drop is recorded as a corroborating signal when it is also present.

    A rollover is recognized two ways. The boundary may move by at least half a window,
    which no drift can do. Or the used fraction may drop while a fresh window opens
    between the two readings: the new boundary sits one window length after a moment
    the two polls bracket. The second form is how an early reset in the first half of a
    week looks. A sliding window opens a fresh window on every poll too, but its used
    fraction never drops, so it never matches.

    Args:
        records: History records (see :func:`quota_router.history.iter_records`).
        plans: Per-account calibrated denominators. Absent, the built-in ``k`` is used
            and stamped on the row, so the row still says which value produced it.
    """
    plans = plans or {}
    found: list[ResetRecord] = []
    # (account/window, kind, when the pair's later reading was taken, text), folded into
    # one warning per window at the end.
    notes: list[tuple[str, str, float, str]] = []

    def note(account_id: str, key: str, kind: str, text: str) -> None:
        notes.append((f"{account_id}/{key}", kind, pair_at_s, text))

    series = _series(records)
    # Share the stronger temporal classification with forgone accounting. The
    # legacy reset reader tolerates incomplete rows, so normalize only its usable
    # observations rather than introducing a stricter parser into this API.
    from .window_state import classify
    state_inputs = []
    for account_id, readings in series.items():
        for reading in readings:
            if reading.provider not in {"claude", "codex", "antigravity"}:
                continue
            windows = [_raw_window(w) for w in reading.windows.values()
                       if w.observed_at_s is not None]
            if not windows:
                continue
            state_inputs.append({"accounts": [{"id": account_id, "provider": reading.provider,
                "tier": reading.tier, "capacity": reading.tier_scale,
                "source": reading.source, "windows": windows}]})
    states = {(account, w["observed_at_s"], w["key"]): r["window_states"][w["key"]]["state"]
              for account, reads in classify(state_inputs).items()
              for r in reads for w in r["windows"]}
    for account_id, readings in sorted(series.items()):
        plan = plans.get(account_id) or Plan()
        for before, after in zip(readings, readings[1:]):
            pair_at_s = after.observed_at_s
            stocks = _stocks(before, plan)
            for key, window in before.windows.items():
                if not window.is_stock:
                    continue
                later = after.windows.get(key)
                if later is None:
                    continue
                if states.get((account_id, window.observed_at_s, key)) in {"waiting", "stale"}:
                    # Nothing ran here. Its elapsed capacity belongs to the
                    # forgone log, including sparse sliding reads and old replicas.
                    continue

                jump = later.resets_at_s - window.resets_at_s
                transition = window_transition(
                    window, later, before.observed_at_s, after.observed_at_s
                )
                if transition == TRANSITION_BACKWARDS:
                    note(account_id, key, "reset time moved backwards",
                        f"{account_id}/{key}: reset time moved backwards "
                        f"{_iso(window.resets_at_s)} -> {_iso(later.resets_at_s)}; "
                        f"refusing to record a reset from data that disagrees with itself"
                    )
                    continue
                dropped = later.used < window.used - USED_QUANTUM
                fresh_start_s = later.resets_at_s - later.length_s
                if transition == TRANSITION_SAME:
                    if dropped:
                        note(account_id, key, "incoherent pair",
                            f"{account_id}/{key}: incoherent pair -- used fell "
                            f"{window.used:.0%} -> {later.used:.0%} but the reset time "
                            f"moved only {jump / window.length_s:.2f} of a window; a "
                            f"window cannot hold less than it did without rolling over, "
                            f"and a rollover moves the boundary by a full length"
                        )
                    elif jump > RESET_JITTER_S:
                        note(account_id, key, "reset time moved forward",
                            f"{account_id}/{key}: reset time moved forward "
                            f"{jump / window.length_s:.2f} of a window without rolling "
                            f"over ({_iso(window.resets_at_s)} -> "
                            f"{_iso(later.resets_at_s)}); recorded as no reset, but a "
                            f"window whose boundary drifts is not the fixed window this "
                            f"measurement assumes"
                        )
                    continue

                signals = ["resets_at_advanced"]
                if dropped:
                    signals.append("used_fraction_dropped")

                if before.observed_at_s > window.resets_at_s + RESET_JITTER_S:
                    # The last reading was taken after its own window had already
                    # closed, so the source was serving a stale value. The reset
                    # happened with nothing current watching, and pairing the stale
                    # value with a zero gap would pass it off as a perfect measurement.
                    found.append(
                        ResetRecord(
                            account=account_id,
                            window=key,
                            reset_at_s=window.resets_at_s,
                            scheduled_reset_at_s=window.resets_at_s,
                            observed=False,
                            scoped=window.scoped,
                            tier=before.tier,
                            tier_scale=before.tier_scale,
                            k=plan.weekly_to_session,
                            source=before.source,
                            signals=tuple(signals),
                            reason=(
                                f"unobserved: the last reading before this reset was "
                                f"taken at {_iso(before.observed_at_s)}, after the "
                                f"window's own boundary at {_iso(window.resets_at_s)}, "
                                f"so its {before.source} source was serving a stale "
                                f"value and the remainder at the reset is unknown"
                            ),
                            kind=KIND_SCHEDULED,
                            provider=before.provider,
                            basis=_basis(before, window, plan, None),
                            raw={"before": _raw_window(window), "after": _raw_window(later),
                                 "weekly_before": _raw_window(before.windows.get(WINDOW_KEY_7D))},
                        )
                    )
                elif window.resets_at_s - after.observed_at_s > EARLY_RESET_MIN_S:
                    # Early: the new window was already visible while the old boundary
                    # was still more than EARLY_RESET_MIN_S away. The fresh window's
                    # own start is the provider's account of the instant; the two polls
                    # bound it, and win when it falls outside them.
                    reset_at_s = min(
                        max(fresh_start_s, before.observed_at_s), after.observed_at_s
                    )
                    found.append(
                        _observed_record(
                            account_id,
                            key,
                            window,
                            before,
                            after,
                            stocks,
                            plan,
                            reset_at_s=reset_at_s,
                            kind=KIND_EARLY,
                            signals=signals,
                        )
                    )
                    # No missed resets to enumerate. The old boundary was still in the
                    # future at the second poll, so the two polls are less than one
                    # window apart and cannot hide a second rollover.
                    continue
                else:
                    found.append(
                        _observed_record(
                            account_id,
                            key,
                            window,
                            before,
                            after,
                            stocks,
                            plan,
                            reset_at_s=window.resets_at_s,
                            kind=KIND_SCHEDULED,
                            signals=signals,
                        )
                    )

                # How many resets fit in the jump. Anchored to ``later`` rather than to
                # ``before``, because ``later.resets_at_s`` is a value the vendor
                # actually published while any instant derived forward from ``before``
                # is arithmetic. Under continuously tiled windows this is exact; if the
                # vendor instead starts a window on first use after an idle stretch it
                # OVER-counts, which is the right direction to be wrong in -- a report
                # that claims more holes than there were never hides one.
                missed = max(0, int(round(jump / window.length_s)) - 1)
                emitted = min(missed, MAX_MISSED_ENUMERATED)
                if missed > emitted:
                    note(account_id, key, "unobserved resets cut",
                        f"{account_id}/{key}: {missed} unobserved resets implied by a "
                        f"jump of {jump / window.length_s:.1f} window lengths; recorded "
                        f"the {emitted} most recent and cut {missed - emitted}"
                    )
                for index in range(emitted, 0, -1):
                    missed_at_s = later.resets_at_s - index * window.length_s
                    found.append(
                        ResetRecord(
                            account=account_id,
                            window=key,
                            reset_at_s=missed_at_s,
                            scheduled_reset_at_s=missed_at_s,
                            observed=False,
                            scoped=window.scoped,
                            tier=before.tier,
                            tier_scale=before.tier_scale,
                            k=plan.weekly_to_session,
                            kind=KIND_SCHEDULED,
                            provider=before.provider,
                            basis=_basis(before, window, plan, None),
                            raw={"before": _raw_window(window), "after": _raw_window(later),
                                 "weekly_before": _raw_window(before.windows.get(WINDOW_KEY_7D))},
                            signals=("resets_at_advanced",),
                            reason=(
                                f"unobserved: the reset time jumped "
                                f"{jump / window.length_s:.1f} window lengths between "
                                f"{_iso(before.observed_at_s)} and "
                                f"{_iso(after.observed_at_s)}, so this reset happened "
                                f"with nothing watching and its remainder is "
                                f"unrecoverable"
                            ),
                        )
                    )

    found.sort(key=lambda row: (row.reset_at_s, row.account, row.window))
    return Detection(records=tuple(found), warnings=_fold_pair_notes(notes))


def _fold_pair_notes(notes: Sequence[tuple[str, str, float, str]]) -> tuple[str, ...]:
    """One warning per account and window: the note itself when there is one, otherwise
    a count by kind, the newest note, and the newest note of each other kind.

    Every ``status`` scans all retained history again, so an unfolded note came back on
    every poll; one record on 2026-10-18 carried 12,203 (BACKLOG #11). Pairs are scanned
    oldest first, so the last note of a window is its newest, and a drift that arrives
    after an earlier scan still shows here. Each example carries the time its pair's
    later reading was taken, and a rare kind keeps its own example, so routine drift
    cannot hide a refused reset behind a bare count.
    """
    grouped: dict[str, list[tuple[str, float, str]]] = {}
    for window, kind, at_s, text in notes:
        grouped.setdefault(window, []).append((kind, at_s, text))
    out: list[str] = []
    for window, items in grouped.items():
        if len(items) == 1:
            out.append(items[0][2])
            continue
        kinds: dict[str, int] = {}
        newest_of: dict[str, tuple[float, str]] = {}
        for kind, at_s, text in items:
            kinds[kind] = kinds.get(kind, 0) + 1
            newest_of[kind] = (at_s, text)
        counts = ", ".join(f"{count} {kind}" for kind, count in kinds.items())

        def example(at_s: float, text: str) -> str:
            return f"(pair read {_iso(at_s)}) {text.removeprefix(f'{window}: ')}"

        newest_kind, newest_at_s, newest_text = items[-1]
        line = (f"{window}: {len(items)} reset-scan notes in retained history ({counts}); "
                f"newest {example(newest_at_s, newest_text)}")
        for kind in kinds:
            if kind != newest_kind:
                line += f"; newest {kind} {example(*newest_of[kind])}"
        out.append(line)
    return tuple(out)


# ======================================================================================
# The file
# ======================================================================================


@dataclass(frozen=True, slots=True)
class WasteWrite:
    """Outcome of an append. Best-effort: failures are warnings, never exceptions.

    The waste series is telemetry about routing, not an input to it, so nothing here may
    raise into a ``status`` run any more than history may raise into a pick.
    """

    ok: bool = False
    path: Path | None = None
    appended: int = 0
    skipped: int = 0
    warnings: tuple[str, ...] = ()
    #: The subset of ``warnings`` that says this append refused or failed. It is about
    #: this run, not a past transition, so ``status`` prints it as a live warning.
    failures: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "failures", tuple(self.failures))


def _iter_lines(path: Path) -> Iterator[str]:
    try:
        handle = open(path, encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return
    except OSError:
        return
    with handle:
        yield from handle


def read_records(
    path: str | os.PathLike[str] | None = None, *, env: Mapping[str, str] | None = None
) -> tuple[tuple[ResetRecord, ...], tuple[str, ...]]:
    """Every record in the series, in file order, plus a count of unreadable lines."""
    target = Path(path) if path is not None else waste_path(env)
    rows: list[ResetRecord] = []
    bad = 0
    for line in _iter_lines(target):
        text = line.strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            bad += 1
            continue
        record = record_from_dict(data)
        if record is None:
            bad += 1
            continue
        rows.append(record)
    warnings = (
        (f"{bad} unreadable line(s) in {target}; the readable rows are unaffected",)
        if bad
        else ()
    )
    return tuple(rows), warnings


def current_records(records: Iterable[ResetRecord]) -> tuple[ResetRecord, ...]:
    """One row per occurrence: the highest schema version's account of it.

    Every line stays on disk. A later version's row for the same window supersedes an
    earlier one only in what is read, which is how a file that may never be rewritten
    is corrected. Rows keep the position of the first line for their occurrence.
    """
    best: dict[tuple[str, str, str], ResetRecord] = {}
    for record in records:
        held = best.get(record.key)
        if held is None or record.version > held.version:
            best[record.key] = record
    return tuple(best.values())


def append_records(
    records: Iterable[ResetRecord],
    *,
    path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> WasteWrite:
    """Append rows the file does not already have, and nothing else.

    Idempotent on :attr:`ResetRecord.key`, the window that ended, so re-running the
    detector over the same history -- or over a longer history that contains it -- can
    never double-count. Only a row of the current version counts as "already have": an
    occurrence recorded by an older writer is appended once more under the current
    version, and :func:`current_records` reads the newer one.

    There is deliberately no ``max_bytes``, no ``keep_days``, and no way to pass a clock
    that could shorten the series. See the module docstring for the incident that made
    that a hard requirement rather than a default.
    """
    target = Path(path) if path is not None else waste_path(env)
    skipped = 0
    appended = 0
    warnings: tuple[str, ...] = ()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as handle:
            # The lock covers read, deduplication and append. Concurrent status and
            # backfill invocations must agree on the first writer's immutable basis.
            deadline = time.monotonic() + LOCK_TIMEOUT_S
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        failure = (f"waste lock timed out after {LOCK_TIMEOUT_S:g}s on {target}; "
                                   "no rows appended",)
                        return WasteWrite(ok=False, path=target, warnings=failure, failures=failure)
                    time.sleep(0.01)
            existing, warnings = read_records(target)
            if warnings:
                # The unreadable-line count names the file; the refusal alone does not.
                failure = warnings + (
                    "refusing to append behind unreadable waste rows; preserve and inspect the file",
                )
                return WasteWrite(ok=False, path=target, warnings=failure, failures=failure)
            earliest: dict[tuple[str, str, str], ResetRecord] = {}
            seen = {record.key for record in existing if record.version >= WASTE_VERSION}
            for old in existing:
                earliest.setdefault(old.key, old)
            with open(target, "rb") as reader:
                reader.seek(0, os.SEEK_END)
                if reader.tell():
                    reader.seek(-1, os.SEEK_END)
                    if reader.read(1) != b"\n":
                        handle.write("\n")
            for record in records:
                if record.key in seen:
                    skipped += 1
                    continue
                payload = record.to_dict()
                old = earliest.get(record.key)
                if record.version >= 3 and old is not None:
                    original = old.logged_basis()
                    original["copied_from_version"] = old.version
                    original["copied_from_occurrence"] = list(old.key)
                    payload["basis"] = original
                handle.write(json.dumps(payload, separators=(",", ":"), allow_nan=False) + "\n")
                seen.add(record.key)
                appended += 1
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        failure = (f"cannot append to waste file {target}: {exc}",)
        return WasteWrite(ok=False, path=target, appended=appended, skipped=skipped,
                          warnings=warnings + failure, failures=failure)
    return WasteWrite(ok=True, path=target, appended=appended, skipped=skipped,
                      warnings=warnings)


def update_from_history(
    records: Iterable[Mapping[str, Any]],
    *,
    plans: Mapping[str, Plan] | None = None,
    path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    now_s: float | None = None,
    ledger: Sequence[Mapping[str, Any]] = (),
) -> WasteWrite:
    """Detect resets in a history and append the ones not already recorded.

    ``now_s`` is accepted and unused on purpose. Every field written here comes out of
    the readings -- observation times, reset instants, published fractions -- so a
    synthetic or wrong clock cannot change what the series says. The last time a
    file-maintenance path trusted a caller-supplied clock it deleted 38 hours of data.
    """
    detection = detect_resets(records, plans=plans)
    annotated = []
    for record in detection.records:
        payload = record.to_dict()
        if ledger:
            from . import calibrations
            payload["basis"]["ledger_ids"] = calibrations.basis_ledger_ids(
                ledger, record.account, record.reset_at_s)
            payload["basis"]["missing_reasons"].pop("ledger_ids", None)
        payload["basis"]["k_source"] = (
            "config" if plans and record.account in plans else "code_default")
        restored = record_from_dict(payload)
        if restored is None:
            raise ValueError("writer generated an invalid v3 reset record")
        annotated.append(restored)
    write = append_records(annotated, path=path, env=env)
    return WasteWrite(
        ok=write.ok,
        path=write.path,
        appended=write.appended,
        skipped=write.skipped,
        warnings=write.warnings + detection.warnings,
        failures=write.failures,
    )


# ======================================================================================
# Reporting
# ======================================================================================


def _blank_bucket() -> dict[str, Any]:
    return {
        "resets": 0,
        "observed": 0,
        "missed": 0,
        "unclassified": 0,
        "wasted_pse": 0.0,
        "max_observation_gap_s": None,
    }


def _blank_early_bucket() -> dict[str, Any]:
    return {"resets": 0, "forfeited_pse": 0.0}


def _absorb(bucket: dict[str, Any], record: ResetRecord) -> None:
    bucket["resets"] += 1
    if record.kind is None:
        bucket["unclassified"] += 1
    if record.observed:
        bucket["observed"] += 1
        if record.wasted_pse is not None:
            bucket["wasted_pse"] += record.wasted_pse
        if record.observation_gap_s is not None:
            current = bucket["max_observation_gap_s"]
            bucket["max_observation_gap_s"] = (
                record.observation_gap_s
                if current is None
                else max(current, record.observation_gap_s)
            )
    else:
        bucket["missed"] += 1


def _absorb_early(bucket: dict[str, Any], record: ResetRecord) -> None:
    bucket["resets"] += 1
    if record.wasted_pse is not None:
        bucket["forfeited_pse"] += record.wasted_pse


def _early_events(records: Sequence[ResetRecord]) -> list[dict[str, Any]]:
    """Early account-wide resets, grouped into events by provider and time.

    Two early resets are one event when they share a provider and the poll gaps that
    bracket them overlap. Several accounts in one event is the signature of a one-off
    reset the provider granted to everyone: the operator's accounts are separate
    subscriptions, and only the provider can move them all at once. One account on its
    own may have had something specific happen to it, or the other accounts may have
    been idle, unread, or not yet set up, so a one-account event claims nothing more.
    """
    rows = sorted(
        (
            record
            for record in records
            if record.kind == KIND_EARLY and not record.scoped
        ),
        key=lambda record: (record.reset_at_s, record.account),
    )
    events: list[dict[str, Any]] = []
    for record in rows:
        low = (
            record.last_observed_at_s
            if record.last_observed_at_s is not None
            else record.reset_at_s
        )
        high = (
            record.first_observed_after_s
            if record.first_observed_after_s is not None
            else record.reset_at_s
        )
        early_by_s = record.boundary_s - record.reset_at_s
        event = next(
            (
                candidate
                for candidate in events
                if candidate["provider"] == record.provider
                and record.provider is not None
                and low <= candidate["_high"] + RESET_JITTER_S
                and high >= candidate["_low"] - RESET_JITTER_S
            ),
            None,
        )
        if event is None:
            event = {
                "provider": record.provider,
                "accounts": [],
                "_low": low,
                "_high": high,
                "early_by_s": [early_by_s, early_by_s],
                "forfeited_pse": 0.0,
            }
            events.append(event)
        if record.account not in event["accounts"]:
            event["accounts"].append(record.account)
        event["_low"] = min(event["_low"], low)
        event["_high"] = max(event["_high"], high)
        event["early_by_s"] = [
            min(event["early_by_s"][0], early_by_s),
            max(event["early_by_s"][1], early_by_s),
        ]
        if record.wasted_pse is not None:
            event["forfeited_pse"] += record.wasted_pse

    return [
        {
            "provider": event["provider"],
            "accounts": sorted(event["accounts"]),
            "shared": len(event["accounts"]) > 1,
            "reset_not_before": _iso(event["_low"]),
            "reset_not_before_s": event["_low"],
            "reset_not_after": _iso(event["_high"]),
            "reset_not_after_s": event["_high"],
            "min_early_by_s": event["early_by_s"][0],
            "max_early_by_s": event["early_by_s"][1],
            "forfeited_pse": event["forfeited_pse"],
        }
        for event in events
    ]


def summarize(
    records: Iterable[ResetRecord], *, ledger: Sequence[Mapping[str, Any]] = (),
    beliefs_as_of: str | None = None, forgone_records=(), ping_events=(),
) -> dict[str, Any]:
    """Derive an auditable report; today's config cannot reinterpret a reset."""
    from .waste_report import summarize as derive
    return derive(records, ledger=ledger, beliefs_as_of=beliefs_as_of,
                  forgone_records=forgone_records, ping_events=ping_events)
