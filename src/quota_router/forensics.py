"""Credential forensics: capture WHY an account goes dark, at the moment it does.

WHAT THIS IS FOR
----------------
Accounts on this machine intermittently lose their stored credential and need an
interactive ``/login``. Two outages are on record in the poller log:

* ``claude_d`` -- 27 hours dark from 2026-08-18 06:15 PDT
* ``claude_b`` --  6.7 hours dark from 2026-08-20 12:22 PDT, plus four brief blips

The cause is not established. Three hypotheses were tested against the existing
data and two of them died:

1. *The poller's own refresh spawn did it.* Ruled out for ``claude_d``. The
   account read healthy at 06:00, blank at 06:15, and the cooldown stamp in the
   06:30 record proves the first spawn of that day happened AT 06:15, after the
   credential was already blank. Nothing of ours ran in the window where the
   damage occurred.
2. *Sheer volume of concurrent readers did it.* Ruled out by the spawn log. The
   default account absorbed 354 headless spawns over five days on top of two
   live sessions and never went dark once in 460 polls, while ``claude_b`` took
   22 and went dark five times.
3. *A config directory was re-pointed at another identity.* Ruled out: every
   slot reported one stable identity across all 460 polls.

What survives is narrower and unproven: the risk may track the number of
concurrently ACTIVE long-lived sessions on one directory, because a short
``claude -p`` against a healthy token performs no renewal at all while a session
that crosses the eight-hour boundary must. That is a theory, and this module
exists because a theory is not a cause.

WHY THE EXISTING POLLER CANNOT ANSWER IT
----------------------------------------
It runs every 900 seconds. Both outages therefore have a fifteen-minute blind
spot around the transition, which is precisely the interval the answer lives in.
This samples every 60 seconds, and on the sample where a credential is lost it
writes down the ten minutes of history leading up to it: what the credential
looked like, and which processes held that directory, at each step.

THE SINGLE-WRITER RULE STILL HOLDS
----------------------------------
This module is a pure reader, like the rest of the package. It reads the Keychain
entry, measures it, and writes its findings to a log of its own. It never mints,
exchanges, repairs, or writes back a credential. See
``tests/test_no_token_rotation.py`` for the structural guard, which scans this
file along with every other module here.

ONE DELIBERATE CONSEQUENCE OF THAT GUARD
----------------------------------------
The guard bans the *name* of the renewal credential from executable code, and
that name is one of the fields whose length is most worth recording: an entry
with a live renewal credential and a blanked access token failed differently
from one where both are gone. So this module never names any field. It measures
the length of every string in the OAuth blob generically and reports them all.
That satisfies the guard honestly rather than by evasion, and it is strictly more
informative than a hand-picked pair would be, because it also captures fields
nobody thought to ask about.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from .providers.base import Runner, run_command
from .providers.claude_oauth import _OAUTH_KEY as OAUTH_CONTAINER_KEY
from .providers.claude_oauth import keychain_service_for

__all__ = [
    "DEFAULT_HISTORY_DEPTH",
    "DEFAULT_SAMPLE_INTERVAL_S",
    "CredentialSample",
    "ProcessHolder",
    "Sample",
    "Transition",
    "classify_entry",
    "detect_transitions",
    "forensics_log_path",
    "forensics_state_path",
    "list_holders",
    "read_credential",
    "run_once",
    "take_sample",
]

#: Sampling cadence. One Keychain read per account plus one ``ps`` call per live
#: session, measured at ~21ms total on this machine. There is no network call, so
#: the cost of a tighter interval is close to nothing while the benefit is direct:
#: the blind spot around a transition is exactly this number. Fifteen minutes was
#: too coarse to see either recorded outage happen.
DEFAULT_SAMPLE_INTERVAL_S: Final[float] = 60.0

#: How many prior samples to carry forward. At the default cadence this is ten
#: minutes of lead-up, which is what gets written into the record when a
#: credential is lost. Kept small on purpose: the state file is rewritten every
#: sample, and the interesting window is the one immediately before the loss.
DEFAULT_HISTORY_DEPTH: Final[int] = 10

#: Credential states, worst to best.
STATE_MISSING: Final[str] = "missing"
STATE_UNREADABLE: Final[str] = "unreadable"
STATE_BLANK: Final[str] = "blank"
STATE_EXPIRED: Final[str] = "expired"
STATE_HEALTHY: Final[str] = "healthy"

#: The states that mean "this account now needs an interactive login". These are
#: the transitions worth a forensic record. ``expired`` is deliberately NOT here:
#: an expired access token is the normal end of an eight-hour life and renews on
#: next use, so treating it as a loss would fire on every account every day.
LOST_STATES: Final[frozenset[str]] = frozenset({STATE_BLANK, STATE_MISSING})

#: Environment variable naming the config directory a Claude Code process holds.
#: A process without it holds the DEFAULT directory, which is why absence is
#: recorded as a real answer rather than as unknown.
_CONFIG_DIR_VAR: Final[str] = "CLAUDE_CONFIG_DIR"

#: Matches a Claude Code CLI process and nothing else. The desktop app ships
#: helper processes whose argv also contains "claude"; they hold no config
#: directory and must not be counted as holders.
_CLAUDE_CMD: Final[re.Pattern[str]] = re.compile(r"(?:^|/)claude(?:\s|$)")

#: Rejected outright regardless of the pattern above.
_NOT_A_CLI: Final[tuple[str, ...]] = ("Claude.app", "Claude Helper", "claudefordesktop")


def _state_root(environ: Mapping[str, str] | None = None, home: Path | str | None = None) -> Path:
    """State directory, following the same XDG rule as the rest of the package."""
    env = os.environ if environ is None else environ
    xdg = (env.get("XDG_STATE_HOME") or "").strip()
    base = Path(xdg) if xdg else Path(os.path.expanduser(str(home) if home else "~")) / ".local" / "state"
    return base / "quota-router"


def forensics_state_path(
    environ: Mapping[str, str] | None = None, home: Path | str | None = None
) -> Path:
    """Where the rolling sample history lives. Rewritten every sample."""
    return _state_root(environ, home) / "credential-forensics.state.json"


def forensics_log_path(
    environ: Mapping[str, str] | None = None, home: Path | str | None = None
) -> Path:
    """Append-only record of transitions. This is the artifact to read after an outage."""
    env = os.environ if environ is None else environ
    override = (env.get("QUOTA_ROUTER_FORENSICS_LOG") or "").strip()
    if override:
        return Path(os.path.expanduser(override))
    root = Path(os.path.expanduser(str(home) if home else "~")) / "Library" / "Logs" / "llm-quota-router"
    return root / "credential-forensics.jsonl"


@dataclass(frozen=True, slots=True)
class ProcessHolder:
    """One live process holding a config directory, as seen at sample time."""

    pid: int
    #: Elapsed run time as ``ps`` reports it, e.g. ``03-01:36:40``. Kept as the
    #: raw string rather than parsed seconds: the question this answers is "was
    #: this the long-lived one?", and the raw form survives a parser bug.
    elapsed: str
    #: Truncated argv. Enough to tell an interactive session from a headless
    #: ``-p`` spawn, which is the distinction the surviving theory turns on.
    argv: str


@dataclass(frozen=True, slots=True)
class CredentialSample:
    """What one account's Keychain entry looked like at one instant."""

    account_id: str
    config_dir: str
    service: str | None
    state: str
    #: Length of every string field in the OAuth blob, by field name. See the
    #: module docstring for why this is generic rather than a named pair.
    field_lengths: dict[str, int] = field(default_factory=dict)
    expires_at: str | None = None
    problem: str | None = None

    @property
    def lost(self) -> bool:
        return self.state in LOST_STATES


