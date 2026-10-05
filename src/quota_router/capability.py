"""Capability tiers: a quality level, resolved to one concrete model per account.

A **capability** (``fast``, ``standard``, ``premium``, ``frontier``) is what a caller
wants; a **model** is what it requests from a vendor. The word "tier" is already taken
here (the subscription plan: ``max_20x``, ``pro``), and a *model class* is something else
again (the family that decides which usage windows apply, see :mod:`model_classes`).

This module is pure. It owns:

* the default capability table and its validation,
* the choosers that turn one account's model list into one id,
* the debounce that keeps a flapping list from moving a capability back and forth,
* the on-disk table that the refresher writes and ``pick`` reads.

Reading model lists is I/O and lives in :mod:`capability_refresh`. ``pick`` never reads
a model list: digital-twin kills ``pick`` at 3000 ms and a timeout silently drops its
routing, so the hot path reads one resolved table and nothing else.

A resolved id is what to **request**. Nothing here proves what a vendor serves: agy was
observed rewriting ``Claude Opus 4.6 (Thinking)`` to ``Gemini 3.8 Flash (High)`` without
saying so. Clients check the served model in their own telemetry.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from .types import PROVIDER_ANTIGRAVITY, PROVIDER_CLAUDE, PROVIDER_CODEX

__all__ = [
    "CAPABILITIES",
    "CAPABILITY_PROVIDERS",
    "DEFAULT_TABLE",
    "Listing",
    "ModelChoice",
    "Unresolved",
    "antigravity_flavour",
    "choose_claude",
    "choose_codex",
    "choose_gemini",
    "lookup",
    "observe",
    "read_table",
    "resolve",
    "table_path",
    "validate_table",
    "write_table",
]

# ======================================================================================
# The table
# ======================================================================================

CAPABILITIES: Final[tuple[str, ...]] = ("fast", "standard", "premium", "frontier")

#: Providers a capability can resolve on. Cursor publishes no model list.
CAPABILITY_PROVIDERS: Final[tuple[str, ...]] = (
    PROVIDER_CLAUDE,
    PROVIDER_CODEX,
    PROVIDER_ANTIGRAVITY,
)

#: Per provider, the selector words that mean "the newest model of this kind". Any other
#: string in the table is an exact pin.
SELECTORS: Final[Mapping[str, tuple[str, ...]]] = {
    PROVIDER_CLAUDE: ("haiku", "sonnet", "opus", "fable"),
    PROVIDER_CODEX: ("luna", "terra", "sol", "astra"),
    PROVIDER_ANTIGRAVITY: ("flash", "pro"),
}

#: The value that removes a provider from a capability.
NONE: Final[str] = "none"

DEFAULT_TABLE: Final[Mapping[str, Mapping[str, str]]] = {
    "fast": {PROVIDER_CLAUDE: "haiku", PROVIDER_CODEX: "luna", PROVIDER_ANTIGRAVITY: "flash"},
    "standard": {
        PROVIDER_CLAUDE: "sonnet",
        PROVIDER_CODEX: "terra",
        PROVIDER_ANTIGRAVITY: "flash",
    },
    "premium": {PROVIDER_CLAUDE: "opus", PROVIDER_CODEX: "sol", PROVIDER_ANTIGRAVITY: "pro"},
    # Antigravity lists no Fable- or Astra-class model.
    "frontier": {PROVIDER_CLAUDE: "fable", PROVIDER_CODEX: "astra", PROVIDER_ANTIGRAVITY: NONE},
}

#: Antigravity labels carry the effort. The table names the High variant; a client that
#: wants another effort rewrites the suffix, as digital-twin already does.
GEMINI_EFFORT: Final[str] = "high"


def validate_table(raw: Any) -> dict[str, dict[str, str]]:
    """Merge an operator's ``[capabilities]`` section over :data:`DEFAULT_TABLE`.

    Raises:
        ValueError: an unknown capability or provider, a non-string value, or a selector
            that belongs to another provider (``codex = "opus"``). Each is a typo that
            would otherwise route silently to the wrong place.
    """
    merged = {cap: dict(row) for cap, row in DEFAULT_TABLE.items()}
    if raw is None:
        return merged
    if not isinstance(raw, Mapping):
        raise ValueError(f"[capabilities] must be a table, got {type(raw).__name__}")
    for cap, row in raw.items():
        if cap not in CAPABILITIES:
            raise ValueError(
                f"[capabilities.{cap}] is not a capability (known: {', '.join(CAPABILITIES)})"
            )
        if not isinstance(row, Mapping):
            raise ValueError(f"[capabilities.{cap}] must be a table, got {type(row).__name__}")
        for provider, value in row.items():
            where = f"capabilities.{cap}.{provider}"
            if provider not in CAPABILITY_PROVIDERS:
                raise ValueError(
                    f"{where}: unknown provider (known: {', '.join(CAPABILITY_PROVIDERS)})"
                )
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{where}: must be a non-empty string")
            text = value.strip()
            for other, words in SELECTORS.items():
                if other != provider and text in words:
                    raise ValueError(
                        f"{where}: {text!r} is a {other} selector, not a {provider} one "
                        f"(use one of {', '.join(SELECTORS[provider])}, an exact model id, "
                        f"or {NONE!r})"
                    )
            merged[cap][provider] = text
    return merged


def antigravity_flavour(account: Any) -> str:
    """``"claude"`` or ``"gemini"``: which Antigravity pool an account draws on.

    Same rule the config documents for ``AGY_MODEL``: a value containing "claude" means
    the Claude pool, anything else means Gemini.
    """
    env = getattr(account, "env", None) or {}
    return "claude" if "claude" in str(env.get("AGY_MODEL", "")).casefold() else "gemini"


# ======================================================================================
# Choosers (pure)
# ======================================================================================


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.split(r"[.-]", text) if part)


def choose_claude(
    models: Sequence[tuple[str, str]], family: str
) -> tuple[str | None, list[str]]:
    """The ``main``-section model of ``family`` in a Claude Code catalog.

    Args:
        models: ``(id, section)`` pairs from ``catalog.config.models``.
        family: ``haiku`` / ``sonnet`` / ``opus`` / ``fable``.

    Returns:
        ``(id, warnings)``. ``id`` is ``None`` when the main section has no such model.
        More than one main model of a family resolves to the highest version, with a
        warning, because the catalog has always held exactly one.
    """
    pattern = re.compile(rf"^claude-{re.escape(family)}-(\d+(?:-\d+)?)(?:-(\d{{8}}))?$")
    matches = []
    for model_id, section in models:
        found = pattern.match(model_id)
        if found and section == "main":
            matches.append((_version(found.group(1)), found.group(2) or "", model_id))
    if not matches:
        return None, []
    matches.sort()
    warnings = []
    if len(matches) > 1:
        names = ", ".join(item[2] for item in matches)
        warnings.append(f"catalog main section lists several {family} models ({names}); took the newest")
    return matches[-1][2], warnings


def choose_codex(slugs: Sequence[str], size: str) -> tuple[str | None, str | None]:
    """The newest ``gpt-<version>-<size>`` among ``slugs``.

    Returns:
        ``(id, note)``. The note says when the newest generation has no model of this
        size, so the answer is a generation behind (``gpt-5.6-terra`` while gpt-6 exists).
    """
    pattern = re.compile(rf"^gpt-(\d+(?:\.\d+)*)-{re.escape(size)}$")
    any_size = re.compile(r"^gpt-(\d+(?:\.\d+)*)-(?:luna|terra|sol|astra)$")
    sized = sorted(
        (_version(found.group(1)), slug)
        for slug in slugs
        if (found := pattern.match(slug))
    )
    if not sized:
        return None, None
    chosen_version, chosen = sized[-1]
    newest_major = max(
        _version(found.group(1))[0] for slug in slugs if (found := any_size.match(slug))
    )
    note = None
    if chosen_version[0] < newest_major:
        note = f"no gpt-{newest_major} {size}; newest {size} is {chosen}"
    return chosen, note


def choose_gemini(
    models: Sequence[tuple[str, str]], line: str, effort: str = GEMINI_EFFORT
) -> str | None:
    """The label of the newest ``gemini-<version>-<line>-<effort>`` in an ``agy models`` list."""
    pattern = re.compile(rf"^gemini-(\d+(?:\.\d+)*)-{re.escape(line)}-{re.escape(effort)}$")
    found = sorted(
        (_version(match.group(1)), label)
        for model_id, label in models
        if (match := pattern.match(model_id))
    )
    return found[-1][1] if found else None


# ======================================================================================
# Listings and resolutions
# ======================================================================================


@dataclass(frozen=True, slots=True)
class Listing:
    """One account's model list, as one read returned it.

    Attributes:
        provider: ``claude`` / ``codex`` / ``antigravity``.
        source: ``claude_catalog`` / ``codex_model_list`` / ``codex_models_cache`` /
            ``agy_models``.
        fetched_at_s: When the *source* fetched it, read from the data (epoch seconds,
            UTC). A new value is what makes a refresh count as a new observation.
        location: The file or command, for messages.
        entries: Claude ``(id, section)``; Codex ``(slug, "list"|"hide")``; Antigravity
            ``(id, label)``.
        extras: Codex only: the home's ``config.toml`` default model, available but not
            in the server list.
    """

    provider: str
    source: str
    fetched_at_s: float | None
    location: str
    entries: tuple[tuple[str, str], ...]
    extras: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "source": self.source,
            "fetched_at": self.fetched_at_s,
            "location": self.location,
            "entries": [list(entry) for entry in self.entries],
            "extras": list(self.extras),
        }

    @classmethod
    def from_json(cls, raw: Any) -> "Listing | None":
        if not isinstance(raw, Mapping):
            return None
        try:
            return cls(
                provider=str(raw["provider"]),
                source=str(raw["source"]),
                fetched_at_s=(
                    float(raw["fetched_at"]) if raw.get("fetched_at") is not None else None
                ),
                location=str(raw["location"]),
                entries=tuple((str(a), str(b)) for a, b in raw["entries"]),
                extras=tuple(str(x) for x in raw.get("extras", ())),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True, slots=True)
class ModelChoice:
    """The model to **request** on one account for one capability.

    ``listed`` means the id was in the account's server-provided list at refresh time.
    It is not proof of what the vendor will serve.
    """

    id: str
    family: str | None
    provider: str
    source: str
    source_fetched_at_s: float | None
    listed: bool
    since_s: float | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "family": self.family,
            "provider": self.provider,
            "source": self.source,
            "source_fetched_at": _iso(self.source_fetched_at_s),
            "listed": self.listed,
            "since": _iso(self.since_s),
            "note": self.note,
        }


    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ModelChoice":
        """Inverse of :meth:`to_dict`, for the Python API."""
        return cls(
            id=str(raw["id"]),
            family=raw.get("family"),
            provider=str(raw.get("provider")),
            source=str(raw.get("source")),
            source_fetched_at_s=_epoch(raw.get("source_fetched_at")),
            listed=bool(raw.get("listed")),
            since_s=_epoch(raw.get("since")),
            note=raw.get("note"),
        )


@dataclass(frozen=True, slots=True)
class Unresolved:
    """No model for this account and capability, and why."""

    reason: str


def _resolution(
    *,
    model_id: str | None,
    reason: str | None,
    family: str | None,
    listing: Listing | None,
    source: str | None = None,
    listed: bool = False,
    note: str | None = None,
    selector: str,
) -> dict[str, Any]:
    return {
        "id": model_id,
        "reason": reason,
        "family": family,
        "source": source or (listing.source if listing else None),
        "source_fetched_at": listing.fetched_at_s if listing else None,
        "listed": listed,
        "note": note,
        "selector": selector,
    }


def resolve(capability: str, selector: str, listing: Listing) -> dict[str, Any]:
    """Resolve one capability on one account's listing to a resolution record.

    A record has an ``id`` or a ``reason``, never a guessed id.
    """
    provider = listing.provider
    if selector == NONE:
        return _resolution(
            model_id=None,
            reason=f"no {provider} model for {capability}",
            family=None,
            listing=listing,
            selector=selector,
        )

    if provider == PROVIDER_CLAUDE:
        ids = {model_id for model_id, _section in listing.entries}
        if selector not in SELECTORS[PROVIDER_CLAUDE]:
            return _resolution(
                model_id=selector, reason=None, family=None, listing=listing,
                source="config_pin", listed=selector in ids, selector=selector,
            )
        chosen, warnings = choose_claude(listing.entries, selector)
        if chosen is None:
            return _resolution(
                model_id=None,
                reason=f"catalog has no main-section {selector} model ({listing.location})",
                family=selector, listing=listing, selector=selector,
            )
        return _resolution(
            model_id=chosen, reason=None, family=selector, listing=listing, listed=True,
            note="; ".join(warnings) or None, selector=selector,
        )

    if provider == PROVIDER_CODEX:
        visible = [slug for slug, visibility in listing.entries if visibility != "hide"]
        if selector not in SELECTORS[PROVIDER_CODEX]:
            listed = selector in visible
            return _resolution(
                model_id=selector, reason=None, family=None, listing=listing,
                source="config_pin", listed=listed, selector=selector,
            )
        chosen, note = choose_codex([*visible, *listing.extras], selector)
        if chosen is None:
            return _resolution(
                model_id=None,
                reason=f"no gpt-*-{selector} model in {listing.source} ({listing.location})",
                family=selector, listing=listing, selector=selector,
            )
        if chosen in visible:
            return _resolution(
                model_id=chosen, reason=None, family=selector, listing=listing, listed=True,
                note=note, selector=selector,
            )
        return _resolution(
            model_id=chosen, reason=None, family=selector, listing=listing,
            source="codex_config_default", listed=False, note=note, selector=selector,
        )

    if provider == PROVIDER_ANTIGRAVITY:
        labels = {label for _model_id, label in listing.entries}
        if selector not in SELECTORS[PROVIDER_ANTIGRAVITY]:
            return _resolution(
                model_id=selector, reason=None, family=None, listing=listing,
                source="config_pin", listed=selector in labels, selector=selector,
            )
        chosen = choose_gemini(listing.entries, selector)
        if chosen is None:
            return _resolution(
                model_id=None,
                reason=f"agy lists no gemini {selector} model at {GEMINI_EFFORT} effort",
                family=selector, listing=listing, selector=selector,
            )
        return _resolution(
            model_id=chosen, reason=None, family=selector, listing=listing, listed=True,
            selector=selector,
        )

    return _resolution(
        model_id=None, reason=f"provider {provider!r} has no model list",
        family=None, listing=listing, selector=selector,
    )


# ======================================================================================
# Debounce
# ======================================================================================

#: A new resolution must be seen in this many new-data refreshes...
DEBOUNCE_MIN_SEEN: Final[int] = 2
#: ...spanning at least this long, before it replaces the committed one. On 2026-10-05
#: codex_b's list carried gpt-6.1-sol at 05:36:47Z and had dropped it by 05:43:29Z.
DEBOUNCE_MIN_AGE_S: Final[float] = 1800.0


def _key(resolution: Mapping[str, Any]) -> str:
    model_id = resolution.get("id")
    return f"id:{model_id}" if model_id else "unresolved"


def observe(
    entry: Mapping[str, Any] | None,
    fresh: Mapping[str, Any],
    now_s: float,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Fold one new-data observation into a capability entry.

    Returns:
        ``(entry, move)``. ``move`` is a record when the committed resolution changed,
        else ``None``.

    Rules:

    * The first resolution ever seen commits at once; there is nothing to flap against.
    * A selector change in config commits at once; that is the operator's decision.
    * Otherwise a different resolution becomes a candidate, and commits only once it
      has been seen in :data:`DEBOUNCE_MIN_SEEN` observations over
      :data:`DEBOUNCE_MIN_AGE_S`. Seeing the committed resolution again clears the
      candidate, so appear-disappear-reappear starts the clock over.
    * "Unresolved" is a resolution like any other, so one empty read cannot drop a
      working model.
    """
    fresh = dict(fresh)
    if not entry or not isinstance(entry.get("committed"), Mapping):
        return (
            {"committed": {**fresh, "since": now_s}, "candidate": None, "moved_at": None,
             "moved_from": None},
            None,
        )

    out = copy.deepcopy(dict(entry))
    committed = out["committed"]

    if committed.get("selector") != fresh.get("selector"):
        move = _move(out, committed, fresh, now_s, cause="config")
        out.update(committed={**fresh, "since": now_s}, candidate=None, moved_at=now_s,
                   moved_from=committed.get("id"))
        return out, move

    if _key(fresh) == _key(committed):
        # Same answer: refresh its metadata (fetched time, note), drop any candidate.
        out["committed"] = {**fresh, "since": committed.get("since", now_s)}
        out["candidate"] = None
        return out, None

    candidate = out.get("candidate")
    if isinstance(candidate, Mapping) and _key(candidate) == _key(fresh):
        seen = int(candidate.get("seen", 1)) + 1
        first_seen = float(candidate.get("first_seen", now_s))
        if seen >= DEBOUNCE_MIN_SEEN and now_s - first_seen >= DEBOUNCE_MIN_AGE_S:
            move = _move(out, committed, fresh, now_s, cause="debounced")
            out.update(committed={**fresh, "since": now_s}, candidate=None, moved_at=now_s,
                       moved_from=committed.get("id"))
            return out, move
        out["candidate"] = {**fresh, "first_seen": first_seen, "seen": seen}
        return out, None

    out["candidate"] = {**fresh, "first_seen": now_s, "seen": 1}
    return out, None


