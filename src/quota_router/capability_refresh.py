"""Read each account's model list and keep the capability table current.

The only code in this package that reads a model list. It runs from the launchd usage
poller (after ``status``) and from ``quotapick capabilities --refresh``; ``pick`` never
calls it.

Sources, per provider:

* **Claude:** ``<config_dir>/cache/model-catalog/<organizationUuid>-*-cc.json``, which
  Claude Code itself writes. A file read; no token is touched. The identity comes from
  the shared ``.claude.json`` reader, which handles the default account's
  ``~/.claude.json`` living outside ``~/.claude``. ``CLAUDE_CONFIG_DIR`` is never set.
* **Codex:** ``model/list`` over the app-server channel ``status`` already uses, at most
  every :data:`NETWORK_INTERVAL_S`; ``models_cache.json`` when the call fails. Plus the
  home's ``config.toml`` default model, which both accounts served while neither list
  carried it.
* **Antigravity:** ``agy models``, run the way the account is launched (its
  ``launch-plan`` prefix and environment), at most every :data:`NETWORK_INTERVAL_S`.
  Gemini-flavour accounts only: a Claude-flavour pool was observed serving Gemini under
  a Claude label.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Final, Mapping, Sequence

from . import capability as cap
from .providers.claude_cli_config import _config_file_candidates, _identity_and_tier
from .providers.codex_app_server import CodexAppServerAdapter, ModelListFailed
from .providers.codex_sessions import CodexAccountConfig
from .types import PROVIDER_ANTIGRAVITY, PROVIDER_CLAUDE, PROVIDER_CODEX

__all__ = ["RefreshDeps", "RefreshReport", "refresh"]

#: Minimum spacing between network reads (``model/list``, ``agy models``) per account.
NETWORK_INTERVAL_S: Final[float] = 900.0
#: ``model/list`` measured 0.05-0.75s on 2026-10-05.
CODEX_TIMEOUT_S: Final[float] = 5.0
#: ``agy models`` measured 3.2s on 2026-10-05.
AGY_TIMEOUT_S: Final[float] = 20.0
#: Oldest ``models_cache.json`` accepted when ``model/list`` fails, and the catalog age
#: past which a Claude resolution carries a warning.
STALE_SOURCE_S: Final[float] = 72 * 3600.0
#: The binary the macOS-user wrapper allowlists. Used only when such an account sets no
#: ``command``; the wrapper refuses anything else, loudly.
MACOS_USER_AGY_BINARY: Final[str] = "/usr/local/bin/agy"


class ReadFailed(Exception):
    """One account's list could not be read. ``kind`` is the error taxonomy."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True, slots=True)
class Completed:
    returncode: int
    stdout: str
    stderr: str


def _default_run(argv: Sequence[str], env: Mapping[str, str], timeout_s: float) -> Completed:
    result = subprocess.run(  # noqa: S603 - argv built here, never a shell string
        list(argv), env=dict(env), capture_output=True, text=True, timeout=timeout_s,
        stdin=subprocess.DEVNULL, check=False,
    )
    return Completed(result.returncode, result.stdout, result.stderr)


def _default_codex_model_list(
    account: CodexAccountConfig, env: Mapping[str, str], timeout_s: float
) -> Any:
    adapter = CodexAppServerAdapter(
        codex_accounts=[account], timeout_s=timeout_s, read_identity=False, env=env
    )
    return adapter.model_list(account, include_hidden=True)


@dataclass(frozen=True, slots=True)
class RefreshDeps:
    """Injected I/O, so tests never spawn a process."""

    codex_model_list: Callable[[CodexAccountConfig, Mapping[str, str], float], Any] = (
        _default_codex_model_list
    )
    run: Callable[[Sequence[str], Mapping[str, str], float], Completed] = _default_run


@dataclass
class RefreshReport:
    """What one refresh did, per account: attempted / succeeded / failed by kind."""

    table_path: str
    outcomes: list[dict[str, Any]] = field(default_factory=list)
    moves: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    locked: bool = False

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [o for o in self.outcomes if o["outcome"] == "failed"]

    def to_dict(self) -> dict[str, Any]:
        attempted = [o for o in self.outcomes if o["outcome"] != "skipped"]
        kinds: dict[str, int] = {}
        for outcome in self.failed:
            kinds[outcome["kind"]] = kinds.get(outcome["kind"], 0) + 1
        return {
            "table_path": self.table_path,
            "locked": self.locked,
            "attempted": len(attempted),
            "succeeded": len([o for o in attempted if o["outcome"] in ("read", "reused")]),
            "failed": len(self.failed),
            "failed_by_kind": kinds,
            "outcomes": self.outcomes,
            "moves": self.moves,
            "warnings": self.warnings,
        }