@dataclass(frozen=True, slots=True)
class Sample:
    """One full sweep: every account's credential, plus who was holding it."""

    at: str
    at_s: float
    credentials: dict[str, CredentialSample] = field(default_factory=dict)
    #: Config directory -> processes holding it. Keyed by directory rather than
    #: by account so a holder of a directory no account claims is still visible.
    holders: dict[str, list[ProcessHolder]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "at_s": self.at_s,
            "credentials": {k: asdict(v) for k, v in self.credentials.items()},
            "holders": {k: [asdict(p) for p in v] for k, v in self.holders.items()},
        }


@dataclass(frozen=True, slots=True)
class Transition:
    """A credential changed state between two samples."""

    account_id: str
    config_dir: str
    previous_state: str
    current_state: str
    #: Seconds between the two samples. The blind spot the cause is hiding in.
    gap_s: float
    #: True when the account just became unusable. False for a recovery, which is
    #: recorded too: how an account healed is evidence about how it broke.
    lost: bool


def classify_entry(raw: str | None, *, now_s: float | None, expires_at_s: float | None) -> str:
    """Classify a Keychain payload. Never raises; every failure is a state."""
    if raw is None:
        return STATE_MISSING
    text = raw.strip()
    if not text:
        return STATE_MISSING
    try:
        blob = json.loads(text)
    except json.JSONDecodeError:
        return STATE_UNREADABLE
    if not isinstance(blob, Mapping):
        return STATE_UNREADABLE
    oauth = blob.get(OAUTH_CONTAINER_KEY)
    if not isinstance(oauth, Mapping):
        return STATE_UNREADABLE
    access = oauth.get("accessToken")
    if not isinstance(access, str) or not access:
        return STATE_BLANK
    if expires_at_s is not None and now_s is not None and expires_at_s <= now_s:
        return STATE_EXPIRED
    return STATE_HEALTHY