def _move(
    entry: Mapping[str, Any],
    committed: Mapping[str, Any],
    fresh: Mapping[str, Any],
    now_s: float,
    *,
    cause: str,
) -> dict[str, Any]:
    return {
        "at": _iso(now_s),
        "at_s": now_s,
        "from": committed.get("id"),
        "from_reason": committed.get("reason"),
        "to": fresh.get("id"),
        "to_reason": fresh.get("reason"),
        "cause": cause,
    }


# ======================================================================================
# The table file
# ======================================================================================

TABLE_VERSION: Final[int] = 1
TABLE_ENV: Final[str] = "QUOTA_ROUTER_CAPABILITIES"
#: An entry older than this is not used: the poller refreshes every 2 minutes and calls
#: the network sources every 15, so an hour means the refresher has stopped.
TABLE_TTL_S: Final[float] = 3600.0


def table_path(env: Mapping[str, str] | None = None) -> Path:
    """``$QUOTA_ROUTER_CAPABILITIES``, else ``capabilities.json`` beside the state file."""
    from .state import state_path  # local: state imports nothing from here, keep it so

    environ = os.environ if env is None else env
    override = (environ.get(TABLE_ENV) or "").strip()
    if override:
        home = environ.get("HOME") or str(Path.home())
        text = home + override[1:] if override == "~" or override.startswith("~/") else override
        return Path(text)
    return state_path(environ).parent / "capabilities.json"


