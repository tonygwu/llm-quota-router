"""Append-only usage history: the router's only observability, and its calibration data.

Every time the router fetches a snapshot it appends one JSON line to
``~/.local/state/quota-router/history.jsonl`` (daily-rotated, size-capped).

**This is load-bearing, not a nice-to-have.** The vendor's own statusline cache only
refreshes during *interactive* sessions, so an account that is used exclusively by
headless jobs has no other record that it was ever spent from -- its usage curve is
invisible everywhere else on the machine. This file is where "claude_c quietly ran itself
to 100% at 3am" becomes observable.

It has two consumers:

* **After the fact**: ``quotapick status`` and the operator, reading what actually
  happened to each pool over time.
* **In-line**: when the oracle is unavailable, the most recent record is replayed as a
  *cached* snapshot (``source="cache"``, reduced confidence) so the router still answers.
* **Calibration**: ``quotapick calibrate`` divides recorded picks by observed usage
  deltas to estimate ``calls_per_window`` per account, which sets the pileup reservation
  cost.

Like :mod:`quota_router.state`, nothing here may ever raise into a routing decision: all
IO failures come back as warnings on the result object.
"""

from __future__ import annotations

import math

import json
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from .types import (
    SOURCE_CACHE,
    AccountSnapshot,
    Identity,
    Window,
)

__all__ = [
    "HISTORY_VERSION",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_KEEP_DAYS",
    "HistoryWrite",
    "Calibration",
    "history_path",
    "append_snapshots",
    "iter_records",
    "latest_record",
    "record_to_snapshots",
    "calibrate",
]

#: Record schema version (``v`` on every line).
HISTORY_VERSION: Final[int] = 1

#: Rotate once the live file passes this size (~2 MB of JSON lines).
DEFAULT_MAX_BYTES: Final[int] = 2 * 1024 * 1024

#: Delete rotated files older than this many days.
DEFAULT_KEEP_DAYS: Final[int] = 30

_ROTATED_GLOB: Final[str] = "history-*.jsonl"


# ======================================================================================
# Location
# ======================================================================================