# ======================================================================================
# Readers
# ======================================================================================


def read_claude_listing(account: Any) -> cap.Listing:
    """The newest Claude Code model catalog for this account's organization."""
    if not account.config_dir:
        raise ReadFailed("no_config_dir", f"{account.id} has no config_dir")
    config_dir = Path(account.config_dir)
    identity, _tier, identity_file = _identity_and_tier(config_dir)
    if identity is None or not identity.organization_uuid:
        tried = ", ".join(str(p) for p in _config_file_candidates(config_dir))
        raise ReadFailed(
            "no_identity", f"no oauthAccount.organizationUuid in any of {tried}"
        )
    catalog_dir = config_dir / "cache" / "model-catalog"
    uuid = identity.organization_uuid
    files = sorted(catalog_dir.glob(f"{uuid}-*-cc.json"))
    if not files:
        raise ReadFailed(
            "no_catalog",
            f"no model catalog for org {uuid[:8]}... in {catalog_dir} (identity from "
            f"{identity_file}); run claude once on this account",
        )
    best: tuple[float, Path, list[tuple[str, str]]] | None = None
    problems = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            fetched_ms = data["fetchedAt"]
            models = data["catalog"]["config"]["models"]
            if not isinstance(fetched_ms, (int, float)) or not isinstance(models, list):
                raise TypeError("fetchedAt or catalog.config.models has the wrong type")
            entries = []
            for model in models:
                if not isinstance(model.get("id"), str) or not isinstance(model.get("section"), str):
                    raise TypeError(f"model entry without string id/section: {model!r:.80}")
                entries.append((model["id"], model["section"]))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            problems.append(f"{path.name}: {exc}")
            continue
        fetched_s = float(fetched_ms) / 1000.0
        if best is None or fetched_s > best[0]:
            best = (fetched_s, path, entries)
    if best is None:
        raise ReadFailed("malformed", f"no readable catalog in {catalog_dir}: {'; '.join(problems)}")
    return cap.Listing(
        provider=PROVIDER_CLAUDE,
        source="claude_catalog",
        fetched_at_s=best[0],
        location=str(best[1]),
        entries=tuple(best[2]),
    )



def _codex_config_default(home: Path) -> tuple[str | None, str | None]:
    """``(model, warning)`` from ``<CODEX_HOME>/config.toml``."""
    path = home / "config.toml"
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return None, None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return None, f"cannot read {path}: {exc}"
    model = data.get("model")
    if model is None:
        return None, None
    if not isinstance(model, str) or not model.strip():
        return None, f"{path}: model is not a non-empty string"
    return model.strip(), None


