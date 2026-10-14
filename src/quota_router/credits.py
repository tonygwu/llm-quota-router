"""The API credit ledger: what each credit pool has left, as far as the router knows.

No vendor publishes the remaining promotional balance (2026-10-14: not Anthropic, not
OpenAI, not Google). So the balance is a ledger, ``credits.jsonl`` beside
``history.jsonl``, append-only and never pruned:

``grant``       a new grant: ``usd`` and ``expires_s``.
``checkpoint``  the figure the operator read in the Console: ``remaining_usd`` and,
                optionally, ``expires_s``.
``hold``        a call in flight: ``run_id`` and ``usd``. It stops counting when the same
                ``run_id`` is spent or released, or after an hour.
``spend``       what a finished call cost: ``run_id``, ``usd``.
``release``     a hold given back without spending.
``exhausted``   the vendor refused a call for lack of credit. The pool reads empty until
                the next grant or checkpoint.

The balance starts from the latest grant or checkpoint. With none, the config's grant
applies, labelled ``config`` at confidence 0.5. Past the expiry, a ``monthly`` pool is
assumed to have a new grant for one more calendar month, labelled ``assumed`` at
confidence 0.6, until a checkpoint confirms it. A wrong assumption fails loud on the
first call, with the vendor's exhaustion error, and costs nothing when no card is on file.
After the linked plan ends nothing renews; credits already granted keep their expiry.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from .config import CreditPoolConfig
from .history import history_path

LEDGER_VERSION: Final[int] = 1

#: A hold older than this no longer counts; the call either finished or died.
HOLD_TTL_S: Final[float] = 3600.0

LINE_TYPES: Final[frozenset[str]] = frozenset(
    {"grant", "checkpoint", "hold", "spend", "release", "exhausted"}
)

SOURCE_OPERATOR: Final[str] = "operator"
SOURCE_CONFIG: Final[str] = "config"
SOURCE_ASSUMED: Final[str] = "assumed"


def ledger_path(env: Mapping[str, str] | None = None) -> Path:
    """``$QUOTA_ROUTER_CREDITS`` (a directory means "in here"), else beside history."""
    environ = os.environ if env is None else env
    override = (environ.get("QUOTA_ROUTER_CREDITS") or "").strip()
    if override:
        path = Path(os.path.expanduser(override))
        return path / "credits.jsonl" if path.is_dir() else path
    return history_path(environ).parent / "credits.jsonl"


def append(env: Mapping[str, str] | None, line: Mapping[str, Any]) -> Path:
    """Append one ledger line under an exclusive lock."""
    if line.get("type") not in LINE_TYPES:
        raise ValueError(f"unknown ledger line type {line.get('type')!r}")
    path = ledger_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"v": LEDGER_VERSION, **line}
    with open(path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return path


def read(env: Mapping[str, str] | None = None) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    """Every readable line, in file order, and a note for each one that is not."""
    path = ledger_path(env)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], ()
    lines: list[dict[str, Any]] = []
    problems: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            problems.append(f"{path} line {number} unreadable: {exc}")
            continue
        if (
            not isinstance(record, dict)
            or record.get("type") not in LINE_TYPES
            or not isinstance(record.get("t"), (int, float))
            or not isinstance(record.get("pool"), str)
        ):
            problems.append(f"{path} line {number} is not a ledger line")
            continue
        lines.append(record)
    return lines, tuple(problems)


def add_month(epoch_s: float) -> float:
    """The same UTC day and time one calendar month later, clamped to the month's end."""
    moment = datetime.fromtimestamp(epoch_s, tz=timezone.utc)
    year, month = (moment.year + 1, 1) if moment.month == 12 else (moment.year, moment.month + 1)
    day = moment.day
    while True:
        try:
            return moment.replace(year=year, month=month, day=day).timestamp()
        except ValueError:
            day -= 1  # 31 Jan -> 28/29 Feb; every month has a 28th


@dataclass(frozen=True, slots=True)
class Balance:
    pool: str
    remaining_usd: float
    grant_usd: float
    expires_s: float
    source: str
    confidence: float
    held_usd: float
    exhausted: bool
    expired: bool
    note: str

    @property
    def available(self) -> bool:
        return self.remaining_usd > 0 and not self.exhausted and not self.expired


def _number(line: Mapping[str, Any], key: str) -> float | None:
    value = line.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def balance(
    pool: CreditPoolConfig,
    lines: Iterable[Mapping[str, Any]],
    *,
    now_s: float,
    plan_end_s: float | None = None,
) -> Balance:
    """What ``pool`` has left at ``now_s``, from its ledger lines."""
    mine = sorted(
        (line for line in lines if line.get("pool") == pool.id and float(line["t"]) <= now_s),
        key=lambda line: float(line["t"]),
    )
    base_t = float("-inf")
    remaining = pool.grant_usd
    expires = pool.expires_s
    source, confidence = SOURCE_CONFIG, 0.5
    for line in mine:
        if line["type"] == "grant":
            usd, exp = _number(line, "usd"), _number(line, "expires_s")
            if usd is None:
                continue
            base_t, remaining, source, confidence = float(line["t"]), usd, SOURCE_OPERATOR, 1.0
            expires = exp if exp is not None else expires
        elif line["type"] == "checkpoint":
            usd, exp = _number(line, "remaining_usd"), _number(line, "expires_s")
            if usd is None:
                continue
            base_t, remaining, source, confidence = float(line["t"]), usd, SOURCE_OPERATOR, 1.0
            expires = exp if exp is not None else expires

    expired = False
    while now_s >= expires:
        renews = pool.renewal == "monthly" and (plan_end_s is None or expires < plan_end_s)
        if not renews:
            expired = True
            remaining = 0.0
            break
        base_t, remaining = expires, pool.grant_usd
        expires = add_month(expires)
        source, confidence = SOURCE_ASSUMED, 0.6

    settled: set[str] = set()
    spent = held = 0.0
    exhausted = False
    for line in mine:
        t = float(line["t"])
        if t < base_t:
            continue
        kind = line["type"]
        if kind in ("spend", "release"):
            settled.add(str(line.get("run_id")))
            if kind == "spend":
                spent += _number(line, "usd") or 0.0
        elif kind == "exhausted":
            exhausted = True
    for line in mine:
        t = float(line["t"])
        if (
            line["type"] == "hold"
            and t >= base_t
            and now_s - t < HOLD_TTL_S
            and str(line.get("run_id")) not in settled
        ):
            held += _number(line, "usd") or 0.0

    left = 0.0 if (exhausted or expired) else max(0.0, remaining - spent - held)
    if expired:
        note = "expired; nothing renews after the linked plan ends" if plan_end_s else "expired"
    elif exhausted:
        note = "the vendor refused a call for lack of credit; record a grant or checkpoint"
    elif source == SOURCE_CONFIG:
        note = "no checkpoint yet: run `quotapick credits set` with the Console figure"
    elif source == SOURCE_ASSUMED:
        note = "renewal assumed, not confirmed: run `quotapick credits set`"
    else:
        note = ""
    return Balance(
        pool=pool.id,
        remaining_usd=left,
        grant_usd=pool.grant_usd,
        expires_s=expires,
        source=source,
        confidence=confidence,
        held_usd=held,
        exhausted=exhausted,
        expired=expired,
        note=note,
    )