def _field_lengths(raw: str | None) -> dict[str, int]:
    """Length of every string in the OAuth blob, by field name.

    No field is named in this function on purpose. The package's credential guard
    bans the name of the renewal credential from executable code, and that field
    is one of the two whose length matters most here. Measuring everything is both
    guard-compliant and more complete.
    """
    if not raw:
        return {}
    try:
        blob = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(blob, Mapping):
        return {}
    oauth = blob.get(OAUTH_CONTAINER_KEY)
    if not isinstance(oauth, Mapping):
        return {}
    return {str(k): len(v) for k, v in oauth.items() if isinstance(v, str)}


def _expires_at(raw: str | None) -> tuple[float | None, str | None]:
    """Expiry as (epoch seconds, ISO string), or (None, None) if absent."""
    if not raw:
        return None, None
    try:
        blob = json.loads(raw)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(blob, Mapping):
        return None, None
    oauth = blob.get(OAUTH_CONTAINER_KEY)
    if not isinstance(oauth, Mapping):
        return None, None
    value = oauth.get("expiresAt")
    if not isinstance(value, (int, float)):
        return None, None
    seconds = float(value) / 1000.0
    import datetime

    iso = datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat()
    return seconds, iso


def read_credential(
    account_id: str,
    config_dir: Path | str,
    *,
    now_s: float,
    runner: Runner | None = None,
    home: Path | str | None = None,
    timeout_s: float = 10.0,
) -> CredentialSample:
    """Measure one account's Keychain entry. Pure read, never raises."""
    resolved = str(Path(os.path.expanduser(str(config_dir))))
    hard_problem: str | None = None
    for service in keychain_service_for(config_dir, home=home):
        outcome = run_command(
            ["security", "find-generic-password", "-s", service, "-w"],
            runner=runner,
            timeout_s=timeout_s,
        )
        if outcome.returncode is None:
            # The tool never ran: missing binary, timeout, or a Keychain prompt
            # with nobody to answer it. Categorically different from "no entry",
            # and conflating them would report a healthy account as logged out.
            hard_problem = hard_problem or f"could not run `security`: {outcome.error}"
            continue
        if outcome.returncode != 0:
            continue
        raw = outcome.stdout or ""
        if not raw.strip():
            continue
        expires_s, expires_iso = _expires_at(raw)
        state = classify_entry(raw, now_s=now_s, expires_at_s=expires_s)
        return CredentialSample(
            account_id=account_id,
            config_dir=resolved,
            service=service,
            state=state,
            field_lengths=_field_lengths(raw),
            expires_at=expires_iso,
            problem=None,
        )
    return CredentialSample(
        account_id=account_id,
        config_dir=resolved,
        service=None,
        state=STATE_UNREADABLE if hard_problem else STATE_MISSING,
        problem=hard_problem or "no Keychain entry found for this config dir",
    )


def _is_cli_process(argv: str) -> bool:
    if any(marker in argv for marker in _NOT_A_CLI):
        return False
    return bool(_CLAUDE_CMD.search(argv.split(" ")[0] if argv else ""))


def list_holders(
    *,
    default_config_dir: str,
    runner: Runner | None = None,
    timeout_s: float = 10.0,
) -> dict[str, list[ProcessHolder]]:
    """Map config directory -> live Claude Code processes holding it.

    Two calls: one ``ps`` for the process table, then one ``ps eww`` per candidate
    to read its environment. The per-process form is used rather than a single
    batched call because a batched row must be split by a leading PID, and a
    process whose own argv contains the variable name would be misattributed. At
    roughly ten candidates this costs about 30ms, which does not justify the
    ambiguity.
    """
    table = run_command(
        ["ps", "-Ao", "pid=,etime=,command="], runner=runner, timeout_s=timeout_s
    )
    if not table.ok:
        return {}

    holders: dict[str, list[ProcessHolder]] = {}
    for line in (table.stdout or "").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid_text, elapsed, argv = parts
        if not pid_text.isdigit() or not _is_cli_process(argv):
            continue
        pid = int(pid_text)

        env_out = run_command(
            ["ps", "eww", "-p", str(pid)], runner=runner, timeout_s=timeout_s
        )
        config_dir = default_config_dir
        if env_out.ok:
            # Last occurrence, not first: the environment is printed after the
            # command, so a session whose own argv mentions the variable cannot
            # shadow the real value.
            matches = re.findall(rf"{_CONFIG_DIR_VAR}=(\S+)", env_out.stdout or "")
            if matches:
                config_dir = str(Path(os.path.expanduser(matches[-1].rstrip("/"))))

        holders.setdefault(config_dir, []).append(
            ProcessHolder(pid=pid, elapsed=elapsed, argv=argv[:200])
        )
    return holders