def _parse_iso(text: Any) -> float | None:
    if not isinstance(text, str):
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def read_codex_listing(
    account: Any, env: Mapping[str, str], deps: RefreshDeps, now_s: float
) -> tuple[cap.Listing, list[str]]:
    """``model/list``, else ``models_cache.json``; plus the ``config.toml`` default."""
    if not account.config_dir:
        raise ReadFailed("no_config_dir", f"{account.id} has no config_dir")
    home = Path(account.config_dir)
    warnings: list[str] = []
    default, problem = _codex_config_default(home)
    if problem:
        warnings.append(f"{account.id}: {problem}")
    extras = (default,) if default else ()

    try:
        result = deps.codex_model_list(
            CodexAccountConfig(account_id=account.id, codex_home=home), env, CODEX_TIMEOUT_S
        )
        data = result.get("data") if isinstance(result, Mapping) else None
        if not isinstance(data, list):
            raise ModelListFailed(f"model/list result has no data list: {str(result)[:120]}")
        entries = []
        for model in data:
            if not isinstance(model, Mapping) or not isinstance(model.get("id"), str):
                raise ModelListFailed(f"model/list entry without a string id: {str(model)[:120]}")
            entries.append((model["id"], "hide" if model.get("hidden") else "list"))
        return (
            cap.Listing(
                provider=PROVIDER_CODEX, source="codex_model_list", fetched_at_s=now_s,
                location=f"codex app-server model/list (CODEX_HOME={home})",
                entries=tuple(entries), extras=extras,
            ),
            warnings,
        )
    except Exception as exc:  # noqa: BLE001 - any failure falls to the cache, reported
        rpc_error = f"{type(exc).__name__}: {exc}"

    cache_path = home / "models_cache.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        fetched_s = _parse_iso(cache.get("fetched_at"))
        entries = [
            (str(m["slug"]), "hide" if m.get("visibility") == "hide" else "list")
            for m in cache["models"]
        ]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ReadFailed(
            "rpc_failed_no_cache",
            f"model/list failed ({rpc_error}) and {cache_path} is unreadable ({exc})",
        ) from exc
    if fetched_s is None or now_s - fetched_s > STALE_SOURCE_S:
        raise ReadFailed(
            "rpc_failed_cache_stale",
            f"model/list failed ({rpc_error}) and {cache_path} is older than 72h",
        )
    warnings.append(f"{account.id}: model/list failed ({rpc_error}); used {cache_path}")
    return (
        cap.Listing(
            provider=PROVIDER_CODEX, source="codex_models_cache", fetched_at_s=fetched_s,
            location=str(cache_path), entries=tuple(entries), extras=extras,
        ),
        warnings,
    )


def _agy_argv_env(account: Any, env: Mapping[str, str]) -> tuple[list[str], dict[str, str]]:
    child = dict(env)
    child.update(account.exec_env())
    for name in account.launch_unset_env:
        child.pop(name, None)
    prefix = list(account.launch_argv_prefix)
    if prefix:
        binary = account.command or MACOS_USER_AGY_BINARY
    else:
        binary = shutil.which(account.exec_command, path=env.get("PATH")) or ""
        if not binary:
            raise ReadFailed(
                "agy_missing", f"{account.exec_command!r} is not on PATH ({env.get('PATH')})"
            )
    return [*prefix, binary, "models"], child


