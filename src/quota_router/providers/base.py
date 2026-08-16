"""Shared plumbing for the provider adapters.

An *adapter* answers one question: "what does the world look like for my provider right
now?" It returns :class:`~quota_router.types.AccountSnapshot` objects and nothing else --
no scoring, no selection, no execution.

Three rules hold for every adapter in this package:

1. **Never raise.** A missing binary, a missing file, malformed JSON, a schema change
   upstream: all of these mean "I have no snapshot for that account", never an exception
   that takes the router down. The router must always be able to answer.
2. **Never spawn implicitly.** Every subprocess call goes through an injectable
   ``runner`` (default :func:`default_runner`, which is :func:`subprocess.run`). Tests
   inject a fake and therefore never spawn a real process.
3. **Never invent data.** A window whose numbers cannot be parsed is dropped, not
   guessed. The one exception is documented clamping of an out-of-range *used* percentage
   toward "fully used", which is the conservative direction (it can only make an account
   look worse, never better).

Purity note: this module is deliberately *not* pure -- it is the layer that touches the
filesystem, the environment and subprocesses, precisely so that
:mod:`quota_router.scoring` and :mod:`quota_router.select` never have to.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..types import WINDOW_KEY_5H as _WINDOW_KEY_5H
from ..types import WINDOW_KEY_7D as _WINDOW_KEY_7D

from ..types import AccountSnapshot, Window

__all__ = [
    # protocol / runner
    "ProviderAdapter",
    "Runner",
    "default_runner",
    "CommandOutcome",
    "run_command",
    # window key vocabulary
    "WINDOW_KEY_5H",
    "WINDOW_KEY_7D",
    "FIVE_HOUR_S",
    "SEVEN_DAY_S",
    # parsing helpers
    "coerce_epoch_seconds",
    "parse_iso8601",
    "parse_timestamp",
    "pct_to_fraction",
    "read_json_file",
    "read_tail_lines",
    "make_window",
    # confidence
    "decay_confidence",
    "DEFAULT_CONFIDENCE_FLOOR",
]


# ======================================================================================
# Window key vocabulary (shared so two sources describing the same window agree)
# ======================================================================================

#: Account-wide rolling 5-hour window.
WINDOW_KEY_5H: str = _WINDOW_KEY_5H
#: Account-wide rolling 7-day window.
WINDOW_KEY_7D: str = _WINDOW_KEY_7D

FIVE_HOUR_S: float = 5 * 3600.0
SEVEN_DAY_S: float = 7 * 86400.0


# ======================================================================================
# Subprocess injection
# ======================================================================================

#: Signature of an injected process runner. It is called exactly like
#: :func:`subprocess.run` with a list ``argv`` and keyword arguments, and must return
#: something with ``returncode`` / ``stdout`` / ``stderr`` attributes.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def default_runner(argv: Sequence[str], **kwargs: Any) -> "subprocess.CompletedProcess[str]":
    """The real runner: :func:`subprocess.run` with ``shell=False``, always.

    Kept as a named function (rather than passing :func:`subprocess.run` directly) so a
    test can assert that the *default* was replaced, and so ``shell=True`` can never be
    smuggled in by a caller.
    """
    kwargs.pop("shell", None)
    return subprocess.run(list(argv), shell=False, **kwargs)  # noqa: S603 - argv is a literal


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """Result of an attempted command, with failure flattened into data.

    ``ok`` means "the process ran and exited 0". Anything else -- binary not found,
    timeout, non-zero exit, OS error -- lands in :attr:`error` as human-readable text and
    is the adapter's cue to produce no snapshot.
    """

    ok: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    error: str | None = None


def run_command(
    argv: Sequence[str],
    *,
    runner: Runner | None = None,
    timeout_s: float = 10.0,
    env: Mapping[str, str] | None = None,
) -> CommandOutcome:
    """Run ``argv`` through ``runner`` and never raise.

    Args:
        argv: Full argument vector. Never a shell string -- adapters build literal
            vectors so there is nothing to quote and nothing to inject.
        runner: Injected process runner; defaults to :func:`default_runner`.
        timeout_s: Wall-clock budget. A timeout is a failed outcome, not an exception.
        env: Environment for the child; ``None`` inherits.
    """
    run = runner if runner is not None else default_runner
    kwargs: dict[str, Any] = {"capture_output": True, "text": True, "timeout": timeout_s}
    if env is not None:
        kwargs["env"] = dict(env)
    try:
        completed = run(list(argv), **kwargs)
    except FileNotFoundError:
        return CommandOutcome(ok=False, error=f"binary not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        return CommandOutcome(ok=False, error=f"timed out after {timeout_s}s: {' '.join(argv)}")
    except (OSError, ValueError) as exc:  # pragma: no cover - defensive
        return CommandOutcome(ok=False, error=f"{type(exc).__name__}: {exc}")

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    code = completed.returncode
    if code != 0:
        return CommandOutcome(
            ok=False,
            stdout=stdout if isinstance(stdout, str) else "",
            stderr=stderr if isinstance(stderr, str) else "",
            returncode=code,
            error=f"exited {code}: {(stderr or stdout or '').strip()[:400]}",
        )
    return CommandOutcome(
        ok=True,
        stdout=stdout if isinstance(stdout, str) else "",
        stderr=stderr if isinstance(stderr, str) else "",
        returncode=code,
    )


# ======================================================================================
# The adapter protocol
# ======================================================================================


@runtime_checkable
class ProviderAdapter(Protocol):
    """What every source of quota truth looks like to the router.

    Implementations are constructed with their injection points (runner, paths, clocks)
    and then asked, once per invocation, for the current picture. ``now_s`` is passed in
    rather than read from the clock so a caller can score a historical moment and so
    tests are deterministic.
    """

    #: Stable, human-readable adapter name (``"claude_oauth"``, ``"codex_sessions"``...).
    #: Surfaced in warnings and ``--json`` diagnostics.
    name: str

    def snapshot(self, now_s: float) -> list[AccountSnapshot]:
        """Return one snapshot per account this adapter can currently speak for.

        An account the adapter cannot describe is simply absent from the list. This call
        must not raise; degraded states are expressed through the returned snapshots
        (lower ``confidence``, ``available=False``, a populated ``note``) or by returning
        an empty list.
        """
        ...


# ======================================================================================
# Parsing helpers
# ======================================================================================

#: Above this magnitude an epoch-like number is milliseconds, not seconds. Seconds-since-
#: epoch stays under 1e11 until the year 5138; milliseconds passed it in 1973.
_MS_THRESHOLD: float = 1e11


def coerce_epoch_seconds(value: Any) -> float | None:
    """Coerce a numeric epoch to seconds, accepting milliseconds transparently.

    The statusline cache writes ``observedAt`` in milliseconds while its ``resets_at``
    fields are in seconds, so both spellings show up in one document. Returns ``None``
    for anything that is not a finite number (booleans included -- ``True`` is not a
    timestamp).
    """
    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    if abs(number) >= _MS_THRESHOLD:
        number /= 1000.0
    return number


def parse_iso8601(text: Any) -> float | None:
    """Parse an ISO-8601 timestamp to epoch seconds, or ``None``.

    Handles the two spellings the oracles actually emit -- a numeric UTC offset
    (``2026-08-14T19:00:00.035216+00:00``) and a trailing ``Z``
    (``2026-08-14T18:46:52Z``). A timestamp with no offset at all is read as UTC, which
    is what every producer here means; guessing local time would silently shift a reset
    by hours.
    """
    if not isinstance(text, str):
        return None
    raw = text.strip()
    if not raw:
        return None
    if raw.endswith(("Z", "z")):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):  # pragma: no cover - defensive
        return None


def parse_timestamp(value: Any) -> float | None:
    """Epoch seconds from either an ISO-8601 string or a numeric epoch (s or ms)."""
    if isinstance(value, str):
        return parse_iso8601(value)
    return coerce_epoch_seconds(value)


def pct_to_fraction(value: Any) -> float | None:
    """Convert a *used percent* (0-100) to a fraction in ``[0, 1]``.

    Out-of-range input is clamped rather than dropped, and the direction matters: a
    ``pct`` above 100 clamps to ``1.0`` ("fully used"), which can only make the account
    look *worse*. Dropping the window instead would delete a constraint and make the
    account look better than it is -- the exact failure mode
    :class:`~quota_router.types.Window` warns about for an empty ``applies_to``.
    Non-numeric input returns ``None`` so the caller can skip the window.
    """
    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    fraction = number / 100.0
    if fraction < 0.0:
        return 0.0
    if fraction > 1.0:
        return 1.0
    return fraction


def read_json_file(path: Path | str) -> Any | None:
    """Read and parse a JSON file, returning ``None`` on any failure.

    Missing file, permission denied, truncated write, invalid UTF-8, invalid JSON: all
    are "no data", never an exception.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except (OSError, ValueError):
        return None
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, TypeError):
        return None