def history_path(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the history file path.

    ``$QUOTA_ROUTER_HISTORY`` wins (a directory means "put ``history.jsonl`` in here"),
    then ``$XDG_STATE_HOME/quota-router``, then ``~/.local/state/quota-router``.
    """
    environ = os.environ if env is None else env

    override = (environ.get("QUOTA_ROUTER_HISTORY") or "").strip()
    if override:
        path = Path(_expand(override, environ))
        return path / "history.jsonl" if path.is_dir() else path

    xdg = (environ.get("XDG_STATE_HOME") or "").strip()
    if xdg:
        base = Path(_expand(xdg, environ))
    else:
        home = environ.get("HOME") or str(Path.home())
        base = Path(home) / ".local" / "state"
    return base / "quota-router" / "history.jsonl"


def _expand(raw: str, env: Mapping[str, str]) -> str:
    text = raw.strip()
    if text == "~" or text.startswith("~/"):
        home = env.get("HOME") or str(Path.home())
        return home + text[1:]
    return text


def _utc_date(epoch_s: float) -> str:
    """``YYYY-MM-DD`` in UTC. UTC on purpose: rotation must not shift with the operator."""
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).strftime("%Y-%m-%d")


def _first_record_day(path: Path) -> str | None:
    """The UTC day of the first record in a history file, or None if unreadable.

    Only the first line is read, so this stays O(1) no matter how large the file is.
    A file whose head is missing, truncated mid-write, or not a record simply yields
    None and the caller falls back to mtime — history is best-effort telemetry and
    must never raise into a routing decision.
    """
    try:
        with path.open("r", encoding="utf-8") as handle:
            first = handle.readline()
    except OSError:
        return None
    if not first.strip():
        return None
    try:
        stamp = json.loads(first).get("t")
    except (ValueError, AttributeError):
        return None
    if not isinstance(stamp, (int, float)):
        return None
    return _utc_date(float(stamp))


def _iso(epoch_s: float) -> str:
    """RFC 3339 timestamp in UTC."""
    return (
        datetime.fromtimestamp(epoch_s, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


# ======================================================================================
# Writing
# ======================================================================================


@dataclass(frozen=True, slots=True)
class HistoryWrite:
    """Outcome of an append. Best-effort: failures are warnings, never exceptions."""

    ok: bool = False
    path: Path | None = None
    rotated_to: Path | None = None
    pruned: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "pruned", tuple(self.pruned))
        object.__setattr__(self, "warnings", tuple(self.warnings))


def _day_from_rotated_name(name: str) -> str | None:
    """``history-2026-08-14.jsonl`` (or ``…-2.jsonl``) -> ``2026-08-14``.

    Returns None for any name that does not carry a well-formed date, so the caller
    can fall back to mtime rather than guess at a file it does not recognize.
    """
    stem = name[len("history-") :].removesuffix(".jsonl") if name.startswith("history-") else ""
    day = stem[:10]
    if len(day) != 10 or day[4] != "-" or day[7] != "-":
        return None
    if not (day[:4].isdigit() and day[5:7].isdigit() and day[8:10].isdigit()):
        return None
    return day


def _rotation_target(path: Path, day: str) -> Path:
    """``history-<day>.jsonl``, disambiguated when that name is taken."""
    candidate = path.with_name(f"history-{day}.jsonl")
    index = 1
    while candidate.exists():
        candidate = path.with_name(f"history-{day}.{index}.jsonl")
        index += 1
    return candidate


def _maybe_rotate(
    path: Path, now_s: float, max_bytes: int, warnings: list[str]
) -> Path | None:
    """Rotate on a day boundary or a size cap; returns the rotated-to path, if any."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        warnings.append(f"cannot stat history file {path}: {exc}")
        return None

    # Rotate on the day of the DATA, not the day the file happened to be touched.
    # Filesystem mtime is wall-clock; the records carry their own logical timestamp.
    # Keying off mtime makes rotation depend on when the process ran rather than on
    # what it recorded, so a caller supplying its own clock (a replay, a backfill, a
    # test) rotates correctly only when wall-clock and logical time agree — which is
    # a bug that hides until the two diverge.
    file_day = _first_record_day(path) or _utc_date(stat.st_mtime)

    day_rolled = file_day != _utc_date(now_s)
    too_big = max_bytes > 0 and stat.st_size >= max_bytes
    if not (day_rolled or too_big):
        return None

    target = _rotation_target(path, file_day)
    try:
        os.replace(path, target)
    except OSError as exc:
        warnings.append(f"cannot rotate history file {path} -> {target}: {exc}")
        return None
    return target


def _prune(path: Path, now_s: float, keep_days: int, warnings: list[str]) -> list[str]:
    """Delete rotated files older than ``keep_days``."""
    if keep_days <= 0:
        return []
    cutoff = now_s - keep_days * 86400.0
    removed: list[str] = []
    try:
        candidates = sorted(path.parent.glob(_ROTATED_GLOB))
    except OSError as exc:
        warnings.append(f"cannot list rotated history files: {exc}")
        return []
    cutoff_day = _utc_date(cutoff)
    for candidate in candidates:
        try:
            # Prefer the day encoded in the rotated filename over the filesystem
            # mtime, for the same reason rotation does: mtime records when the file
            # was last touched, not which day's data it holds. A file copied,
            # restored from backup, or written under an injected clock has an mtime
            # that says nothing about its contents — and deleting telemetry on that
            # basis is silent data loss.
            day = _day_from_rotated_name(candidate.name)
            if day is not None:
                if day >= cutoff_day:  # lexicographic works on YYYY-MM-DD
                    continue
            elif candidate.stat().st_mtime >= cutoff:
                continue
            candidate.unlink()
            removed.append(candidate.name)
        except OSError as exc:
            warnings.append(f"cannot prune {candidate}: {exc}")
    return removed


def append_snapshots(
    snapshots: Sequence[AccountSnapshot],
    *,
    now_s: float,
    event: str = "pick",
    chosen: str | None = None,
    model_class: str | None = None,
    multiplier: float | None = None,
    cost: float | None = None,
    regime: str | None = None,
    degraded: bool = False,
    path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    keep_days: int = DEFAULT_KEEP_DAYS,
) -> HistoryWrite:
    """Append one record describing this invocation's snapshot and decision.

    Args:
        snapshots: Every account seen this run (not just the winner -- the point is the
            usage curve of the accounts *nobody* is watching).
        now_s: Epoch seconds for the record and for rotation/pruning decisions.
        event: ``"pick"``, ``"status"``, ``"exec"`` ... Free-form; calibration keys off
            ``chosen``, not this, so a dry run or a ``status`` call can record its
            snapshot without being counted as demand.
        chosen: The account this invocation actually spent from, if any. Leave ``None``
            for observation-only runs.
        model_class / multiplier / cost: What was requested and what it reserved; the
            multiplier is what makes calibration exact across cheap and expensive classes.

    Returns:
        A :class:`HistoryWrite`; ``ok=False`` with warnings when anything went wrong. The
        caller logs the warning and carries on -- history never blocks a decision.
    """
    target = Path(path) if path is not None else history_path(env)
    warnings: list[str] = []

    record: dict[str, Any] = {
        "v": HISTORY_VERSION,
        "t": float(now_s),
        "at": _iso(now_s),
        "event": event,
        "chosen": chosen,
        "model_class": model_class,
        "multiplier": multiplier,
        "cost": cost,
        "regime": regime,
        "degraded": bool(degraded),
        "accounts": [snapshot.to_dict() for snapshot in snapshots],
    }

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return HistoryWrite(
            ok=False, path=target, warnings=(f"cannot create history directory: {exc}",)
        )

    rotated = _maybe_rotate(target, now_s, max_bytes, warnings)
    pruned = _prune(target, now_s, keep_days, warnings) if rotated else []

    try:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError as exc:
        warnings.append(f"cannot append to history file {target}: {exc}")
        return HistoryWrite(
            ok=False, path=target, rotated_to=rotated, pruned=pruned, warnings=warnings
        )

    return HistoryWrite(
        ok=True, path=target, rotated_to=rotated, pruned=pruned, warnings=warnings
    )


# ======================================================================================
# Reading
# ======================================================================================


def iter_records(
    path: str | os.PathLike[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    since_s: float | None = None,
    include_rotated: bool = False,
) -> Iterator[dict[str, Any]]:
    """Yield history records oldest-first, skipping anything unparseable.

    A truncated final line (a crash mid-append) is simply skipped: a partial record must
    not stop the router from reading the good ones before it.
    """
    target = Path(path) if path is not None else history_path(env)

    files: list[Path] = []
    if include_rotated:
        try:
            files.extend(sorted(target.parent.glob(_ROTATED_GLOB)))
        except OSError:
            pass
    files.append(target)

    for file_path in files:
        try:
            handle = open(file_path, encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                timestamp = record.get("t")
                if since_s is not None and (
                    not isinstance(timestamp, (int, float)) or timestamp < since_s
                ):
                    continue
                yield record


def latest_record(
    path: str | os.PathLike[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    with_accounts: bool = True,
) -> dict[str, Any] | None:
    """The most recent record, optionally requiring that it carries account data."""
    newest: dict[str, Any] | None = None
    for record in iter_records(path, env=env):
        if with_accounts and not record.get("accounts"):
            continue
        newest = record
    return newest


def _window_from_dict(data: Mapping[str, Any]) -> Window | None:
    try:
        applies_to = data.get("applies_to")
        return Window(
            key=data["key"],
            used_fraction=data["used_fraction"],
            length_s=data["length_s"],
            resets_at_s=data["resets_at_s"],
            observed_at_s=data["observed_at_s"],
            applies_to=frozenset(applies_to) if applies_to else None,
            expected_used_fraction=data.get("expected_used_fraction"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def record_to_snapshots(
    record: Mapping[str, Any],
    *,
    source: str = SOURCE_CACHE,
    confidence: float | None = None,
) -> tuple[AccountSnapshot, ...]:
    """Rebuild :class:`~quota_router.types.AccountSnapshot` objects from a record.

    Used for the oracle-unavailable fallback, so the snapshots come back marked
    ``source="cache"`` with a caller-supplied (lower) confidence rather than pretending to
    be a live reading.
    """
    accounts = record.get("accounts")
    if not isinstance(accounts, list):
        return ()

    out: list[AccountSnapshot] = []
    for entry in accounts:
        if not isinstance(entry, Mapping) or not entry.get("id"):
            continue
        windows = tuple(
            window
            for window in (
                _window_from_dict(item)
                for item in entry.get("windows", [])
                if isinstance(item, Mapping)
            )
            if window is not None
        )
        identity_raw = entry.get("identity")
        identity = None
        if isinstance(identity_raw, Mapping) and identity_raw.get("email"):
            try:
                identity = Identity(
                    email=identity_raw.get("email", ""),
                    organization_uuid=identity_raw.get("organization_uuid", "") or "",
                    organization_name=identity_raw.get("organization_name"),
                    account_number=identity_raw.get("account_number"),
                )
            except (TypeError, ValueError):
                identity = None
        try:
            out.append(
                AccountSnapshot(
                    id=str(entry["id"]),
                    provider=str(entry.get("provider") or ""),
                    windows=windows,
                    tier=entry.get("tier"),
                    source=source,
                    confidence=(
                        confidence if confidence is not None else entry.get("confidence", 1.0)
                    ),
                    available=bool(entry.get("available", True)),
                    note=entry.get("note"),
                    identity=identity,
                )
            )
        except (TypeError, ValueError):
            continue
    return tuple(out)


# ======================================================================================
# Calibration
# ======================================================================================


@dataclass(frozen=True, slots=True)
class Calibration:
    """Estimated ``calls_per_window`` for one account.

    Args:
        account: Account id.
        calls_per_window: How many baseline-class (multiplier 1.0) calls the window holds.
            ``None`` when there is not enough evidence.
        demand: Total multiplier-weighted picks observed.
        consumed: Total window fraction observed burning over the same span.
        samples: Number of usable consecutive-record pairs.
        window_key: Which window the estimate was taken from.
        reason: Why the estimate is ``None``, when it is.
    """

    account: str
    calls_per_window: float | None = None
    demand: float = 0.0
    consumed: float = 0.0
    samples: int = 0
    window_key: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready mapping."""
        return {
            "account": self.account,
            "calls_per_window": self.calls_per_window,
            "demand": self.demand,
            "consumed": self.consumed,
            "samples": self.samples,
            "window_key": self.window_key,
            "reason": self.reason,
        }


def _account_windows(record: Mapping[str, Any]) -> dict[str, dict[str, Mapping[str, Any]]]:
    """``{account_id: {window_key: window_dict}}`` for one record."""
    out: dict[str, dict[str, Mapping[str, Any]]] = {}
    accounts = record.get("accounts")
    if not isinstance(accounts, list):
        return out
    for entry in accounts:
        if not isinstance(entry, Mapping):
            continue
        account_id = entry.get("id")
        if not isinstance(account_id, str):
            continue
        windows = {
            str(window.get("key")): window
            for window in entry.get("windows", [])
            if isinstance(window, Mapping) and window.get("key")
        }
        if windows:
            out[account_id] = windows
    return out


def calibrate(
    records: Iterable[Mapping[str, Any]],
    *,
    window_key: str = "five_hour",
    min_samples: int = 3,
    min_demand: float = 3.0,
    min_consumed: float = 0.02,
) -> dict[str, Calibration]:
    """Estimate ``calls_per_window`` per account from recorded picks vs observed burn.

    The estimator is deliberately simple and explainable::

        calls_per_window = (multiplier-weighted picks) / (observed fraction consumed)

    walking consecutive records and skipping any pair where the window reset (a drop in
    used fraction, or a changed reset timestamp) so a rollover is never read as negative
    demand.

    It is biased *low* on purpose. The operator also spends these accounts interactively,
    and that burn lands in the denominator without a matching pick in the numerator -- so
    the estimate under-counts how many calls fit, the derived reservation cost is a little
    high, and concurrent callers spread out slightly more than strictly necessary. That is
    the safe direction: an over-eager reservation wastes a few seconds of routing
    preference, an under-eager one lets a pileup happen.

    Args:
        records: History records, oldest first (see :func:`iter_records`).
        window_key: Window to measure. The 5-hour window is the right default: it turns
            over often enough to produce many samples.
        min_samples / min_demand / min_consumed: Evidence floors below which the estimate
            is reported as ``None`` with a reason rather than as a confident number.
    """
    demand: dict[str, float] = {}
    consumed: dict[str, float] = {}
    samples: dict[str, int] = {}
    previous: dict[str, Mapping[str, Any]] = {}
    seen_accounts: set[str] = set()

    for record in records:
        windows_by_account = _account_windows(record)
        seen_accounts.update(windows_by_account)

        for account_id, windows in windows_by_account.items():
            current = windows.get(window_key)
            if current is None:
                continue
            prior = previous.get(account_id)
            previous[account_id] = current
            if prior is None:
                continue

            try:
                delta = float(current["used_fraction"]) - float(prior["used_fraction"])
            except (KeyError, TypeError, ValueError):
                continue
            if delta <= 0.0:
                continue  # window rolled over (or nothing happened)
            if current.get("resets_at_s") != prior.get("resets_at_s"):
                continue  # a new window: the delta spans a reset and is meaningless

            consumed[account_id] = consumed.get(account_id, 0.0) + delta
            samples[account_id] = samples.get(account_id, 0) + 1

        # Attribute this record's own pick to the account it was routed to. Ordering is
        # deliberate: a pick recorded *with* a snapshot burns budget that shows up in the
        # NEXT record's delta, which is exactly the pair we just measured.
        #
        # The signal is a populated ``chosen``, not ``event == "pick"``: an ``exec`` run
        # spends exactly as much as a ``pick`` does, while a dry run or a ``status`` call
        # records the snapshot with ``chosen = null`` and must not inflate demand.
        chosen = record.get("chosen")
        if isinstance(chosen, str) and chosen:
            weight = record.get("multiplier")
            if not isinstance(weight, (int, float)) or isinstance(weight, bool):
                weight = 1.0
            demand[chosen] = demand.get(chosen, 0.0) + float(weight)
            seen_accounts.add(chosen)

    out: dict[str, Calibration] = {}
    for account_id in sorted(seen_accounts):
        account_demand = demand.get(account_id, 0.0)
        account_consumed = consumed.get(account_id, 0.0)
        account_samples = samples.get(account_id, 0)

        reason = ""
        if account_samples < min_samples:
            reason = (
                f"only {account_samples} usable sample(s) of window {window_key!r}; "
                f"need {min_samples}"
            )
        elif account_demand < min_demand:
            reason = (
                f"only {account_demand:g} weighted pick(s) recorded; need {min_demand:g}"
            )
        elif account_consumed < min_consumed:
            reason = (
                f"only {account_consumed:.4f} of the window observed burning; "
                f"need {min_consumed:g}"
            )

        out[account_id] = Calibration(
            account=account_id,
            calls_per_window=(
                None if reason else round(account_demand / account_consumed, 2)
            ),
            demand=account_demand,
            consumed=account_consumed,
            samples=account_samples,
            window_key=window_key,
            reason=reason,
        )
    return out


# ======================================================================================
# k -- the weekly:session capacity ratio
# ======================================================================================

#: A sample gap beyond this fraction of the five-hour window means the bar could have
#: risen and reset unseen, losing that rise permanently. Two samples per window is the
#: floor at which increments are still attributable.
MAX_GAP_FRACTION_OF_WINDOW: Final[float] = 0.4

#: Both bars are integer percentages, so any single reading carries +/-0.5pp. A
#: contiguous run of increments telescopes to (last - first), leaving two endpoints of
#: error per run rather than two per sample.
QUANTUM_PP: Final[float] = 1.0

#: The weekly window CONTAINS the session window, so the weekly budget cannot be
#: smaller than a single session budget. An estimate below this is not a measurement,
#: it is evidence the input was contaminated -- and it must be refused however narrow
#: its interval, because a tight interval around a wrong number is the dangerous case.
MIN_PLAUSIBLE_K: Final[float] = 1.0

#: Below this much observed weekly consumption the quantum dominates and no interval
#: is worth reporting, however dense the sampling.
MIN_WEEKLY_CONSUMED_PP: Final[float] = 5.0

FIVE_HOUR_S: Final[float] = 5 * 3600.0


@dataclass(frozen=True, slots=True)
class WeeklyToSessionEstimate:
    """Estimated ``k`` for one account, with the evidence that produced it.

    ``k`` is ``None`` whenever the evidence cannot support a point estimate. That is
    the important case: both ways this measurement fails bias it LOW and leave no
    outward sign, so answering anyway would be confidently wrong.
    """

    account: str
    k: float | None
    low: float | None
    high: float | None
    session_increment_pp: float
    weekly_consumed_pp: float
    samples: int
    session_resets_seen: int
    max_gap_s: float
    span_s: float
    dropped_pairs: int
    undersampled: bool
    reason: str | None = None


def _account_series(
    records: Iterable[Mapping[str, Any]], account: str
) -> dict[str, list[tuple[float, float | None, float | None]]]:
    """Timestamped (session, weekly) readings for one account, grouped BY SOURCE.

    Sources disagree by a constant offset -- the statusline cache lags the live
    endpoint -- so comparing a reading from one against a reading from the other
    manufactures consumption out of the offset. Only same-source comparisons are
    valid.

    Grouping rather than choosing is what matters here. An earlier version picked
    the source with the most readings and discarded the rest, which was correct
    about the offset and wrong about the cost: an account whose token expires when
    idle alternates between sources, so half its readings were thrown away and the
    survivors were twice as far apart. The estimates stayed accurate but the density
    guard refused all of them, and the account swung between k=4.0 and k=9.4
    depending on the window.

    Each source's own series is internally consistent, so both contribute -- live
    compared to the previous live reading, cache to the previous cache reading,
    never one to the other.

    THE CLOCK IS ``observed_at_s``, NOT ``t``
    ----------------------------------------
    Every row carries two times: ``t``, when this router wrote the row, and
    ``observed_at_s``, when the vendor actually reported those numbers. They are not
    interchangeable. The cache source republishes its last reading on every poll, so
    a bar last truly read fifteen hours ago is re-emitted every thirty minutes under
    a fresh ``t``.

    Keyed on ``t`` that reads as a dense series which does not exist, and the pair
    straddling a cache refresh looks thirty minutes wide when it in fact spans
    eighteen hours and several session resets. The gap guard then accepts it, and
    its weekly movement lands in the denominator with no matching session movement
    -- exactly the one-way bias the guard exists to prevent. Live, one such pair
    pulled an account's k from ~9.8 to 6.2 and made nested calibration windows
    disagree in a way that looked like a plan-tier difference.

    So rows are keyed by observation time, and a row whose observation time does not
    ADVANCE is dropped: it is a copy of a reading already counted, not a new sighting
    of the bar. Rows predating ``observed_at_s`` fall back to ``t``, which is the
    best available answer for them and leaves their behavior unchanged.
    """
    by_source: dict[str, list[tuple[float, float | None, float | None]]] = {}
    for record in records:
        t = record.get("t")
        if not isinstance(t, (int, float)):
            continue
        for snapshot in record.get("accounts") or ():
            if not isinstance(snapshot, Mapping) or snapshot.get("id") != account:
                continue
            windows = {
                w.get("key"): w
                for w in (snapshot.get("windows") or ())
                if isinstance(w, Mapping)
            }
            session_window = windows.get("5h") or {}
            session = session_window.get("used_fraction")
            weekly = (windows.get("7d") or {}).get("used_fraction")
            observed = session_window.get("observed_at_s")
            clock = float(observed) if isinstance(observed, (int, float)) else float(t)
            source = str(snapshot.get("source") or "unknown")
            by_source.setdefault(source, []).append((clock, session, weekly))

    deduped: dict[str, list[tuple[float, float | None, float | None]]] = {}
    for source, rows in by_source.items():
        rows.sort(key=lambda row: row[0])
        kept: list[tuple[float, float | None, float | None]] = []
        for row in rows:
            # Ties and backward steps are both republished history: the reading was
            # already counted when it was new, and counting it again would fabricate
            # sampling density the vendor never gave us.
            if kept and row[0] <= kept[-1][0]:
                continue
            kept.append(row)
        deduped[source] = kept
    return deduped


def _median_step(times: Sequence[float]) -> float:
    """Typical gap between samples, used to bound the one-way sampling loss.

    Taken across ALL sources' readings, because that is the cadence at which the
    bars are actually observed -- which is what determines how much burn can accrue
    unseen before a reset.
    """
    gaps = sorted(b - a for a, b in zip(times, times[1:]))
    return gaps[len(gaps) // 2] if gaps else 0.0


def estimate_weekly_to_session(
    records: Iterable[Mapping[str, Any]],
) -> dict[str, WeeklyToSessionEstimate]:
    """Estimate ``k`` per account: five-hour increments over weekly consumption.

    Both bars measure the *same* work against different denominators, so the ratio
    of how fast they move is the ratio of their capacities. Increments are summed
    rather than differenced end-to-end because the five-hour bar resets and a
    difference across a rollover is meaningless.

    Two guards, because both failure modes are silent and both bias low:

    * a sample gap large relative to the five-hour window means increments may have
      been lost to an unseen reset -- refuse rather than under-report;
    * a thin weekly denominator means the integer quantum dominates -- refuse
      rather than report noise.
    """
    materialized = list(records)
    accounts: list[str] = []
    for record in materialized:
        for snapshot in record.get("accounts") or ():
            if isinstance(snapshot, Mapping):
                account = snapshot.get("id")
                if isinstance(account, str) and account not in accounts:
                    accounts.append(account)

    results: dict[str, WeeklyToSessionEstimate] = {}
    for account in accounts:
        by_source = _account_series(materialized, account)
        session_pp = weekly_pp = 0.0
        resets = 0
        max_gap = 0.0
        pairs = 0
        # Each source is its own contiguous run: the first pair inside a source has
        # no predecessor to telescope against, so every source contributes one
        # endpoint's worth of quantization error.
        session_runs = max(1, len(by_source))
        weekly_runs = max(1, len(by_source))

        gap_limit = MAX_GAP_FRACTION_OF_WINDOW * FIVE_HOUR_S
        dropped = 0
        total_pairs = sum(max(0, len(rows) - 1) for rows in by_source.values())
        all_times = sorted(row[0] for rows in by_source.values() for row in rows)
        # Observation density is a property of the readings, not of any one source.
        # The guard asks how long the bar could have moved UNSEEN -- and a reading
        # from either source is a sighting. Measuring within a source instead
        # reports the spacing of that source alone, which on an account whose
        # readings alternate is double the real cadence and refuses estimates that
        # are perfectly well sampled.
        #
        # The per-pair `gap_limit` below stays within-source, because that is the
        # interval over which THAT series could have lost increments.
        max_gap = max((b - a for a, b in zip(all_times, all_times[1:])), default=0.0)
        pairs_iter = [
            (a, b) for rows in by_source.values() for a, b in zip(rows, rows[1:])
        ]
        for (t0, s0, w0), (t1, s1, w1) in pairs_iter:
            gap = t1 - t0
            if gap > gap_limit:
                # Not evidence: we cannot know what happened inside the gap. Excluding
                # it from numerator AND denominator keeps the ratio unbiased, whereas
                # counting it would attribute a partial rise to a full interval.
                dropped += 1
                session_runs += 1
                weekly_runs += 1
                continue
            if s0 is not None and s1 is not None:
                if s1 >= s0:
                    session_pp += (s1 - s0) * 100.0
                else:
                    resets += 1
                    session_runs += 1
            if w0 is not None and w1 is not None:
                if w1 >= w0:
                    weekly_pp += (w1 - w0) * 100.0
                else:
                    weekly_runs += 1
            pairs += 1

        undersampled = total_pairs > 0 and pairs == 0
        reason: str | None = None
        k = low = high = None

        if total_pairs == 0:
            reason = "no consecutive samples for this account"
        elif undersampled:
            reason = (
                f"every sample gap exceeds {gap_limit / 60:.0f}m; the session bar can "
                f"rise and reset unseen, which loses the rise and biases k low"
            )
        elif weekly_pp < MIN_WEEKLY_CONSUMED_PP:
            reason = (
                f"only {weekly_pp:.1f}pp of weekly consumption observed; below "
                f"{MIN_WEEKLY_CONSUMED_PP:.0f}pp the 1pp quantum dominates the ratio"
            )
        elif session_pp <= 0.0:
            reason = "no session-window increase observed"
        elif session_pp / weekly_pp < MIN_PLAUSIBLE_K:
            reason = (
                f"k={session_pp / weekly_pp:.2f} is physically impossible (< "
                f"{MIN_PLAUSIBLE_K:g}); the weekly window contains the session window, "
                f"so the input is contaminated -- {session_pp:.0f}pp session against "
                f"{weekly_pp:.0f}pp weekly"
            )
        else:
            session_err = QUANTUM_PP * math.sqrt(session_runs)
            weekly_err = QUANTUM_PP * math.sqrt(weekly_runs)
            k = session_pp / weekly_pp
            low = max(0.0, session_pp - session_err) / (weekly_pp + weekly_err)
            high = (session_pp + session_err) / max(1e-9, weekly_pp - weekly_err)

            # Discrete sampling loses burn ONE WAY. Whatever accrues between the last
            # sample of a window and its reset is never observed, so every estimate is
            # biased low by at most one sampling interval per window. It is bounded,
            # one-directional, and does not shrink with more samples -- only with a
            # shorter interval -- so it belongs in the interval rather than being
            # averaged away. Without it a 15-minute cadence reports k ~11.4 against a
            # true 12.0 and the interval confidently excludes the right answer.
            typical_step = _median_step(all_times)
            if typical_step > 0.0:
                lost = min(0.5, typical_step / FIVE_HOUR_S)
                high = high / max(1e-9, 1.0 - lost)

        results[account] = WeeklyToSessionEstimate(
            account=account,
            k=k,
            low=low,
            high=high,
            session_increment_pp=session_pp,
            weekly_consumed_pp=weekly_pp,
            samples=pairs,
            session_resets_seen=resets,
            max_gap_s=max_gap,
            span_s=(all_times[-1] - all_times[0]) if len(all_times) > 1 else 0.0,
            dropped_pairs=dropped,
            undersampled=undersampled,
            reason=reason,
        )
    return results


#: Interval width, relative to the estimate, at which k is worth adopting.
#:
#: Set against what the decision is actually sensitive to, not against how precise
#: the arithmetic could be made. k scales an account's weekly capacity, and the
#: value it replaces is wrong by about 2x -- so an estimate good to +/-15% captures
#: essentially the whole benefit, and the difference between 6.2 and 6.4 is routing
#: noise. One full five-hour window clears 15%; chasing 10% would require spanning
#: a reset for a third significant figure nothing reads.
ADOPTION_RELATIVE_WIDTH: Final[float] = 0.15

#: Increments are lost between the last sample before a reset and the reset itself,
#: so sampling density bounds that one-way loss. At this gap the worst case is ~5%
#: of a window.
ADOPTION_MAX_GAP_S: Final[float] = 900.0


def adoption_ready(
    *, k: float | None, low: float | None, high: float | None, max_gap_s: float
) -> bool:
    """Is this estimate precise enough, and sampled densely enough, to adopt?

    Gates on RELATIVE precision rather than an absolute amount of observed burn.
    An absolute weekly threshold cannot work: one five-hour window can only move
    the weekly bar by 100/k pp -- about 17pp at k=6 and 8pp at k=12 -- so any bar
    above that is unreachable without spanning a reset, and spanning a reset is
    precisely what a clean measurement avoids.
    """
    if k is None or low is None or high is None or k <= 0:
        return False
    if max_gap_s > ADOPTION_MAX_GAP_S:
        return False
    return (high - low) / k <= ADOPTION_RELATIVE_WIDTH