def read_agy_listing(
    account: Any, env: Mapping[str, str], deps: RefreshDeps, now_s: float
) -> tuple[cap.Listing, list[str]]:
    """``agy models``, run the way this account is launched."""
    argv, child = _agy_argv_env(account, env)
    try:
        done = deps.run(argv, child, AGY_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise ReadFailed("agy_timeout", f"{' '.join(argv)} timed out after {AGY_TIMEOUT_S:g}s")
    except OSError as exc:
        raise ReadFailed("agy_failed", f"{' '.join(argv)}: {exc}") from exc
    if done.returncode != 0:
        tail = (done.stderr or done.stdout).strip()[-300:]
        raise ReadFailed("agy_failed", f"{' '.join(argv)} exited {done.returncode}: {tail}")
    entries, warnings = [], []
    for line in done.stdout.splitlines():
        text = line.strip()
        if not text or text == "Fetching available models...":
            continue
        if "\t" not in text:
            warnings.append(f"{account.id}: unexpected agy models line {text[:80]!r}")
            continue
        model_id, label = text.split("\t", 1)
        entries.append((model_id.strip(), label.strip()))
    if not entries:
        raise ReadFailed("agy_empty", f"{' '.join(argv)} listed no models")
    return (
        cap.Listing(
            provider=PROVIDER_ANTIGRAVITY, source="agy_models", fetched_at_s=now_s,
            location=" ".join(argv), entries=tuple(entries),
        ),
        warnings,
    )


# ======================================================================================
# The refresh
# ======================================================================================


def _due(prior: cap.Listing | None, now_s: float, force: bool) -> bool:
    if force or prior is None or prior.fetched_at_s is None:
        return True
    return now_s - prior.fetched_at_s >= NETWORK_INTERVAL_S


def refresh(
    config: Any,
    *,
    env: Mapping[str, str],
    now_s: float,
    deps: RefreshDeps | None = None,
    force: bool = False,
) -> RefreshReport:
    """Read what is due, debounce, and write the table. Never raises for one account."""
    deps = deps or RefreshDeps()
    path = cap.table_path(env)
    report = RefreshReport(table_path=str(path))
    path.parent.mkdir(parents=True, exist_ok=True)

    lock = open(path.with_name(".capabilities.lock"), "a+")  # noqa: SIM115 - held below
    try:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            report.locked = True
            report.warnings.append("another refresh holds the lock; skipped")
            return report
        table, error = cap.read_table(path)
        if error:
            report.warnings.append(f"{error}; rebuilding it")
        table = table or cap.empty_table()
        _refresh_locked(config, table, report, env=env, now_s=now_s, deps=deps, force=force)
        table["written_at"] = now_s
        cap.write_table(path, table)
        if report.moves:
            with open(cap.moves_path(path), "a", encoding="utf-8") as log:
                for move in report.moves:
                    log.write(json.dumps(move, sort_keys=True) + "\n")
    finally:
        lock.close()
    return report


def _refresh_locked(
    config: Any,
    table: dict[str, Any],
    report: RefreshReport,
    *,
    env: Mapping[str, str],
    now_s: float,
    deps: RefreshDeps,
    force: bool,
) -> None:
    capabilities = getattr(config, "capabilities", None) or cap.validate_table(None)
    accounts_out: dict[str, Any] = {}
    prior_accounts = table.get("accounts", {})

    for account in config.enabled_accounts():
        provider = account.provider
        if provider not in cap.CAPABILITY_PROVIDERS:
            continue
        if provider == PROVIDER_ANTIGRAVITY and cap.antigravity_flavour(account) == "claude":
            report.outcomes.append({
                "account": account.id, "provider": provider, "outcome": "skipped",
                "kind": "claude_flavour_antigravity",
                "detail": "a Claude-flavour Antigravity pool can serve Gemini under a "
                          "Claude label, so it never takes part in capability routing",
            })
            continue

        prior = prior_accounts.get(account.id) if isinstance(prior_accounts, dict) else None
        prior_listing = cap.Listing.from_json(prior.get("list")) if isinstance(prior, dict) else None
        if prior_listing is not None and prior_listing.provider != provider:
            prior, prior_listing = None, None

        try:
            if provider == PROVIDER_CLAUDE:
                listing, warnings = read_claude_listing(account), []
                read = "read"
            elif _due(prior_listing, now_s, force):
                reader = read_codex_listing if provider == PROVIDER_CODEX else read_agy_listing
                listing, warnings = reader(account, env, deps, now_s)
                read = "read"
            else:
                listing, warnings, read = prior_listing, [], "reused"
        except ReadFailed as exc:
            report.outcomes.append({
                "account": account.id, "provider": provider, "outcome": "failed",
                "kind": exc.kind, "detail": str(exc),
            })
            if isinstance(prior, dict):
                accounts_out[account.id] = prior  # keep it; the TTL will expire it
            continue

        entry = dict(prior) if isinstance(prior, dict) else {"capabilities": {}}
        entry["provider"] = provider
        if read == "read":
            entry["refreshed_at"] = now_s
        entry["list"] = listing.to_json()
        new_data = prior_listing is None or listing.fetched_at_s != prior_listing.fetched_at_s
        entry_warnings = list(warnings)
        if (
            provider == PROVIDER_CLAUDE
            and listing.fetched_at_s is not None
            and now_s - listing.fetched_at_s > STALE_SOURCE_S
        ):
            entry_warnings.append(
                f"{account.id}: model catalog is {cap._duration(now_s - listing.fetched_at_s)} "
                f"old ({listing.location})"
            )
        entry["warnings"] = entry_warnings
        report.warnings.extend(entry_warnings)

        cap_entries = dict(entry.get("capabilities") or {})
        for name in cap.CAPABILITIES:
            selector = capabilities[name].get(provider, cap.NONE)
            fresh = cap.resolve(name, selector, listing)
            prior_cap = cap_entries.get(name)
            if new_data or not isinstance(prior_cap, dict) or (
                prior_cap.get("committed", {}).get("selector") != selector
            ):
                updated, move = cap.observe(prior_cap, fresh, now_s)
                cap_entries[name] = updated
                if move:
                    report.moves.append({"account": account.id, "capability": name, **move})
        entry["capabilities"] = cap_entries
        accounts_out[account.id] = entry
        report.outcomes.append({
            "account": account.id, "provider": provider, "outcome": read,
            "kind": None, "source": listing.source, "new_data": new_data,
        })

    table["accounts"] = accounts_out