def read_tail_lines(path: Path | str, *, max_bytes: int = 262_144) -> list[str]:
    """Return the last complete lines of ``path``, reading at most ``max_bytes``.

    Session transcripts are hundreds of megabytes; the rate-limit rows we want are the
    most recent ones. Seeking to the tail keeps this O(max_bytes) per file instead of
    O(file). When the file is larger than the window, the first (necessarily partial)
    line is discarded rather than fed to a JSON parser.
    """
    if max_bytes <= 0:
        return []
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - max_bytes)
            handle.seek(start)
            raw = handle.read()
    except (OSError, ValueError):
        return []
    if not raw:
        return []
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    return lines


def make_window(
    *,
    key: str,
    used_fraction: float | None,
    length_s: float,
    resets_at_s: float | None,
    observed_at_s: float,
    applies_to: frozenset[str] | None = None,
    expected_used_fraction: float | None = None,
) -> Window | None:
    """Build a :class:`~quota_router.types.Window`, or ``None`` if the inputs are unusable.

    :class:`Window` validates hard (that is the point of the contract); an adapter must
    never let that validation escape as an exception, so every construction funnels
    through here.
    """
    if used_fraction is None or resets_at_s is None:
        return None
    try:
        return Window(
            key=key,
            used_fraction=used_fraction,
            length_s=length_s,
            resets_at_s=resets_at_s,
            observed_at_s=observed_at_s,
            applies_to=applies_to,
            expected_used_fraction=expected_used_fraction,
        )
    except (TypeError, ValueError):
        return None


# ======================================================================================
# Confidence
# ======================================================================================

#: Floor a decayed confidence never goes below. Deliberately non-zero: a stale reading is
#: still evidence, and zero is reserved for "no observation exists at all" (antigravity).
DEFAULT_CONFIDENCE_FLOOR: float = 0.2


def decay_confidence(
    age_s: float,
    *,
    fresh_s: float,
    stale_s: float,
    ceiling: float = 1.0,
    floor: float = DEFAULT_CONFIDENCE_FLOOR,
) -> float:
    """Linearly decay confidence from ``ceiling`` to ``floor`` between the two ages.

    ``age_s <= fresh_s`` -> ``ceiling``; ``age_s >= stale_s`` -> ``floor``; linear in
    between. A negative age (the producer's clock is ahead of ours) counts as fresh
    rather than as an error -- clock skew is not a quota signal.
    """
    if not math.isfinite(age_s):
        return floor
    if age_s <= fresh_s:
        return ceiling
    if age_s >= stale_s or stale_s <= fresh_s:
        return floor
    ratio = (age_s - fresh_s) / (stale_s - fresh_s)
    return ceiling - ratio * (ceiling - floor)
