"""``quotapick doctor`` -- check that this machine's routing is actually in force.

Every check here exists because the thing it checks has failed silently. A router
that is merely *installed* proves nothing: the failures this command is for all
produce a well-formed answer that names the wrong account.

WHAT MAKES A CHECK BELONG HERE
------------------------------
Three properties. The failure is silent, so nothing else reports it. It is
machine state rather than a code defect, so no unit test can see it. And it
changes which account gets spent, so it costs the operator quota.

THE SKEW PROBLEM
----------------
The operator's policy lives in ONE file -- ``manual_rate_per_day`` in
``config.toml``. The code that reads it does not: every consumer carries its own
copy of this package, pinned or vendored in its own environment. A copy older
than the feature does not reject the key, it IGNORES it, and then routes onto an
account the operator reserved for their own interactive use.

Measured on 2026-09-20, six days after the reserve shipped: of four consumer
environments on this machine, two were behind it. One was a nightly job making
~2000 model calls a run.

So :func:`check_consumers` asks each environment what it actually has, rather
than trusting a version string. The version here is static across many commits
and cannot answer the question.

RUNNING OTHER INTERPRETERS
--------------------------
``doctor`` with no arguments touches only this process and the operator's own
config. Checking another environment means executing its interpreter, so it is
opt-in: name one with ``--consumer``, or a tree to search with ``--scan``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

__all__ = [
    "REQUIRED_FEATURES",
    "Check",
    "ConsumerReport",
    "check_consumers",
    "check_reserve_is_enforced",
    "discover_consumer_pythons",
    "format_report",
    "inspect_consumer",
    "probe_source",
    "worst_exit_code",
]

#: Exit codes. ``doctor`` is meant to be wired into a cron or a pre-flight, so a
#: warning and a failure are distinguishable without parsing the text.
EXIT_OK: Final[int] = 0
EXIT_WARN: Final[int] = 1
EXIT_FAIL: Final[int] = 3

#: Submodules a consumer must have for the operator's config to be obeyed, with
#: what silently breaks without each one.
#:
#: Keyed by submodule rather than by version because the version string is static
#: across many commits, so it cannot answer "does this copy honour the reserve".
REQUIRED_FEATURES: Final[Mapping[str, str]] = {
    "reserve": (
        "manual_rate_per_day is ignored, so a pick can name an account whose "
        "weekly pool is reserved for the operator's interactive use"
    ),
}

#: Printed inside the consumer's own interpreter. Kept to the standard library and
#: to one JSON line, because the only thing that crosses the process boundary is
#: that line.
#:
#: The feature list is substituted by REPLACING a token, not by %-formatting: the
#: script contains its own "%s: %s" and %-formatting the whole thing raises
#: "not enough arguments for format string".
_PROBE: Final[str] = r"""
import importlib.util, json
out = {"features": {}, "version": None, "path": None}
try:
    import quota_router
    out["path"] = getattr(quota_router, "__file__", None)
except Exception as exc:
    out["error"] = "%s: %s" % (type(exc).__name__, exc)
    print(json.dumps(out)); raise SystemExit(0)
try:
    import importlib.metadata as md
    out["version"] = md.version("llm-quota-router")
except Exception:
    pass
for name in __FEATURES__:
    try:
        out["features"][name] = importlib.util.find_spec("quota_router." + name) is not None
    except ImportError:
        out["features"][name] = False