def moves_path(table: Path) -> Path:
    """Where committed moves are logged: beside the table, never in ``history.jsonl``.

    ``history.jsonl`` is the snapshot cache ``pick`` falls back on; a record of another
    shape there could be read as the latest snapshot.
    """
    return table.with_name("capability-moves.jsonl")


def empty_table() -> dict[str, Any]:
    return {"version": TABLE_VERSION, "written_at": None, "accounts": {}}


def read_table(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """``(table, None)``, ``(None, None)`` when absent, or ``(None, error)`` when unreadable."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"cannot read capability table {path}: {exc}"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return None, f"capability table {path} is not valid JSON: {exc}"
    if not isinstance(data, dict) or data.get("version") != TABLE_VERSION:
        return None, f"capability table {path} has an unknown shape (version {data.get('version') if isinstance(data, dict) else '?'})"
    if not isinstance(data.get("accounts"), dict):
        return None, f"capability table {path} has no accounts table"
    return data, None


def write_table(path: Path, table: Mapping[str, Any]) -> None:
    """Write atomically: ``pick`` may read at any moment and must never see half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=".capabilities.", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(table, out, indent=1, sort_keys=True)
            out.write("\n")
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def lookup(
    table: Mapping[str, Any] | None,
    account_id: str,
    capability: str,
    *,
    now_s: float,
    path: Path,
    ttl_s: float = TABLE_TTL_S,
) -> ModelChoice | Unresolved:
    """The committed model for one account and capability, from the table alone."""
    if table is None:
        return Unresolved(
            f"no capability table at {path}; run `quotapick capabilities --refresh`"
        )
    entry = table.get("accounts", {}).get(account_id)
    if not isinstance(entry, Mapping):
        return Unresolved(f"{account_id} has no entry in the capability table {path}")
    refreshed = entry.get("refreshed_at")
    if not isinstance(refreshed, (int, float)):
        return Unresolved(f"capability table entry for {account_id} has no refreshed_at")
    age = now_s - float(refreshed)
    if age > ttl_s:
        return Unresolved(
            f"capability table for {account_id} is {_duration(age)} old "
            f"(TTL {_duration(ttl_s)}); is the usage poller running?"
        )
    cap_entry = entry.get("capabilities", {}).get(capability)
    committed = cap_entry.get("committed") if isinstance(cap_entry, Mapping) else None
    if not isinstance(committed, Mapping):
        return Unresolved(f"capability table has no {capability} entry for {account_id}")
    model_id = committed.get("id")
    if not model_id:
        return Unresolved(str(committed.get("reason") or "unresolved"))
    return ModelChoice(
        id=str(model_id),
        family=committed.get("family"),
        provider=str(entry.get("provider")),
        source=str(committed.get("source")),
        source_fetched_at_s=committed.get("source_fetched_at"),
        listed=bool(committed.get("listed")),
        since_s=committed.get("since"),
        note=committed.get("note"),
    )


def fresh_listing(
    table: Mapping[str, Any] | None,
    account_id: str,
    *,
    now_s: float,
    path: Path,
    ttl_s: float = TABLE_TTL_S,
) -> Listing | Unresolved:
    """An account's last-read model list, if the table holds a fresh one."""
    if table is None:
        return Unresolved(
            f"no capability table at {path}; run `quotapick capabilities --refresh`"
        )
    entry = table.get("accounts", {}).get(account_id)
    if not isinstance(entry, Mapping):
        return Unresolved(f"{account_id} has no entry in the capability table {path}")
    refreshed = entry.get("refreshed_at")
    if not isinstance(refreshed, (int, float)) or now_s - float(refreshed) > ttl_s:
        return Unresolved(f"capability table for {account_id} is missing or past its TTL")
    listing = Listing.from_json(entry.get("list"))
    if listing is None:
        return Unresolved(f"capability table entry for {account_id} has no readable list")
    return listing


def listing_offers(listing: Listing, model_id: str) -> tuple[bool, bool]:
    """``(available, listed)`` for an exact id in one account's listing.

    Codex's ``config.toml`` default is available but not listed by the server.
    """
    if listing.provider == PROVIDER_ANTIGRAVITY:
        labels = {label for _id, label in listing.entries}
        return model_id in labels, model_id in labels
    if listing.provider == PROVIDER_CODEX:
        visible = {slug for slug, visibility in listing.entries if visibility != "hide"}
        return model_id in visible or model_id in listing.extras, model_id in visible
    ids = {model_id_ for model_id_, _section in listing.entries}
    return model_id in ids, model_id in ids


#: Which provider serves a model class, for ``--capability X --model <pin>``.
PROVIDER_FOR_CLASS: Final[Mapping[str, str]] = {
    "opus": PROVIDER_CLAUDE,
    "sonnet": PROVIDER_CLAUDE,
    "haiku": PROVIDER_CLAUDE,
    "fable": PROVIDER_CLAUDE,
    "gpt": PROVIDER_CODEX,
    "gemini": PROVIDER_ANTIGRAVITY,
}

#: How long after a committed move ``pick`` keeps warning about it.
MOVE_WARNING_S: Final[float] = 24 * 3600.0


def pick_notes(
    table: Mapping[str, Any] | None, account_id: str, capability: str, *, now_s: float
) -> list[str]:
    """Warnings ``pick`` adds for one account: recent moves, pending candidates, stale sources."""
    if table is None:
        return []
    entry = table.get("accounts", {}).get(account_id)
    if not isinstance(entry, Mapping):
        return []
    notes = [str(w) for w in entry.get("warnings", []) or []]
    cap_entry = (entry.get("capabilities") or {}).get(capability)
    if not isinstance(cap_entry, Mapping):
        return notes
    committed = cap_entry.get("committed") or {}
    moved_at = cap_entry.get("moved_at")
    if isinstance(moved_at, (int, float)) and now_s - moved_at <= MOVE_WARNING_S:
        notes.append(
            f"capability {capability} on {account_id} moved "
            f"{cap_entry.get('moved_from') or 'unresolved'} -> "
            f"{committed.get('id') or 'unresolved'} at {_iso(moved_at)}"
        )
    candidate = cap_entry.get("candidate")
    if isinstance(candidate, Mapping):
        notes.append(
            f"capability {capability} on {account_id}: "
            f"{candidate.get('id') or 'unresolved'} seen {candidate.get('seen')}x since "
            f"{_iso(candidate.get('first_seen'))}, not yet committed; still routing "
            f"{committed.get('id') or 'unresolved'}"
        )
    return notes


# ======================================================================================
# Formatting helpers
# ======================================================================================


def _epoch(text: Any) -> float | None:
    if not isinstance(text, str):
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def _iso(epoch_s: float | None) -> str | None:
    if epoch_s is None:
        return None
    return (
        datetime.fromtimestamp(float(epoch_s), tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{seconds // 60}m"
    hours, rest = divmod(seconds, 3600)
    return f"{hours}h{rest // 60:02d}m" if hours < 48 else f"{hours // 24}d{hours % 24}h"