def take_sample(
    accounts: Sequence[tuple[str, str]],
    *,
    now_s: float,
    now_iso: str,
    default_config_dir: str,
    runner: Runner | None = None,
    home: Path | str | None = None,
) -> Sample:
    """One full sweep across ``accounts``, given as ``(account_id, config_dir)``."""
    creds = {
        account_id: read_credential(
            account_id, config_dir, now_s=now_s, runner=runner, home=home
        )
        for account_id, config_dir in accounts
    }
    holders = list_holders(default_config_dir=default_config_dir, runner=runner)
    return Sample(at=now_iso, at_s=now_s, credentials=creds, holders=holders)


def detect_transitions(previous: Sample | None, current: Sample) -> list[Transition]:
    """Every account whose credential state changed between two samples.

    A first-ever sample yields nothing. Without a prior state there is no
    transition, and reporting one would file the whole fleet as newly broken the
    first time this runs.
    """
    if previous is None:
        return []
    out: list[Transition] = []
    for account_id, cur in current.credentials.items():
        prev = previous.credentials.get(account_id)
        if prev is None or prev.state == cur.state:
            continue
        out.append(
            Transition(
                account_id=account_id,
                config_dir=cur.config_dir,
                previous_state=prev.state,
                current_state=cur.state,
                gap_s=round(current.at_s - previous.at_s, 3),
                lost=cur.state in LOST_STATES and prev.state not in LOST_STATES,
            )
        )
    return out


def _load_state(path: Path) -> list[dict[str, Any]]:
    """Prior samples, oldest first. A corrupt or missing file means no history.

    Losing the history costs context in the next record. Raising here would stop
    the sampler, which costs every future record. So this degrades and continues.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    samples = data.get("samples") if isinstance(data, Mapping) else None
    return [s for s in samples if isinstance(s, Mapping)] if isinstance(samples, list) else []


def _rehydrate(raw: Mapping[str, Any]) -> Sample:
    creds = {
        k: CredentialSample(**v)
        for k, v in (raw.get("credentials") or {}).items()
        if isinstance(v, Mapping)
    }
    holders = {
        k: [ProcessHolder(**p) for p in v if isinstance(p, Mapping)]
        for k, v in (raw.get("holders") or {}).items()
        if isinstance(v, list)
    }
    return Sample(
        at=str(raw.get("at") or ""),
        at_s=float(raw.get("at_s") or 0.0),
        credentials=creds,
        holders=holders,
    )


def run_once(
    accounts: Sequence[tuple[str, str]],
    *,
    now_s: float,
    now_iso: str,
    default_config_dir: str,
    state_path: Path,
    log_path: Path,
    runner: Runner | None = None,
    home: Path | str | None = None,
    history_depth: int = DEFAULT_HISTORY_DEPTH,
) -> tuple[Sample, list[Transition]]:
    """Sample once, record any transition, and roll the history forward.

    Returns the sample and the transitions found, so a caller can print them.
    Writing the log is best-effort: an unwritable log directory must not stop the
    sampling that fills it.
    """
    history = _load_state(state_path)
    previous = _rehydrate(history[-1]) if history else None

    current = take_sample(
        accounts,
        now_s=now_s,
        now_iso=now_iso,
        default_config_dir=default_config_dir,
        runner=runner,
        home=home,
    )
    transitions = detect_transitions(previous, current)

    if transitions:
        record = {
            "at": now_iso,
            "at_s": now_s,
            "transitions": [asdict(t) for t in transitions],
            # The whole retained window, not just the previous sample. Which
            # process appeared four samples before the loss is exactly the kind
            # of thing that will not be obvious until it is read.
            "lead_up": history,
            "current": current.to_dict(),
        }
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError:
            pass

    history.append(current.to_dict())
    trimmed = history[-max(1, history_depth) :]
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"samples": trimmed}), encoding="utf-8")
        os.replace(tmp, state_path)
    except OSError:
        pass

    return current, transitions