print(json.dumps(out))
"""


@dataclass(frozen=True)
class Check:
    """One verdict, with the action that clears it.

    ``remedy`` is not optional for a failure. A check that says something is wrong
    and not what to do about it gets ignored, which is the same as not running.
    """

    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str
    remedy: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "remedy": self.remedy,
        }


@dataclass(frozen=True)
class ConsumerReport:
    """What one environment actually has installed."""

    python: str
    version: str | None = None
    path: str | None = None
    features: Mapping[str, bool] = field(default_factory=dict)
    error: str | None = None

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(
            name for name in REQUIRED_FEATURES if not self.features.get(name, False)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "python": self.python,
            "version": self.version,
            "path": self.path,
            "features": dict(self.features),
            "missing": list(self.missing),
            "error": self.error,
        }


def probe_source(features: Iterable[str] = tuple(REQUIRED_FEATURES)) -> str:
    """The probe script, with the feature list baked in."""
    return _PROBE.replace("__FEATURES__", repr(tuple(features)))


def inspect_consumer(
    python: str | Path,
    *,
    timeout_s: float = 20.0,
    run=subprocess.run,
    features: Iterable[str] = tuple(REQUIRED_FEATURES),
) -> ConsumerReport:
    """Ask ONE interpreter what it has. Never raises.

    A consumer that cannot be probed is reported as an error rather than skipped.
    Skipping would make a broken environment look like a healthy one, which is
    the failure mode this whole command exists to remove.
    """
    target = str(python)
    try:
        completed = run(
            [target, "-c", probe_source(features)],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ConsumerReport(python=target, error=f"probe timed out after {timeout_s:g}s")
    except OSError as exc:
        return ConsumerReport(python=target, error=f"{type(exc).__name__}: {exc}")

    line = (completed.stdout or "").strip().splitlines()
    if not line:
        tail = (completed.stderr or "").strip()[-300:]
        return ConsumerReport(
            python=target,
            error=f"probe printed nothing (exit {completed.returncode}): {tail or 'no stderr'}",
        )
    try:
        payload = json.loads(line[-1])
    except json.JSONDecodeError:
        return ConsumerReport(python=target, error=f"probe output was not JSON: {line[-1][:200]!r}")
    if not isinstance(payload, dict):
        return ConsumerReport(python=target, error=f"probe output was not an object: {payload!r}")

    return ConsumerReport(
        python=target,
        version=payload.get("version"),
        path=payload.get("path"),
        features={k: bool(v) for k, v in (payload.get("features") or {}).items()},
        error=payload.get("error"),
    )


def discover_consumer_pythons(
    roots: Sequence[str | Path], *, max_depth: int = 3
) -> list[str]:
    """Virtualenv interpreters under ``roots``, sorted, de-duplicated.

    Bounded depth on purpose. An unbounded walk of a home directory finds
    thousands of throwaway environments and takes minutes, and a doctor nobody
    waits for is a doctor nobody runs.

    The paths are returned UNRESOLVED, and that is the whole point. A venv's
    ``bin/python`` is a symlink to the base interpreter, so ``Path.resolve()``
    collapses every environment on the machine to the same handful of system
    interpreters -- none of which can import the consumer's ``quota_router``.
    What makes the import work is being invoked THROUGH the venv path, which is
    how the interpreter finds that venv's ``pyvenv.cfg`` and site-packages.
    Resolving here reported "No module named quota_router" for environments that
    had it installed, which reads as "nothing to check".
    """
    found: set[str] = set()
    for root in roots:
        base = Path(root).expanduser()
        if not base.is_dir():
            continue
        for depth in range(max_depth + 1):
            pattern = "/".join(["*"] * depth + [".venv", "bin", "python"])
            for candidate in base.glob(pattern):
                if candidate.is_file():
                    found.add(str(candidate))
    return sorted(found)


def check_consumers(
    reports: Sequence[ConsumerReport],
    *,
    own_version: str | None,
    named: Iterable[str] = (),
) -> list[Check]:
    """One check per probed environment, plus nothing when none were asked for.

    An environment that ``--scan`` found WITHOUT quota_router installed is not a
    finding. Plenty of virtualenvs have no business carrying it. Only an
    environment that HAS it and is too old to obey the config is.

    An environment in ``named`` is different, because the operator said with
    ``--consumer`` that it is a consumer. Skipping it printed "all clear" about an
    interpreter nothing had checked, which is how a mistyped venv path looked.
    """
    named_pythons = {str(python) for python in named}
    checks: list[Check] = []
    for report in reports:
        label = f"consumer[{report.python}]"
        if report.error and not report.features:
            if "No module named" in (report.error or ""):
                if report.python in named_pythons:
                    checks.append(
                        Check(
                            label,
                            "warn",
                            f"cannot import quota_router, so nothing here reads the config: "
                            f"{report.error}",
                            remedy=(
                                "check the path names the consumer's own venv interpreter; "
                                "drop it from --consumer if that project only runs the cl, "
                                "cdx or quotapick binaries"
                            ),
                        )
                    )
                continue
            checks.append(
                Check(label, "warn", f"could not be probed: {report.error}",
                      remedy="run the probe by hand, or drop it from --scan")
            )
            continue
        missing = report.missing
        version = report.version or "unknown"
        if missing:
            why = "; ".join(f"quota_router.{name}: {REQUIRED_FEATURES[name]}" for name in missing)
            checks.append(
                Check(
                    label,
                    "fail",
                    f"version {version} is missing {why}",
                    remedy=(
                        "bump this consumer's llm-quota-router pin and re-sync its "
                        "environment; the key is not rejected by an old copy, it is ignored"
                    ),
                )
            )
            continue
        note = f"version {version}"
        if own_version and report.version and report.version != own_version:
            note += f" (this quotapick is {own_version})"
        checks.append(Check(label, "ok", f"{note}, all required features present"))
    return checks


def check_reserve_is_enforced(
    *,
    reserves: Sequence[object],
    winner: str | None,
) -> list[Check]:
    """A configured reserve must change what routing does, not merely be printed.

    ``reserves`` are the ManualReserve records the pick actually applied. An empty
    list when the config declares a rate means the reserve never reached routing,
    which is exactly the skew failure, seen from the other side.

    The second half is the one that matters: if an account has nothing spendable
    left after its hold, routing must not have chosen it.
    """
    checks: list[Check] = []
    if not reserves:
        return [
            Check(
                "reserve",
                "ok",
                "no account declares manual_rate_per_day, so nothing is held back",
            )
        ]
    for held in reserves:
        account = getattr(held, "account_id", "?")
        spendable = float(getattr(held, "spendable", 0.0))
        reserve = float(getattr(held, "reserve", 0.0))
        remaining = float(getattr(held, "remaining", 0.0))
        detail = (
            f"{remaining:.0%} left - {reserve:.0%} budgeted hold = {spendable:.0%} spendable"
        )
        if spendable <= 0.0 and winner == account:
            checks.append(
                Check(
                    f"reserve[{account}]",
                    "fail",
                    f"{detail}, yet routing still chose it",
                    remedy=(
                        "the hold is being computed and not applied; check that this "
                        "quotapick is the one on PATH and re-run `quotapick status`"
                    ),
                )
            )
            continue
        checks.append(Check(f"reserve[{account}]", "ok", detail))
    return checks


#: How far back a committed capability move is still worth a warning.
CAPABILITY_MOVE_WARN_S: Final[float] = 7 * 24 * 3600.0


def check_capabilities(
    *,
    table: Mapping[str, Any] | None,
    table_error: str | None,
    path: str,
    accounts: Sequence[tuple[str, str]],
    now_s: float,
) -> list[Check]:
    """Is capability routing set up and current? One check per capability.

    Args:
        accounts: ``(account_id, pool)`` for every enabled account that can take part.
            ``pool`` is the provider, or ``antigravity (claude)`` for a Claude-flavour
            Antigravity account, so its Claude label is never read as disagreeing with
            a Gemini pool's Gemini label.

    No table at all means capability routing is not in use, which is not a fault. A
    table that exists but has gone past its TTL means the poller stopped refreshing
    it, and every capability pick is now excluding accounts: that is a failure.
    """
    from . import capability as cap

    remedy_refresh = (
        "run `quotapick capabilities --refresh`; if it keeps going stale, re-run "
        "ops/install-launchd.sh from repo-prod so the usage poller refreshes it, and "
        "read ~/Library/Logs/llm-quota-router/capability-refresh.log"
    )
    if table_error:
        return [Check("capabilities", "fail", table_error, remedy=remedy_refresh)]
    if table is None:
        return [
            Check(
                "capabilities",
                "ok",
                f"not set up (no table at {path}); `pick --capability` would exclude "
                f"every account until it exists",
            )
        ]

    checks: list[Check] = []
    for name in cap.CAPABILITIES:
        resolved: dict[str, Any] = {}
        unresolved: dict[str, str] = {}
        pool_of = dict(accounts)
        for account_id in pool_of:
            result = cap.lookup(table, account_id, name, now_s=now_s, path=Path(path))
            if isinstance(result, cap.ModelChoice):
                resolved[account_id] = result
            else:
                unresolved[account_id] = result.reason

        by_provider: dict[str, dict[str, list[str]]] = {}
        for account_id, choice in resolved.items():
            by_provider.setdefault(pool_of[account_id], {}).setdefault(choice.id, []).append(
                account_id
            )
        detail = "; ".join(
            f"{provider}: "
            + ", ".join(f"{model_id} ({', '.join(ids)})" for model_id, ids in sorted(models.items()))
            for provider, models in sorted(by_provider.items())
        ) or "no account resolves it"

        problems: list[str] = []
        stale = sorted(a for a, why in unresolved.items() if "TTL" in why)
        if stale:
            problems.append(f"past the table's TTL: {', '.join(stale)}")
        for provider, models in sorted(by_provider.items()):
            if len(models) > 1:
                problems.append(f"{provider} accounts disagree ({', '.join(sorted(models))})")
        for account_id, choice in sorted(resolved.items()):
            if choice.source == "codex_models_cache":
                problems.append(f"{account_id} fell back to models_cache.json")
            entry = table.get("accounts", {}).get(account_id, {})
            cap_entry = (entry.get("capabilities") or {}).get(name) or {}
            moved_at = cap_entry.get("moved_at")
            if isinstance(moved_at, (int, float)) and now_s - moved_at <= CAPABILITY_MOVE_WARN_S:
                problems.append(
                    f"{account_id} moved {cap_entry.get('moved_from') or 'unresolved'} -> "
                    f"{choice.id} at {cap._iso(moved_at)}"
                )
            for warning in entry.get("warnings", []) or []:
                if "old" in str(warning):
                    problems.append(str(warning))

        if not resolved:
            status = "fail"
            remedy = remedy_refresh
            reasons = sorted(set(unresolved.values()))[:3]
            detail = f"no enabled account resolves {name}: " + " | ".join(reasons)
        elif stale:
            status, remedy = "fail", remedy_refresh
        elif problems:
            status, remedy = "warn", "informational; `quotapick capabilities` shows the table"
        else:
            status, remedy = "ok", ""
        if problems:
            detail += " -- " + "; ".join(dict.fromkeys(problems))
        checks.append(Check(f"capability[{name}]", status, detail, remedy=remedy))
    return checks


def worst_exit_code(checks: Sequence[Check]) -> int:
    """EXIT_FAIL if anything failed, EXIT_WARN if anything warned, else EXIT_OK."""
    statuses = {check.status for check in checks}
    if "fail" in statuses:
        return EXIT_FAIL
    if "warn" in statuses:
        return EXIT_WARN
    return EXIT_OK


_MARK: Final[Mapping[str, str]] = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL"}


def format_report(checks: Sequence[Check]) -> str:
    """Aligned one line per check, with remedies gathered underneath.

    Remedies go at the bottom rather than inline so the verdict column stays
    readable when every check passes, which is the common case.
    """
    if not checks:
        return "no checks ran\n"
    width = max(len(check.name) for check in checks)
    lines = [f"{_MARK[check.status]}  {check.name.ljust(width)}  {check.detail}" for check in checks]
    remedies = [check for check in checks if check.status != "ok" and check.remedy]
    if remedies:
        lines.append("")
        for check in remedies:
            lines.append(f"  {check.name}: {check.remedy}")
    failed = sum(1 for check in checks if check.status == "fail")
    warned = sum(1 for check in checks if check.status == "warn")
    lines.append("")
    lines.append(
        f"{len(checks)} checks, {failed} failed, {warned} warned"
        if (failed or warned)
        else f"{len(checks)} checks, all clear"
    )
    return "\n".join(lines) + "\n"


def own_version() -> str | None:
    """This process's own package version, or None when it cannot be read."""
    try:
        import importlib.metadata as md

        return md.version("llm-quota-router")
    except Exception:  # noqa: BLE001 -- a missing dist is a fact, not an error here
        return None


def default_python() -> str:
    """The interpreter running this process."""
    return sys.executable
