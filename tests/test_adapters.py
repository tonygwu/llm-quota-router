"""Adapter tests: fixture-driven, hermetic, with **zero subprocesses and zero network**.

Every process call in the package goes through an injected runner and every HTTP call
through an injected opener, so nothing here reaches the process table or a socket.
Filesystem sources are pointed at ``tmp_path``. A module-scoped autouse guard replaces
``urllib.request.urlopen`` with a function that raises a :class:`BaseException` -- chosen
deliberately over ``Exception`` so that neither the adapter's own ``except`` clauses nor
``collect_snapshots``'s catch-all can swallow an accidental real request into a warning.

The Claude usage fixture below (:func:`usage_payload`) is a trimmed real capture of
``GET /api/oauth/usage``: an idle 5-hour window, a 7-day window at 74%, and the
model-scoped weekly row for Fable at 95%. Its numbers are load-bearing -- the pacing
arithmetic asserted further down is derived from them and from :data:`NOW`.

The one test that touches the operator's real machine is marked ``live`` and is
deselected by default (``pytest -m live`` to run it); even that one only reads files.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

import pytest

from quota_router.providers import (
    AntigravityAdapter,
    ClaudeOAuthAdapter,
    ClaudeStatuslineAdapter,
    CodexSessionsAdapter,
    ProviderAdapter,
    claude_configs_from_policy,
    collect_snapshots,
    load_snapshots,
)
from quota_router.providers.base import (
    coerce_epoch_seconds,
    decay_confidence,
    parse_iso8601,
    pct_to_fraction,
    read_tail_lines,
)
from quota_router.providers.claude_cli_config import (
    ClaudeAccountConfig,
    discover_claude_configs,
)
from quota_router.providers.claude_oauth import (
    HTTP_METHOD,
    KEYCHAIN_SERVICE_BASE,
    USAGE_URL,
    keychain_service_for,
    parse_usage_payload,
    read_access_token,
    usage_cache_path,
)
from quota_router.providers.codex_sessions import window_key_for_minutes
from quota_router.types import (
    ACCOUNT_ANTIGRAVITY_CLAUDE,
    ACCOUNT_ANTIGRAVITY_GEMINI,
    ACCOUNT_CLAUDE,
    ACCOUNT_CLAUDE_B,
    ACCOUNT_CLAUDE_C,
    ACCOUNT_CLAUDE_D,
    ACCOUNT_CODEX,
    SOURCE_ASSUMED,
    SOURCE_CACHE,
    SOURCE_LIVE,
    TIER_MAX_5X,
    TIER_MAX_20X,
    TIER_PRO,
    TIER_UNKNOWN,
    Identity,
)

#: Evaluation instant: 2026-08-16T02:00:00Z, three hours before the captured payload's
#: 5-hour reset and ~3.3 days before its weekly reset.
NOW = 1786845600.0

#: ``resets_at`` of the two window families in :func:`usage_payload`, as epoch seconds.
FIVE_HOUR_RESETS_AT_S = 1786856399.642936
SEVEN_DAY_RESETS_AT_S = 1787129999.642959
FABLE_RESETS_AT_S = 1787129999.643181

#: An access token shaped like the real thing but obviously fake. Never a credential.
ACCESS_TOKEN = "sk-ant-oat01-not-a-real-token"

#: Anonymized identities, in the order the three local config directories are declared.
IDENTITIES = (
    ("acct1@example.com", "00000000-0000-4000-8000-000000000001"),
    ("acct2@example.com", "00000000-0000-4000-8000-000000000002"),
    ("acct3@example.com", "00000000-0000-4000-8000-000000000003"),
)


class NetworkAttempted(BaseException):
    """Raised by the guard below. A ``BaseException`` so nothing can catch it."""


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Hard guarantee: no test in this module may open a socket."""

    def explode(*args: Any, **kwargs: Any) -> None:
        raise NetworkAttempted(f"a test tried to reach the network: {args!r}")

    monkeypatch.setattr(urllib.request, "urlopen", explode)
    yield


# ======================================================================================
# Helpers
# ======================================================================================


class FakeRunner:
    """Stand-in for :func:`subprocess.run` that records how it was called."""

    def __init__(
        self,
        stdout: str = "",
        *,
        returncode: int = 0,
        stderr: str = "",
        raises: BaseException | None = None,
    ) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.raises = raises
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), dict(kwargs)))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


class FakeKeychain:
    """``security find-generic-password`` keyed by the ``-s`` service name.

    A service that is not in ``entries`` answers exactly like the real tool does when the
    item is absent: exit 44 with the "could not be found" message on stderr.
    """

    NOT_FOUND = 44

    def __init__(self, entries: Mapping[str, str]) -> None:
        self.entries = dict(entries)
        self.calls: list[list[str]] = []

    @property
    def services(self) -> list[str]:
        return [argv[argv.index("-s") + 1] for argv in self.calls if "-s" in argv]

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        service = argv[argv.index("-s") + 1] if "-s" in argv else ""
        if service in self.entries:
            return subprocess.CompletedProcess(argv, 0, self.entries[service], "")
        return subprocess.CompletedProcess(
            argv,
            self.NOT_FOUND,
            "",
            "security: SecKeychainSearchCopyNext: The specified item could not be found "
            "in the keychain.\n",
        )


class FakeResponse:
    """The minimum of ``http.client.HTTPResponse`` that the adapter uses."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False


class FakeOpener:
    """Stand-in for :func:`urllib.request.urlopen` that records the requests it saw."""

    def __init__(
        self,
        payload: Any = None,
        *,
        body: bytes | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self.payload = payload
        self.body = body
        self.raises = raises
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[Any] = []

    def __call__(self, request: urllib.request.Request, timeout: Any = None) -> FakeResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.raises is not None:
            raise self.raises
        if self.body is not None:
            return FakeResponse(self.body)
        return FakeResponse(json.dumps(self.payload).encode("utf-8"))


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(USAGE_URL, code, "nope", {}, None)  # type: ignore[arg-type]


def keychain_blob(
    *, access_token: str | None = ACCESS_TOKEN, expires_at_s: float | None = None, **extra: Any
) -> str:
    """One Keychain entry as Claude Code writes it, minus anything credential-shaped.

    Claude Code stores ``expiresAt`` in **milliseconds**; keeping that unit here is the
    point -- reading it as seconds would date every token to 1970 and mark every account
    expired.
    """
    oauth: dict[str, Any] = {"scopes": ["user:inference", "user:profile"]}
    if access_token is not None:
        oauth["accessToken"] = access_token
    if expires_at_s is not None:
        oauth["expiresAt"] = expires_at_s * 1000.0
    oauth.update(extra)
    return json.dumps({"claudeAiOauth": oauth})


def usage_payload(**overrides: Any) -> dict[str, Any]:
    """A trimmed real ``GET /api/oauth/usage`` response.

    ``limits[]`` is the only part the adapter reads; the top-level ``five_hour`` /
    ``seven_day`` blocks are kept because the real payload has them and a parser that
    quietly started preferring them would be a regression worth catching.
    """
    payload: dict[str, Any] = {
        "five_hour": {"utilization": 0.0, "resets_at": "2026-08-16T04:59:59.642936+00:00"},
        "seven_day": {"utilization": 74.0, "resets_at": "2026-08-19T08:59:59.642959+00:00"},
        "limits": [
            {
                "kind": "session",
                "group": "session",
                "percent": 0,
                "severity": "normal",
                "resets_at": "2026-08-16T04:59:59.642936+00:00",
                "scope": None,
            },
            {
                "kind": "weekly_all",
                "group": "weekly",
                "percent": 74,
                "severity": "normal",
                "resets_at": "2026-08-19T08:59:59.642959+00:00",
                "scope": None,
            },
            {
                "kind": "weekly_scoped",
                "group": "weekly",
                "percent": 95,
                "severity": "critical",
                "resets_at": "2026-08-19T08:59:59.643181+00:00",
                "scope": {"model": {"id": None, "display_name": "Fable"}},
            },
        ],
    }
    payload.update(overrides)
    return payload


def make_config(
    home: Path,
    *,
    account_id: str = ACCOUNT_CLAUDE,
    dir_name: str = ".claude",
    index: int = 0,
    tier: str = TIER_MAX_20X,
    create: bool = True,
) -> ClaudeAccountConfig:
    """A resolved config whose directory really exists under ``home``."""
    config_dir = home / dir_name
    if create:
        config_dir.mkdir(parents=True, exist_ok=True)
    return ClaudeAccountConfig(
        account_id=account_id,
        config_dir=config_dir,
        identity=Identity(*IDENTITIES[index]),
        tier=tier,
    )


def healthy_adapter(
    home: Path, *, configs: list[ClaudeAccountConfig] | None = None, payload: Any = None
) -> tuple[ClaudeOAuthAdapter, FakeKeychain, FakeOpener]:
    """An adapter wired to a live-looking Keychain entry and usage endpoint."""
    resolved = configs if configs is not None else [make_config(home)]
    keychain = FakeKeychain(
        {
            service: keychain_blob(expires_at_s=NOW + 3600)
            for config in resolved
            for service in keychain_service_for(config.config_dir, home=home)
        }
    )
    opener = FakeOpener(usage_payload() if payload is None else payload)
    adapter = ClaudeOAuthAdapter(runner=keychain, opener=opener, home=home, configs=resolved)
    return adapter, keychain, opener


def write_claude_config(
    home: Path,
    dir_name: str,
    *,
    email: str,
    uuid: str,
    tier: str | None,
    sibling: bool = False,
) -> Path:
    """Write a ``.claude.json`` in either supported layout and return its path."""
    config_dir = home / dir_name
    config_dir.mkdir(parents=True, exist_ok=True)
    path = (home / f"{dir_name}.json") if sibling else (config_dir / ".claude.json")
    account: dict[str, Any] = {
        "emailAddress": email,
        "organizationUuid": uuid,
        "organizationName": f"{email}'s Organization",
    }
    if tier is not None:
        account["organizationRateLimitTier"] = tier
    path.write_text(json.dumps({"oauthAccount": account}), encoding="utf-8")
    return path


def write_statusline(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def token_count_line(
    *,
    timestamp: str,
    limit_id: str = "codex",
    limit_name: str | None = None,
    used_percent: float | None = 55.0,
    window_minutes: int | None = 10080,
    resets_at: int = 1787196557,
    plan_type: str | None = "pro",
    primary_null: bool = False,
) -> str:
    """One real-shaped ``event_msg``/``token_count`` transcript line."""
    primary = (
        None
        if primary_null
        else {
            "used_percent": used_percent,
            "window_minutes": window_minutes,
            "resets_at": resets_at,
        }
    )
    return json.dumps(
        {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"total_tokens": 1}},
                "rate_limits": {
                    "limit_id": limit_id,
                    "limit_name": limit_name,
                    "primary": primary,
                    "secondary": None,
                    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                    "plan_type": plan_type,
                },
            },
        }
    )


def write_session(day_dir: Path, name: str, lines: list[str], *, mtime: float | None = None) -> Path:
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def fake_id_token(email: str, account_id: str, plan: str) -> str:
    """A JWT-shaped string whose payload segment carries the auth claims."""

    def segment(data: dict[str, Any]) -> str:
        raw = json.dumps(data).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    header = segment({"alg": "RS256", "typ": "JWT"})
    payload = segment(
        {
            "email": email,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": account_id,
                "chatgpt_plan_type": plan,
            },
        }
    )
    return f"{header}.{payload}.not-a-real-signature"


# ======================================================================================
# Protocol conformance
# ======================================================================================


def all_adapters(tmp_path: Path) -> list[Any]:
    adapter, _keychain, _opener = healthy_adapter(tmp_path)
    return [
        adapter,
        ClaudeStatuslineAdapter(paths={}),
        CodexSessionsAdapter(codex_home="/nonexistent"),
        AntigravityAdapter(which=lambda _binary: None),
    ]


def test_every_adapter_satisfies_the_protocol(tmp_path: Path) -> None:
    for adapter in all_adapters(tmp_path):
        assert isinstance(adapter, ProviderAdapter)
        assert adapter.snapshot(NOW) is not None


def test_every_adapter_declares_a_stable_name(tmp_path: Path) -> None:
    """``collect_snapshots`` and the ``--json`` diagnostics attribute warnings by name.

    Split out from the conformance test above so a missing ``name`` is reported as its
    own named failure rather than hiding inside "the protocol".
    """
    for adapter in all_adapters(tmp_path):
        assert isinstance(adapter.name, str) and adapter.name


# ======================================================================================
# base helpers
# ======================================================================================


def test_epoch_helpers_handle_the_units_that_actually_occur() -> None:
    # The statusline file mixes both units in one document. Tolerances are absolute:
    # pytest's default *relative* tolerance at epoch magnitudes is ~1800 seconds.
    assert coerce_epoch_seconds(1786734090784) == pytest.approx(1786734090.784, abs=1e-3)
    assert coerce_epoch_seconds(1786752000) == 1786752000
    assert coerce_epoch_seconds(True) is None
    assert coerce_epoch_seconds("nope") is None
    assert parse_iso8601("2026-08-14T18:46:52Z") == 1786733212.0
    assert parse_iso8601("2026-08-14T19:00:00.035216+00:00") == pytest.approx(
        1786734000.035216, abs=1e-6
    )
    assert parse_iso8601("not a date") is None


def test_pct_clamps_toward_fully_used_instead_of_dropping_the_window() -> None:
    # Clamping high is the conservative direction: it can only make an account look
    # worse. Dropping the window would delete a constraint.
    assert pct_to_fraction(120) == 1.0
    assert pct_to_fraction(-5) == 0.0
    assert pct_to_fraction(60) == pytest.approx(0.6)
    assert pct_to_fraction(None) is None


def test_confidence_decay_is_linear_between_fresh_and_stale() -> None:
    assert decay_confidence(0, fresh_s=100, stale_s=300) == 1.0
    assert decay_confidence(-50, fresh_s=100, stale_s=300) == 1.0
    assert decay_confidence(200, fresh_s=100, stale_s=300) == pytest.approx(0.6)
    assert decay_confidence(9999, fresh_s=100, stale_s=300) == pytest.approx(0.2)


def test_read_tail_lines_reads_only_the_tail(tmp_path: Path) -> None:
    path = tmp_path / "big.jsonl"
    path.write_text("HEAD\n" + ("x" * 5000) + "\nTAIL\n", encoding="utf-8")
    lines = read_tail_lines(path, max_bytes=64)
    assert "HEAD" not in lines
    assert lines[-1] == "TAIL"


# ======================================================================================
# claude_oauth -- Keychain service naming
# ======================================================================================


def test_keychain_service_for_puts_the_unsuffixed_name_first_only_for_the_default_dir() -> None:
    """Verified against all three of this operator's real accounts.

    The default directory's entry is unsuffixed; every other directory is suffixed with
    the first eight hex digits of the SHA-256 of its absolute path.

    The default gets a suffixed fallback because the unsuffixed name is an observed
    convention rather than a documented contract. A SLOT gets no unsuffixed fallback:
    it would read the default account's credentials, which misattributes one account's
    usage to another rather than reporting the slot as logged out.
    """
    home = Path("/Users/tonygwu")

    default = keychain_service_for(home / ".claude", home=home)
    assert default[0] == KEYCHAIN_SERVICE_BASE
    assert default[1].startswith(f"{KEYCHAIN_SERVICE_BASE}-")

    for dir_name, digest in ((".claude-b", "6bf31a73"), (".claude-c", "8af63c1d")):
        candidates = keychain_service_for(home / dir_name, home=home)
        assert candidates == (f"{KEYCHAIN_SERVICE_BASE}-{digest}",)


def test_keychain_service_names_are_per_directory_and_expand_a_tilde() -> None:
    home = Path("/Users/tonygwu")
    assert keychain_service_for("/Users/tonygwu/.claude-b", home=home) == keychain_service_for(
        Path("/Users/tonygwu/.claude-b"), home=home
    )
    # Two different directories must never collide onto one Keychain entry.
    assert keychain_service_for(home / ".claude-b", home=home)[0] != keychain_service_for(
        home / ".claude-c", home=home
    )[0]


def test_the_default_dir_tries_its_second_candidate_when_the_first_misses(tmp_path: Path) -> None:
    """Only the default directory has a second candidate to try.

    A slot directory deliberately has exactly one, so there is nothing to fall
    through to -- falling through would read the default account's credentials.
    """
    config_dir = tmp_path / ".claude"
    first, second = keychain_service_for(config_dir, home=tmp_path)
    keychain = FakeKeychain({second: keychain_blob(expires_at_s=NOW + 3600)})

    read = read_access_token(config_dir, runner=keychain, home=tmp_path, now_s=NOW)

    assert read.usable is True
    assert read.token == ACCESS_TOKEN
    assert read.service == second
    assert keychain.services == [first, second]


def test_a_slot_dir_never_reaches_the_default_accounts_entry(tmp_path: Path) -> None:
    config_dir = tmp_path / ".claude-b"
    keychain = FakeKeychain({KEYCHAIN_SERVICE_BASE: keychain_blob(expires_at_s=NOW + 3600)})

    read = read_access_token(config_dir, runner=keychain, home=tmp_path, now_s=NOW)

    assert read.usable is False, "a logged-out slot must not borrow account A's token"
    assert KEYCHAIN_SERVICE_BASE not in keychain.services


# ======================================================================================
# claude_oauth -- the read is read-only, by construction
# ======================================================================================


def test_oauth_runs_exactly_one_read_only_keychain_command(tmp_path: Path) -> None:
    adapter, keychain, _opener = healthy_adapter(tmp_path)
    adapter.snapshot(NOW)

    assert len(keychain.calls) == 1
    argv = keychain.calls[0]
    assert argv == [
        "security",
        "find-generic-password",
        "-s",
        KEYCHAIN_SERVICE_BASE,
        "-w",
    ]
    # The mutating verbs must never appear: this package reads credentials, never writes
    # or deletes them.
    assert not {"add-generic-password", "delete-generic-password", "-U", "set-generic-password"} & set(argv)


def test_oauth_asks_the_usage_endpoint_with_a_bearer_get_and_no_body(tmp_path: Path) -> None:
    """The structural half of "we never rotate": a GET with no request body.

    A token exchange is necessarily a POST with a form body. There is no code path here
    that can produce one, and this asserts the shape rather than trusting the intent.
    """
    adapter, _keychain, opener = healthy_adapter(tmp_path)
    adapter.snapshot(NOW)

    assert len(opener.requests) == 1
    request = opener.requests[0]
    assert request.full_url == USAGE_URL
    assert request.get_method() == HTTP_METHOD == "GET"
    assert request.data is None
    assert request.get_header("Authorization") == f"Bearer {ACCESS_TOKEN}"
    assert request.get_header("Accept") == "application/json"
    assert opener.timeouts == [10.0]


# ======================================================================================
# claude_oauth -- the healthy path
# ======================================================================================


def test_oauth_maps_a_real_usage_payload_onto_windows(tmp_path: Path) -> None:
    adapter, _keychain, _opener = healthy_adapter(tmp_path)
    snapshots = adapter.snapshot(NOW)

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.id == ACCOUNT_CLAUDE
    assert snapshot.provider == "claude"
    assert snapshot.source == SOURCE_LIVE
    assert snapshot.available is True
    assert snapshot.confidence == pytest.approx(1.0)
    assert snapshot.tier == TIER_MAX_20X and snapshot.capacity == 1.0
    assert snapshot.identity is not None
    assert snapshot.identity.email == "acct1@example.com"

    assert [window.key for window in snapshot.windows] == ["5h", "7d", "fable"]
    five_hour, seven_day, fable = snapshot.windows
    assert five_hour.used_fraction == pytest.approx(0.0)
    assert seven_day.used_fraction == pytest.approx(0.74)
    assert fable.used_fraction == pytest.approx(0.95)
    assert five_hour.length_s == 5 * 3600
    assert seven_day.length_s == 7 * 86400
    assert fable.length_s == 7 * 86400
    assert five_hour.resets_at_s == pytest.approx(FIVE_HOUR_RESETS_AT_S, abs=1e-3)
    assert seven_day.resets_at_s == pytest.approx(SEVEN_DAY_RESETS_AT_S, abs=1e-3)
    assert fable.resets_at_s == pytest.approx(FABLE_RESETS_AT_S, abs=1e-3)
    assert all(window.observed_at_s == NOW for window in snapshot.windows)


def test_oauth_window_model_gating_follows_the_scoped_row(tmp_path: Path) -> None:
    adapter, _keychain, _opener = healthy_adapter(tmp_path)
    snapshot = adapter.snapshot(NOW)[0]

    five_hour, seven_day, fable = snapshot.windows
    assert five_hour.applies_to is None and seven_day.applies_to is None
    assert fable.applies_to == frozenset({"fable"})

    # A scoped window governs only its own class; asking about another class skips it.
    assert [w.key for w in snapshot.applicable_windows("fable")] == ["5h", "7d", "fable"]
    assert [w.key for w in snapshot.applicable_windows("sonnet")] == ["5h", "7d"]
    # ...and skipping it changes which window binds.
    assert snapshot.binding_window_key(NOW, "fable") == "fable"
    assert snapshot.binding_window_key(NOW, "sonnet") == "7d"


def test_oauth_windows_derive_their_pacing_baseline_from_the_reset_time(tmp_path: Path) -> None:
    """The usage endpoint publishes no ``expectedPct``; the uniform prior has to fill in.

    The retired oracle shipped a baseline of its own. This endpoint does not, so every
    window here derives one from ``resets_at`` and the window length -- and that must be
    true of the *weekly* windows too, not just the 5-hour one.
    """
    adapter, _keychain, _opener = healthy_adapter(tmp_path)
    snapshot = adapter.snapshot(NOW)[0]
    assert all(window.expected_used_fraction is None for window in snapshot.windows)

    five_hour = snapshot.window("5h")
    assert five_hour is not None
    elapsed_5h = 1.0 - (FIVE_HOUR_RESETS_AT_S - NOW) / (5 * 3600)
    assert five_hour.expected_used_at(NOW) == pytest.approx(elapsed_5h)
    assert five_hour.slack(NOW) == pytest.approx((1 - 0.0) - (1 - elapsed_5h))
    assert five_hour.ahead_of_pace(NOW) is False  # idle window, plenty of slack

    seven_day = snapshot.window("7d")
    assert seven_day is not None
    elapsed_7d = 1.0 - (SEVEN_DAY_RESETS_AT_S - NOW) / (7 * 86400)
    assert seven_day.slack(NOW) == pytest.approx((1 - 0.74) - (1 - elapsed_7d))
    assert seven_day.ahead_of_pace(NOW) is True

    fable = snapshot.window("fable")
    assert fable is not None
    assert fable.slack(NOW) == pytest.approx((1 - 0.95) - (1 - elapsed_7d))
    # Fable is the tightest applicable window, so it sets the account's min slack.
    assert snapshot.min_slack(NOW, "fable") == pytest.approx(fable.slack(NOW))
    assert snapshot.min_slack(NOW, "sonnet") == pytest.approx(seven_day.slack(NOW))


def test_oauth_reads_every_configured_account_independently(tmp_path: Path) -> None:
    configs = [
        make_config(tmp_path, account_id=ACCOUNT_CLAUDE, dir_name=".claude", index=0),
        make_config(tmp_path, account_id=ACCOUNT_CLAUDE_B, dir_name=".claude-b", index=1),
        make_config(
            tmp_path,
            account_id=ACCOUNT_CLAUDE_C,
            dir_name=".claude-c",
            index=2,
            tier=TIER_MAX_5X,
        ),
    ]
    adapter, keychain, opener = healthy_adapter(tmp_path, configs=configs)
    snapshots = adapter.snapshot(NOW)

    assert [snapshot.id for snapshot in snapshots] == [
        ACCOUNT_CLAUDE,
        ACCOUNT_CLAUDE_B,
        ACCOUNT_CLAUDE_C,
    ]
    assert [snapshot.capacity for snapshot in snapshots] == [1.0, 1.0, 0.25]
    assert all(snapshot.source == SOURCE_LIVE for snapshot in snapshots)
    assert [snapshot.identity.email for snapshot in snapshots if snapshot.identity] == [
        "acct1@example.com",
        "acct2@example.com",
        "acct3@example.com",
    ]
    # One Keychain read and one HTTP GET per account -- never a shared token.
    assert keychain.services == [
        KEYCHAIN_SERVICE_BASE,
        keychain_service_for(tmp_path / ".claude-b", home=tmp_path)[0],
        keychain_service_for(tmp_path / ".claude-c", home=tmp_path)[0],
    ]
    assert len(opener.requests) == 3


def test_oauth_never_emits_two_snapshots_for_one_account_id(tmp_path: Path) -> None:
    """One pool, one candidate. A duplicate id would double-count it in the ranking."""
    configs = [
        make_config(tmp_path, account_id=ACCOUNT_CLAUDE, dir_name=".claude", index=0),
        make_config(tmp_path, account_id=ACCOUNT_CLAUDE_B, dir_name=".claude-b", index=1),
    ]
    adapter, _keychain, _opener = healthy_adapter(tmp_path, configs=configs)

    ids = [snapshot.id for snapshot in adapter.snapshot(NOW)]
    assert len(ids) == len(set(ids))
    assert set(ids) == {ACCOUNT_CLAUDE, ACCOUNT_CLAUDE_B}


def test_oauth_unknown_tier_keeps_capacity_neutral_and_lowers_confidence(tmp_path: Path) -> None:
    config = make_config(tmp_path, tier=TIER_UNKNOWN)
    adapter, _keychain, _opener = healthy_adapter(tmp_path, configs=[config])
    snapshot = adapter.snapshot(NOW)[0]

    assert snapshot.tier == TIER_UNKNOWN
    assert snapshot.capacity == 1.0  # neutral, per the contract
    assert snapshot.confidence < 1.0
    assert snapshot.available is True  # an unknown tier is not a reason to skip the pool
    assert "tier unknown" in (snapshot.note or "")


# ======================================================================================
# claude_oauth -- the ways an account goes dark
# ======================================================================================


def test_a_blank_access_token_is_reported_as_logged_out_not_as_no_data(tmp_path: Path) -> None:
    """The exact shape a rotation casualty leaves behind.

    When Claude Code's refresh fails it *blanks* the token in place rather than deleting
    the Keychain entry. A parser that treats an empty string as "nothing to see" turns
    two destroyed logins into a silent absence; this must surface as an unavailable
    account with a note the operator can act on.
    """
    config = make_config(tmp_path)
    keychain = FakeKeychain({KEYCHAIN_SERVICE_BASE: keychain_blob(access_token="")})
    opener = FakeOpener(usage_payload())
    adapter = ClaudeOAuthAdapter(runner=keychain, opener=opener, home=tmp_path, configs=[config])

    snapshots = adapter.snapshot(NOW)

    assert len(snapshots) == 1  # present and unavailable, never absent
    snapshot = snapshots[0]
    assert snapshot.available is False
    assert snapshot.windows == ()
    assert snapshot.confidence == 0.0
    assert snapshot.source == SOURCE_LIVE
    note = snapshot.note or ""
    assert "acct1@example.com" in note  # which account went dark
    assert "logged out" in note
    assert "/login" in note  # ...and what to do about it
    assert opener.requests == []  # a blank token is never presented to the endpoint

    # The same fact, at the level that decides it.
    read = read_access_token(config.config_dir, runner=keychain, home=tmp_path, now_s=NOW)
    assert read.usable is False
    assert read.token is None
    assert "logged out" in (read.problem or "")


def test_an_expired_access_token_is_reported_without_ever_calling_the_endpoint(
    tmp_path: Path,
) -> None:
    """Expiry is a refusal, not a trigger to mint a replacement.

    Minting one is precisely the operation that destroyed two logins. The account goes
    unavailable and stays that way until its own traffic refreshes it.
    """
    config = make_config(tmp_path)
    keychain = FakeKeychain(
        {KEYCHAIN_SERVICE_BASE: keychain_blob(expires_at_s=NOW - 60)}  # one minute stale
    )
    opener = FakeOpener(usage_payload())
    adapter = ClaudeOAuthAdapter(runner=keychain, opener=opener, home=tmp_path, configs=[config])

    snapshot = adapter.snapshot(NOW)[0]

    assert snapshot.available is False
    assert snapshot.windows == ()
    assert "expired" in (snapshot.note or "")
    assert opener.requests == [], "an expired token must not be presented to the endpoint"
    assert len(keychain.calls) == 1  # and no second look for a fresher entry


def test_a_token_that_is_still_valid_is_not_treated_as_expired(tmp_path: Path) -> None:
    """The boundary in the other direction: ``expiresAt`` is milliseconds, not seconds."""
    config = make_config(tmp_path)
    keychain = FakeKeychain({KEYCHAIN_SERVICE_BASE: keychain_blob(expires_at_s=NOW + 1)})
    opener = FakeOpener(usage_payload())
    adapter = ClaudeOAuthAdapter(runner=keychain, opener=opener, home=tmp_path, configs=[config])

    snapshot = adapter.snapshot(NOW)[0]
    assert snapshot.available is True
    assert len(opener.requests) == 1

    read = read_access_token(config.config_dir, runner=keychain, home=tmp_path, now_s=NOW)
    assert read.expires_at_s == pytest.approx(NOW + 1)


def test_a_token_with_no_expiry_is_used_rather_than_assumed_dead(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    keychain = FakeKeychain({KEYCHAIN_SERVICE_BASE: keychain_blob()})  # no expiresAt
    opener = FakeOpener(usage_payload())
    adapter = ClaudeOAuthAdapter(runner=keychain, opener=opener, home=tmp_path, configs=[config])

    snapshot = adapter.snapshot(NOW)[0]
    assert snapshot.available is True
    assert [window.key for window in snapshot.windows] == ["5h", "7d", "fable"]


def test_http_401_surfaces_the_rejection_instead_of_retrying(tmp_path: Path) -> None:
    """A 401 means the token is dead. The only safe response is to say so."""
    config = make_config(tmp_path)
    keychain = FakeKeychain({KEYCHAIN_SERVICE_BASE: keychain_blob(expires_at_s=NOW + 3600)})
    opener = FakeOpener(raises=http_error(401))
    adapter = ClaudeOAuthAdapter(runner=keychain, opener=opener, home=tmp_path, configs=[config])

    snapshot = adapter.snapshot(NOW)[0]

    assert snapshot.available is False
    assert snapshot.windows == ()
    assert snapshot.confidence == 0.0
    note = snapshot.note or ""
    assert "401" in note and "token rejected" in note
    assert "acct1@example.com" in note
    assert len(opener.requests) == 1, "a rejected token must not be retried"


@pytest.mark.parametrize(
    ("keychain_stdout", "opener", "expected_fragment"),
    [
        pytest.param(
            keychain_blob(expires_at_s=NOW + 3600),
            FakeOpener(raises=http_error(500)),
            "HTTP 500",
            id="server-error",
        ),
        pytest.param(
            keychain_blob(expires_at_s=NOW + 3600),
            FakeOpener(raises=urllib.error.URLError("no route to host")),
            "URLError",
            id="network-down",
        ),
        pytest.param(
            keychain_blob(expires_at_s=NOW + 3600),
            FakeOpener(body=b"not json at all"),
            "unparseable usage payload",
            id="garbage-body",
        ),
        pytest.param(
            keychain_blob(expires_at_s=NOW + 3600),
            FakeOpener({"limits": []}),
            "no usable window",
            id="empty-limits",
        ),
        pytest.param(
            keychain_blob(expires_at_s=NOW + 3600),
            FakeOpener(["not", "an", "object"]),
            "not a JSON object",
            id="payload-not-an-object",
        ),
        pytest.param("not json at all", FakeOpener(), "not JSON", id="keychain-entry-corrupt"),
        pytest.param(
            json.dumps({"somethingElse": {}}),
            FakeOpener(),
            "no claudeAiOauth block",
            id="keychain-entry-reshaped",
        ),
    ],
)
def test_oauth_degrades_visibly_instead_of_raising(
    tmp_path: Path, keychain_stdout: str, opener: FakeOpener, expected_fragment: str
) -> None:
    config = make_config(tmp_path)
    keychain = FakeKeychain({KEYCHAIN_SERVICE_BASE: keychain_stdout})
    adapter = ClaudeOAuthAdapter(runner=keychain, opener=opener, home=tmp_path, configs=[config])

    snapshots = adapter.snapshot(NOW)

    assert len(snapshots) == 1
    assert snapshots[0].available is False
    assert snapshots[0].windows == ()
    assert expected_fragment in (snapshots[0].note or "")


@pytest.mark.parametrize(
    ("runner", "expected_note"),
    [
        # The tool never ran. This is an environment fault, and it must NOT read as
        # "this account has no entry" -- that phrasing sends the operator off to
        # re-login an account that was never logged out.
        pytest.param(
            FakeRunner(raises=FileNotFoundError("security")),
            "could not run `security`",
            id="binary-missing",
        ),
        pytest.param(
            FakeRunner(raises=subprocess.TimeoutExpired(cmd="security", timeout=10.0)),
            "could not run `security`",
            id="keychain-hung",
        ),
        # The tool ran and answered honestly: there is nothing stored here.
        pytest.param(
            FakeRunner("", returncode=44, stderr="not found"),
            "no Keychain entry found",
            id="no-entry",
        ),
        pytest.param(FakeRunner("   \n"), "no Keychain entry found", id="entry-empty"),
    ],
)
def test_a_keychain_that_cannot_answer_leaves_the_account_unavailable(
    tmp_path: Path, runner: FakeRunner, expected_note: str
) -> None:
    config = make_config(tmp_path)
    opener = FakeOpener(usage_payload())
    adapter = ClaudeOAuthAdapter(runner=runner, opener=opener, home=tmp_path, configs=[config])

    snapshot = adapter.snapshot(NOW)[0]

    assert snapshot.available is False
    assert expected_note in (snapshot.note or "")
    assert opener.requests == []
    # Both candidate service names were tried before giving up.
    assert len(runner.calls) == 2


def test_oauth_skips_a_config_dir_that_does_not_exist(tmp_path: Path) -> None:
    """An account we cannot spawn a CLI for must not be offered as a candidate at all."""
    config = ClaudeAccountConfig(
        account_id=ACCOUNT_CLAUDE_C,
        config_dir=tmp_path / "never-created",
        identity=Identity(*IDENTITIES[2]),
        tier=TIER_MAX_5X,
    )
    keychain = FakeKeychain({})
    opener = FakeOpener(usage_payload())
    adapter = ClaudeOAuthAdapter(runner=keychain, opener=opener, home=tmp_path, configs=[config])

    assert adapter.snapshot(NOW) == []
    assert keychain.calls == []
    assert opener.requests == []


# ======================================================================================
# claude_oauth -- payload parsing
# ======================================================================================


def test_parse_usage_payload_clamps_an_out_of_range_percentage() -> None:
    payload = usage_payload()
    payload["limits"][1]["percent"] = 130
    windows, warnings = parse_usage_payload(payload, observed_at_s=NOW)

    seven_day = next(window for window in windows if window.key == "7d")
    assert seven_day.used_fraction == 1.0
    assert seven_day.remaining_fraction == 0.0
    assert seven_day.is_exhausted
    assert warnings == []


def test_parse_usage_payload_keeps_an_unnameable_scoped_row_as_a_constraint() -> None:
    """Dropping a scoped row we cannot name would delete a real limit and flatter us."""
    payload = usage_payload()
    payload["limits"][2]["scope"] = {"model": {"id": None, "display_name": None}}
    windows, _warnings = parse_usage_payload(payload, observed_at_s=NOW)

    assert [window.key for window in windows] == ["5h", "7d", "scoped"]
    scoped = windows[2]
    assert scoped.applies_to is None  # account-wide, i.e. it constrains everything
    assert scoped.applies("sonnet") is True


def test_parse_usage_payload_survives_rows_it_cannot_use() -> None:
    payload = usage_payload()
    payload["limits"].extend(
        [
            {"kind": "something_new", "percent": 5, "resets_at": "2026-08-19T08:59:59Z"},
            {"kind": "weekly_scoped", "percent": "???", "scope": None},
            "not even a mapping",
        ]
    )
    windows, warnings = parse_usage_payload(payload, observed_at_s=NOW)

    assert [window.key for window in windows] == ["5h", "7d", "fable"]
    joined = " ".join(warnings)
    assert "unrecognized limits[] kind" in joined
    assert "lacked percent or resets_at" in joined


def test_parse_usage_payload_disambiguates_two_rows_that_want_the_same_key() -> None:
    """Two scoped rows for the same model must both survive, not silently collapse."""
    payload = usage_payload()
    payload["limits"].append(
        {
            "kind": "weekly_scoped",
            "percent": 12,
            "resets_at": "2026-08-19T08:59:59.643181+00:00",
            "scope": {"model": {"id": None, "display_name": "Fable"}},
        }
    )
    windows, _warnings = parse_usage_payload(payload, observed_at_s=NOW)

    keys = [window.key for window in windows]
    assert len(keys) == len(set(keys))
    assert keys[:3] == ["5h", "7d", "fable"]
    assert keys[3].startswith("fable#")


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("not a mapping", "usage payload is not a JSON object"),
        ({"no_limits_key": 1}, "usage payload has no limits[] array"),
        ({"limits": "a string is a Sequence"}, "usage payload has no limits[] array"),
    ],
)
def test_parse_usage_payload_refuses_a_shape_it_does_not_understand(
    payload: Any, expected: str
) -> None:
    windows, warnings = parse_usage_payload(payload, observed_at_s=NOW)
    assert windows == []
    assert warnings == [expected]


# ======================================================================================
# claude_cli_config -- identity / tier discovery
# ======================================================================================


def test_discovery_reads_tiers_from_both_claude_json_layouts(tmp_path: Path) -> None:
    """``~/.claude.json`` (sibling) and ``~/.claude-b/.claude.json`` (inside) both count."""
    write_claude_config(
        tmp_path, ".claude", email="acct1@example.com", uuid=IDENTITIES[0][1],
        tier="default_claude_max_20x", sibling=True,
    )
    write_claude_config(
        tmp_path, ".claude-b", email="acct2@example.com", uuid=IDENTITIES[1][1],
        tier="default_claude_max_20x",
    )
    write_claude_config(
        tmp_path, ".claude-c", email="acct3@example.com", uuid=IDENTITIES[2][1],
        tier="default_claude_max_5x",
    )

    configs = {config.account_id: config for config in discover_claude_configs(home=tmp_path)}
    assert configs[ACCOUNT_CLAUDE].tier == TIER_MAX_20X
    assert configs[ACCOUNT_CLAUDE].source_path == tmp_path / ".claude.json"
    assert configs[ACCOUNT_CLAUDE].identity is not None
    assert configs[ACCOUNT_CLAUDE].identity.email == "acct1@example.com"
    assert configs[ACCOUNT_CLAUDE_C].tier == TIER_MAX_5X
    assert configs[ACCOUNT_CLAUDE_C].source_path == tmp_path / ".claude-c" / ".claude.json"


def test_discovery_returns_unresolved_directories_rather_than_hiding_them(tmp_path: Path) -> None:
    """"Not set up" and "never configured" are different states the caller must see."""
    write_claude_config(
        tmp_path, ".claude", email="acct1@example.com", uuid=IDENTITIES[0][1], tier=None
    )
    configs = {config.account_id: config for config in discover_claude_configs(home=tmp_path)}

    assert set(configs) == {ACCOUNT_CLAUDE, ACCOUNT_CLAUDE_B, ACCOUNT_CLAUDE_C, ACCOUNT_CLAUDE_D}
    assert configs[ACCOUNT_CLAUDE].resolved is True
    assert configs[ACCOUNT_CLAUDE].tier == TIER_UNKNOWN  # present, but no tier recorded
    assert configs[ACCOUNT_CLAUDE_B].resolved is False
    assert configs[ACCOUNT_CLAUDE_B].identity is None


def test_discovery_honours_the_env_override(tmp_path: Path) -> None:
    write_claude_config(
        tmp_path, "custom-a", email="acct2@example.com", uuid=IDENTITIES[1][1],
        tier="default_claude_max_5x",
    )
    env = {
        "QUOTA_ROUTER_CLAUDE_CONFIG_DIRS": json.dumps(
            {ACCOUNT_CLAUDE_B: str(tmp_path / "custom-a")}
        )
    }
    configs = discover_claude_configs(home=tmp_path, env=env)

    assert [config.account_id for config in configs] == [ACCOUNT_CLAUDE_B]
    assert configs[0].config_dir == tmp_path / "custom-a"
    assert configs[0].tier == TIER_MAX_5X


# ======================================================================================
# claude_statusline
# ======================================================================================


def statusline_payload(**overrides: Any) -> dict[str, Any]:
    """The verified real shape: ``observedAt`` in ms, ``resets_at`` in seconds."""
    payload: dict[str, Any] = {
        "observedAt": 1786734090784,
        "provider": "claude",
        "source": "claude_status_line",
        "rate_limits": {
            "five_hour": {"used_percentage": 0, "resets_at": 1786752000},
            "seven_day": {"used_percentage": 60, "resets_at": 1787007600},
        },
    }
    payload.update(overrides)
    return payload


def test_statusline_reads_the_real_cache_shape(tmp_path: Path) -> None:
    write_statusline(tmp_path / "claude-rate-limits.json", statusline_payload())
    adapter = ClaudeStatuslineAdapter(directory=tmp_path, home=tmp_path, env={})
    snapshots = adapter.snapshot(1786734090.784 + 10)

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.id == ACCOUNT_CLAUDE
    assert snapshot.source == SOURCE_CACHE
    assert [window.key for window in snapshot.windows] == ["5h", "7d"]
    five_hour, seven_day = snapshot.windows
    assert five_hour.length_s == 5 * 3600
    assert seven_day.length_s == 7 * 86400
    assert seven_day.used_fraction == pytest.approx(0.6)
    # observedAt is milliseconds; it must not be read as seconds (that would be 1926).
    assert five_hour.observed_at_s == pytest.approx(1786734090.784, abs=1e-3)


def test_statusline_passes_unknown_window_keys_through_as_model_scoped(tmp_path: Path) -> None:
    """A per-model key the writer adds later must work with no code change here."""
    payload = statusline_payload()
    payload["rate_limits"]["fable"] = {"used_percentage": 80, "resets_at": 1787007600}
    payload["rate_limits"]["opus_five_hour"] = {"used_percentage": 10, "resets_at": 1786752000}
    write_statusline(tmp_path / "claude-rate-limits.json", payload)

    snapshot = ClaudeStatuslineAdapter(directory=tmp_path, home=tmp_path, env={}).snapshot(
        1786734090.784
    )[0]
    windows = {window.key: window for window in snapshot.windows}

    assert windows["fable"].applies_to == frozenset({"fable"})
    assert windows["fable"].length_s == 7 * 86400
    assert windows["opus_five_hour"].applies_to == frozenset({"opus"})
    assert windows["opus_five_hour"].length_s == 5 * 3600
    # The account-wide windows stay unconstrained.
    assert windows["5h"].applies_to is None
    assert [w.key for w in snapshot.applicable_windows("fable")] == ["5h", "7d", "fable"]


def test_statusline_confidence_decays_with_age(tmp_path: Path) -> None:
    write_statusline(tmp_path / "claude-rate-limits.json", statusline_payload())
    write_claude_config(
        tmp_path, ".claude", email="acct1@example.com", uuid="u1", tier="default_claude_max_20x"
    )
    adapter = ClaudeStatuslineAdapter(directory=tmp_path, home=tmp_path, env={})
    observed = 1786734090.784

    fresh = adapter.snapshot(observed + 10)[0]
    middling = adapter.snapshot(observed + 960)[0]
    ancient = adapter.snapshot(observed + 86400)[0]

    assert fresh.confidence == pytest.approx(1.0)
    assert 0.2 < middling.confidence < 1.0
    assert ancient.confidence == pytest.approx(0.2)
    assert fresh.tier == TIER_MAX_20X  # tier still comes from .claude.json


def test_statusline_missing_or_malformed_files_produce_no_snapshot(tmp_path: Path) -> None:
    (tmp_path / "claude-b-rate-limits.json").write_text("{ truncated", encoding="utf-8")
    (tmp_path / "claude-c-rate-limits.json").write_text(
        json.dumps({"observedAt": 1, "rate_limits": {}}), encoding="utf-8"
    )
    adapter = ClaudeStatuslineAdapter(directory=tmp_path, home=tmp_path, env={})

    assert adapter.snapshot(NOW) == []  # claude-rate-limits.json does not exist at all
    assert any("no parseable window" in warning for warning in adapter.warnings)


def test_statusline_env_overrides_are_honoured(tmp_path: Path) -> None:
    target = write_statusline(tmp_path / "custom" / "one.json", statusline_payload())
    adapter = ClaudeStatuslineAdapter(
        env={"QUOTA_ROUTER_STATUSLINE_PATHS": json.dumps({ACCOUNT_CLAUDE_B: str(target)})},
        home=tmp_path,
    )
    snapshots = adapter.snapshot(1786734090.784)
    assert [snapshot.id for snapshot in snapshots] == [ACCOUNT_CLAUDE_B]


# ======================================================================================
# codex_sessions
# ======================================================================================


def test_codex_reads_the_verified_transcript_shape(tmp_path: Path) -> None:
    day = tmp_path / "sessions" / "2026" / "08" / "14"
    write_session(
        day,
        "rollout-2026-08-14T12-00-01.jsonl",
        [
            json.dumps({"type": "session_meta", "payload": {"id": "abc"}}),
            token_count_line(timestamp="2026-08-14T19:00:01.800Z", used_percent=55.0),
            token_count_line(timestamp="2026-08-14T19:05:01.800Z", used_percent=59.0),
        ],
    )
    adapter = CodexSessionsAdapter(codex_home=tmp_path, read_identity=False)
    snapshots = adapter.snapshot(NOW)

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.id == ACCOUNT_CODEX
    assert snapshot.provider == "codex"
    assert snapshot.tier == TIER_PRO and snapshot.capacity == 1.0
    assert [window.key for window in snapshot.windows] == ["7d"]
    window = snapshot.windows[0]
    assert window.length_s == 10080 * 60
    assert window.used_fraction == pytest.approx(0.59)  # newest row wins
    assert window.resets_at_s == 1787196557
    assert window.applies_to is None


def test_codex_maps_a_non_default_limit_id_to_a_model_scoped_window(tmp_path: Path) -> None:
    day = tmp_path / "sessions" / "2026" / "08" / "14"
    write_session(
        day,
        "rollout.jsonl",
        [
            token_count_line(timestamp="2026-08-14T19:00:00Z", limit_id="codex"),
            token_count_line(
                timestamp="2026-08-14T19:00:00Z",
                limit_id="codex_bengalfox",
                limit_name="GPT-5.3-Codex-Spark",
                used_percent=12.0,
            ),
            # "premium" reports primary: null -- no window at all.
            token_count_line(
                timestamp="2026-08-14T19:00:00Z",
                limit_id="premium",
                plan_type=None,
                primary_null=True,
            ),
        ],
    )
    adapter = CodexSessionsAdapter(codex_home=tmp_path, read_identity=False)
    snapshot = adapter.snapshot(NOW)[0]
    windows = {window.key: window for window in snapshot.windows}

    assert set(windows) == {"7d", "codex_bengalfox"}  # premium contributed nothing
    scoped = windows["codex_bengalfox"]
    assert scoped.applies_to is not None
    assert {"codex_bengalfox", "bengalfox", "gpt-5.3-codex-spark"} <= scoped.applies_to
    assert scoped.applies("gpt-5.3-codex-spark") is True
    assert scoped.applies("some-other-model") is False
    assert windows["7d"].applies("some-other-model") is True
    assert any("premium" in warning for warning in adapter.warnings)


def test_codex_reads_only_file_tails(tmp_path: Path) -> None:
    """A contradictory row past the tail window must not be seen -- proving tail-only I/O.

    The poison row is both *newer* and wildly different, so if the whole file were read
    it would win on timestamp and the assertion below would fail.
    """
    day = tmp_path / "sessions" / "2026" / "08" / "14"
    poison = token_count_line(timestamp="2099-01-01T00:00:00Z", used_percent=99.0)
    padding = [json.dumps({"type": "response_item", "payload": {"text": "x" * 200}}) for _ in range(200)]
    write_session(day, "rollout.jsonl", [poison, *padding, token_count_line(
        timestamp="2026-08-14T19:00:00Z", used_percent=42.0
    )])

    snapshot = CodexSessionsAdapter(
        codex_home=tmp_path, read_identity=False, tail_bytes=4096
    ).snapshot(NOW)[0]
    assert snapshot.windows[0].used_fraction == pytest.approx(0.42)


def test_codex_looks_at_only_the_newest_files(tmp_path: Path) -> None:
    day = tmp_path / "sessions" / "2026" / "08" / "14"
    base = 1786700000.0
    for index in range(10):
        limit_id = "codex_ancient" if index == 0 else "codex"
        write_session(
            day,
            f"rollout-{index:02d}.jsonl",
            [token_count_line(timestamp="2026-08-14T19:00:00Z", limit_id=limit_id)],
            mtime=base + index * 60,
        )

    snapshot = CodexSessionsAdapter(
        codex_home=tmp_path, read_identity=False, max_files=8
    ).snapshot(NOW)[0]
    assert all(window.key != "codex_ancient" for window in snapshot.windows)


def test_codex_walks_back_through_day_directories(tmp_path: Path) -> None:
    write_session(
        tmp_path / "sessions" / "2026" / "07" / "31",
        "old.jsonl",
        [token_count_line(timestamp="2026-07-31T10:00:00Z", used_percent=10.0)],
    )
    snapshot = CodexSessionsAdapter(codex_home=tmp_path, read_identity=False).snapshot(NOW)[0]
    assert snapshot.windows[0].used_fraction == pytest.approx(0.10)
    # More than a week old: confidence has decayed to the floor but the data is still
    # offered.
    assert snapshot.confidence == pytest.approx(0.2)


def test_codex_reads_identity_from_auth_json_without_leaking_the_token(tmp_path: Path) -> None:
    write_session(
        tmp_path / "sessions" / "2026" / "08" / "14",
        "rollout.jsonl",
        [token_count_line(timestamp="2026-08-14T19:00:00Z", plan_type=None)],
    )
    token = fake_id_token("codexuser@example.com", "acct-uuid-1", "pro")
    (tmp_path / "auth.json").write_text(
        json.dumps({"tokens": {"id_token": token, "access_token": "secret"}}), encoding="utf-8"
    )

    snapshot = CodexSessionsAdapter(codex_home=tmp_path).snapshot(NOW)[0]
    assert snapshot.identity is not None
    assert snapshot.identity.email == "codexuser@example.com"
    assert snapshot.identity.organization_uuid == "acct-uuid-1"
    assert snapshot.tier == TIER_PRO  # plan came from the auth claims
    assert "secret" not in (snapshot.note or "")
    assert token not in json.dumps(snapshot.to_dict())


def test_codex_with_no_sessions_returns_nothing(tmp_path: Path) -> None:
    adapter = CodexSessionsAdapter(codex_home=tmp_path / "missing", read_identity=False)
    assert adapter.snapshot(NOW) == []
    assert any("no session transcripts" in warning for warning in adapter.warnings)


def test_codex_ignores_unparseable_lines(tmp_path: Path) -> None:
    write_session(
        tmp_path / "sessions" / "2026" / "08" / "14",
        "rollout.jsonl",
        [
            '{"rate_limits": TRUNCATED',
            "not json at all",
            token_count_line(timestamp="2026-08-14T19:00:00Z", used_percent=33.0),
        ],
    )
    snapshot = CodexSessionsAdapter(codex_home=tmp_path, read_identity=False).snapshot(NOW)[0]
    assert snapshot.windows[0].used_fraction == pytest.approx(0.33)


def test_window_key_for_minutes() -> None:
    assert window_key_for_minutes(10080) == "7d"
    assert window_key_for_minutes(300) == "5h"
    assert window_key_for_minutes(90) == "90m"
    assert window_key_for_minutes(None) == "window"


# ======================================================================================
# antigravity
# ======================================================================================


def test_antigravity_reports_two_unobservable_pools() -> None:
    snapshots = AntigravityAdapter(which=lambda _binary: "/usr/local/bin/agy").snapshot(NOW)

    assert [snapshot.id for snapshot in snapshots] == [
        ACCOUNT_ANTIGRAVITY_GEMINI,
        ACCOUNT_ANTIGRAVITY_CLAUDE,
    ]
    for snapshot in snapshots:
        assert snapshot.windows == ()
        assert snapshot.confidence == 0.0
        assert snapshot.source == SOURCE_ASSUMED
        assert snapshot.available is True
        assert snapshot.min_slack(NOW) is None  # nothing observable to score


def test_antigravity_failure_learned_deadline_is_the_only_signal() -> None:
    adapter = AntigravityAdapter(
        which=lambda _binary: "/usr/local/bin/agy",
        exhausted_until={
            ACCOUNT_ANTIGRAVITY_GEMINI: NOW + 1554,  # "Resets in 25m54s"
            ACCOUNT_ANTIGRAVITY_CLAUDE: NOW - 5,  # already expired
        },
    )
    by_id = {snapshot.id: snapshot for snapshot in adapter.snapshot(NOW)}

    assert by_id[ACCOUNT_ANTIGRAVITY_GEMINI].available is False
    assert "exhaustion until" in (by_id[ACCOUNT_ANTIGRAVITY_GEMINI].note or "")
    assert by_id[ACCOUNT_ANTIGRAVITY_CLAUDE].available is True
    assert by_id[ACCOUNT_ANTIGRAVITY_CLAUDE].confidence == 0.0


def test_antigravity_reads_a_state_file(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps({"exhausted_until": {ACCOUNT_ANTIGRAVITY_GEMINI: "2026-08-16T04:00:00Z"}}),
        encoding="utf-8",
    )
    adapter = AntigravityAdapter(which=lambda _binary: "/bin/agy", state_path=state)
    by_id = {snapshot.id: snapshot for snapshot in adapter.snapshot(NOW)}

    assert by_id[ACCOUNT_ANTIGRAVITY_GEMINI].available is False
    assert by_id[ACCOUNT_ANTIGRAVITY_CLAUDE].available is True


def test_antigravity_without_the_binary_is_unavailable() -> None:
    adapter = AntigravityAdapter(which=lambda _binary: None)
    snapshots = adapter.snapshot(NOW)
    assert all(snapshot.available is False for snapshot in snapshots)
    assert any("not on PATH" in warning for warning in adapter.warnings)


# ======================================================================================
# Cross-adapter
# ======================================================================================


def test_no_adapter_spawns_a_process(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The hard guarantee: nothing here reaches the process table.

    :class:`ClaudeOAuthAdapter` gets an injected runner and opener; every other adapter
    must not touch :mod:`subprocess` at all.
    """

    def explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"a test spawned a process: {args!r}")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)
    monkeypatch.setattr(subprocess, "check_output", explode)

    write_statusline(tmp_path / "claude-rate-limits.json", statusline_payload())
    write_session(
        tmp_path / "sessions" / "2026" / "08" / "14",
        "rollout.jsonl",
        [token_count_line(timestamp="2026-08-14T19:00:00Z")],
    )
    oauth, _keychain, _opener = healthy_adapter(tmp_path)
    adapters = [
        oauth,
        ClaudeStatuslineAdapter(directory=tmp_path, home=tmp_path, env={}),
        CodexSessionsAdapter(codex_home=tmp_path, read_identity=False),
        AntigravityAdapter(which=lambda _binary: None),
    ]
    snapshots, warnings = collect_snapshots(adapters, NOW)
    assert {snapshot.id for snapshot in snapshots} >= {ACCOUNT_CLAUDE, ACCOUNT_CODEX}
    assert not [warning for warning in warnings if "spawned a process" in warning]


def test_collect_snapshots_prefers_the_live_read_over_the_cache(tmp_path: Path) -> None:
    """The live read wins over a statusline cache: it carries the scoped windows too."""
    write_statusline(tmp_path / "claude-rate-limits.json", statusline_payload())
    oauth, _keychain, _opener = healthy_adapter(tmp_path)
    snapshots, _warnings = collect_snapshots(
        [oauth, ClaudeStatuslineAdapter(directory=tmp_path, home=tmp_path, env={})], NOW
    )
    by_id = {snapshot.id: snapshot for snapshot in snapshots}

    assert by_id[ACCOUNT_CLAUDE].source == SOURCE_LIVE
    assert "fable" in {window.key for window in by_id[ACCOUNT_CLAUDE].windows}

    # And the fallback is used when the live adapter produces nothing at all.
    silent = ClaudeOAuthAdapter(
        runner=FakeKeychain({}),
        opener=FakeOpener(),
        home=tmp_path,
        configs=[
            ClaudeAccountConfig(
                account_id=ACCOUNT_CLAUDE,
                config_dir=tmp_path / "never-created",
                identity=Identity(*IDENTITIES[0]),
                tier=TIER_MAX_20X,
            )
        ],
    )
    fallback, _ = collect_snapshots(
        [silent, ClaudeStatuslineAdapter(directory=tmp_path, home=tmp_path, env={})], NOW
    )
    assert [snapshot.source for snapshot in fallback] == [SOURCE_CACHE]


def test_collect_snapshots_survives_a_broken_adapter() -> None:
    class Exploding:
        name = "exploding"

        def snapshot(self, now_s: float) -> list[Any]:
            raise RuntimeError("upstream changed shape")

    snapshots, warnings = collect_snapshots(
        [Exploding(), AntigravityAdapter(which=lambda _binary: None)], NOW
    )
    assert len(snapshots) == 2
    assert any("upstream changed shape" in warning for warning in warnings)


class FakePolicyAccount:
    """Duck-typed stand-in for the policy layer's ``AccountConfig``."""

    def __init__(
        self,
        account_id: str,
        *,
        config_dir: str | None = None,
        tier: str = TIER_UNKNOWN,
        identity_email: str | None = None,
        provider: str = "claude",
    ) -> None:
        self.id = account_id
        self.config_dir = config_dir
        self.tier = tier
        self.identity_email = identity_email
        self.provider = provider


class FakePolicy:
    def __init__(self, *accounts: FakePolicyAccount) -> None:
        self._accounts = accounts

    def enabled_accounts(self) -> tuple[FakePolicyAccount, ...]:
        return self._accounts


def test_policy_supplies_config_dirs_overrides_and_fallbacks(tmp_path: Path) -> None:
    write_claude_config(
        tmp_path, "custom-a", email="acct1@example.com", uuid=IDENTITIES[0][1],
        tier="default_claude_max_20x",
    )
    policy = FakePolicy(
        FakePolicyAccount(ACCOUNT_CLAUDE, config_dir=str(tmp_path / "custom-a")),
        # No config dir on disk: the declared tier is the manual override that case exists
        # for, and the declared email is how the identity is still written down.
        FakePolicyAccount(
            ACCOUNT_CLAUDE_C,
            config_dir=str(tmp_path / "missing"),
            tier="max_5x",
            identity_email="acct3@example.com",
        ),
        FakePolicyAccount(ACCOUNT_CODEX, provider="codex"),  # not a Claude account
    )

    configs = {
        config.account_id: config
        for config in claude_configs_from_policy(policy, env={}, home=tmp_path)
    }
    assert set(configs) == {ACCOUNT_CLAUDE, ACCOUNT_CLAUDE_C}
    assert configs[ACCOUNT_CLAUDE].identity is not None
    assert configs[ACCOUNT_CLAUDE].tier == TIER_MAX_20X
    assert configs[ACCOUNT_CLAUDE_C].identity is None
    assert configs[ACCOUNT_CLAUDE_C].tier == TIER_MAX_5X
    assert configs[ACCOUNT_CLAUDE_C].declared_email == "acct3@example.com"


def test_the_policy_tier_reaches_the_snapshot_as_capacity(tmp_path: Path) -> None:
    """The "config is unreadable, I know what I pay for" override has to survive the read."""
    write_claude_config(
        tmp_path, "custom-a", email="acct1@example.com", uuid=IDENTITIES[0][1], tier=None
    )
    (tmp_path / "custom-c").mkdir()
    policy = FakePolicy(
        FakePolicyAccount(ACCOUNT_CLAUDE, config_dir=str(tmp_path / "custom-a")),
        FakePolicyAccount(ACCOUNT_CLAUDE_C, config_dir=str(tmp_path / "custom-c"), tier="max_5x"),
    )
    configs = list(claude_configs_from_policy(policy, env={}, home=tmp_path))
    adapter, _keychain, _opener = healthy_adapter(tmp_path, configs=configs)

    snapshots = adapter.snapshot(NOW)
    assert {snapshot.id: snapshot.capacity for snapshot in snapshots} == {
        ACCOUNT_CLAUDE: 1.0,  # tier unreadable -> neutral capacity
        ACCOUNT_CLAUDE_C: 0.25,  # declared max_5x
    }
    assert {snapshot.id: snapshot.tier for snapshot in snapshots} == {
        ACCOUNT_CLAUDE: TIER_UNKNOWN,
        ACCOUNT_CLAUDE_C: TIER_MAX_5X,
    }


def test_load_snapshots_is_the_one_call_the_cli_needs(tmp_path: Path) -> None:
    """End to end through the CLI's single entry point, with no Keychain entries.

    ``load_snapshots`` exposes no hook for the HTTP opener, so the only hermetic path
    through it is one where no token is found -- which is also the shape that matters
    operationally: every account still appears, marked unavailable, never silently
    dropped.
    """
    write_claude_config(
        tmp_path, ".claude", email="acct1@example.com", uuid=IDENTITIES[0][1],
        tier="default_claude_max_20x", sibling=True,
    )
    write_claude_config(
        tmp_path, ".claude-c", email="acct3@example.com", uuid=IDENTITIES[2][1],
        tier="default_claude_max_5x",
    )
    policy = FakePolicy(
        FakePolicyAccount(ACCOUNT_CLAUDE),
        FakePolicyAccount(ACCOUNT_CLAUDE_C),
    )
    keychain = FakeKeychain({})

    snapshots, warnings = load_snapshots(
        now_s=NOW,
        config=policy,
        env={},
        runner=keychain,
        home=tmp_path,
        accounts=[ACCOUNT_CLAUDE, ACCOUNT_CLAUDE_C, ACCOUNT_ANTIGRAVITY_GEMINI],
    )

    # Every subprocess it ran was the read-only Keychain query.
    assert keychain.calls
    assert all(argv[:2] == ["security", "find-generic-password"] for argv in keychain.calls)
    by_id = {snapshot.id: snapshot for snapshot in snapshots}
    assert set(by_id) == {ACCOUNT_CLAUDE, ACCOUNT_CLAUDE_C, ACCOUNT_ANTIGRAVITY_GEMINI}
    assert by_id[ACCOUNT_CLAUDE].available is False
    assert by_id[ACCOUNT_CLAUDE].tier == TIER_MAX_20X  # tier still resolved from disk
    assert all(isinstance(warning, str) for warning in warnings)
    # The account filter really filters.
    assert ACCOUNT_CLAUDE_B not in by_id


def test_load_snapshots_signature_matches_what_the_cli_injects() -> None:
    """The CLI passes only the kwargs the loader declares; these are the ones it offers."""
    import inspect

    parameters = inspect.signature(load_snapshots).parameters
    assert {"config", "env", "now_s", "timeout_s", "runner", "accounts"} <= set(parameters)
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY for parameter in parameters.values()
    )


# ======================================================================================
# Live (deselected by default: `pytest -m live`)
# ======================================================================================


@pytest.mark.live
def test_live_claude_configs_resolve_on_this_machine() -> None:
    configs = {config.account_id: config for config in discover_claude_configs()}
    resolved = [config for config in configs.values() if config.resolved]
    if not resolved:
        pytest.skip("no ~/.claude*/.claude.json on this machine")
    assert all(config.identity is not None and config.identity.email for config in resolved)


@pytest.mark.live
def test_live_codex_sessions_are_fast_and_parse() -> None:
    home = Path(os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex")))
    if not (home / "sessions").is_dir():
        pytest.skip("no codex sessions on this machine")

    started = datetime.now(tz=timezone.utc)
    snapshots = CodexSessionsAdapter(codex_home=home).snapshot(
        datetime.now(tz=timezone.utc).timestamp()
    )
    elapsed = (datetime.now(tz=timezone.utc) - started).total_seconds()

    assert elapsed < 2.0, f"reading session tails took {elapsed:.2f}s -- too slow for per-call use"
    if snapshots:
        assert snapshots[0].windows


# ======================================================================================
# Usage cache — the endpoint rate-limits, so an uncached per-call read throttles itself
# ======================================================================================


def _keychain_with_token(expires_at_ms: float) -> FakeRunner:
    return FakeRunner(
        json.dumps({"claudeAiOauth": {"accessToken": "tok", "expiresAt": expires_at_ms}})
    )


def test_a_fresh_cache_is_reused_instead_of_refetching(tmp_path: Path) -> None:
    """Within the TTL the endpoint is not called at all.

    This is not a latency nicety. The usage endpoint returns HTTP 429 under
    modest load (observed during development), and the router runs once per LLM
    call across every account -- so an uncached read rate-limits itself and the
    whole fleet degrades to the statusline cache.
    """
    config = make_config(tmp_path)
    runner = _keychain_with_token((NOW + 3600) * 1000)
    opener = FakeOpener(usage_payload())

    first = ClaudeOAuthAdapter(runner=runner, opener=opener, home=tmp_path, configs=[config])
    assert first.snapshot(NOW)[0].available is True
    assert len(opener.requests) == 1

    second = ClaudeOAuthAdapter(runner=runner, opener=opener, home=tmp_path, configs=[config])
    snapshot = second.snapshot(NOW + 30)[0]

    assert snapshot.available is True
    assert len(opener.requests) == 1, "a fresh cache must not trigger a second fetch"
    assert "cached" in (snapshot.note or "")


def test_an_expired_cache_refetches(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    runner = _keychain_with_token((NOW + 3600) * 1000)
    opener = FakeOpener(usage_payload())

    ClaudeOAuthAdapter(runner=runner, opener=opener, home=tmp_path, configs=[config]).snapshot(NOW)
    later = ClaudeOAuthAdapter(runner=runner, opener=opener, home=tmp_path, configs=[config])
    later.snapshot(NOW + 121)

    assert len(opener.requests) == 2


def test_a_throttled_endpoint_serves_the_recent_cache_and_says_so(tmp_path: Path) -> None:
    """HTTP 429 must not black out an account we have recent numbers for."""
    config = make_config(tmp_path)
    runner = _keychain_with_token((NOW + 3600) * 1000)

    ok = FakeOpener(usage_payload())
    ClaudeOAuthAdapter(runner=runner, opener=ok, home=tmp_path, configs=[config]).snapshot(NOW)

    throttled = FakeOpener(
        raises=urllib.error.HTTPError(USAGE_URL, 429, "Too Many Requests", {}, None)
    )
    snapshot = ClaudeOAuthAdapter(
        runner=runner, opener=throttled, home=tmp_path, configs=[config]
    ).snapshot(NOW + 300)[0]

    assert snapshot.available is True
    assert snapshot.windows, "a served cache must still carry its windows"
    note = snapshot.note or ""
    assert "429" in note and "served cache" in note, (
        f"the operator must be able to tell a served cache from a fresh read; got {note!r}"
    )


def test_a_cache_older_than_the_stale_bound_is_refused(tmp_path: Path) -> None:
    """Past the bound we go dark rather than route on numbers old enough to be wrong."""
    config = make_config(tmp_path)
    runner = _keychain_with_token((NOW + 86400) * 1000)

    ok = FakeOpener(usage_payload())
    ClaudeOAuthAdapter(runner=runner, opener=ok, home=tmp_path, configs=[config]).snapshot(NOW)

    throttled = FakeOpener(
        raises=urllib.error.HTTPError(USAGE_URL, 429, "Too Many Requests", {}, None)
    )
    snapshot = ClaudeOAuthAdapter(
        runner=runner, opener=throttled, home=tmp_path, configs=[config]
    ).snapshot(NOW + 901)[0]

    assert snapshot.available is False
    assert snapshot.windows == ()


def test_the_cache_never_escapes_an_injected_home(tmp_path: Path) -> None:
    """A synthetic home must cache inside itself, never in the operator's real state dir."""
    real_home_cache = usage_cache_path(ACCOUNT_CLAUDE)
    injected = usage_cache_path(ACCOUNT_CLAUDE, home=tmp_path)

    assert injected != real_home_cache
    assert str(injected).startswith(str(tmp_path)), injected


def test_a_429_backoff_is_honored_on_the_next_invocation(tmp_path: Path) -> None:
    """After a 429 we must not re-ask inside the window the server named.

    Re-requesting through a throttle is what earns the throttle. The observed
    endpoint returns `Retry-After` in the tens of seconds, so a router invoked per
    LLM call would otherwise spend every one of those calls on a rejected request.
    """
    config = make_config(tmp_path)
    runner = _keychain_with_token((NOW + 86400) * 1000)

    ok = FakeOpener(usage_payload())
    ClaudeOAuthAdapter(runner=runner, opener=ok, home=tmp_path, configs=[config]).snapshot(NOW)

    throttled = FakeOpener(
        raises=urllib.error.HTTPError(
            USAGE_URL, 429, "Too Many Requests", {"Retry-After": "50"}, None
        )
    )
    # Past the TTL, so this one really does try the endpoint and gets refused.
    ClaudeOAuthAdapter(
        runner=runner, opener=throttled, home=tmp_path, configs=[config]
    ).snapshot(NOW + 300)
    assert len(throttled.requests) == 1

    # Still inside Retry-After: serve the cache, ask nothing.
    again = FakeOpener(
        raises=urllib.error.HTTPError(
            USAGE_URL, 429, "Too Many Requests", {"Retry-After": "50"}, None
        )
    )
    snapshot = ClaudeOAuthAdapter(
        runner=runner, opener=again, home=tmp_path, configs=[config]
    ).snapshot(NOW + 320)[0]

    assert again.requests == [], "must not re-request inside the server's backoff window"
    assert snapshot.available is True
    assert snapshot.windows

    # Past it, we are allowed to try again.
    after = FakeOpener(usage_payload())
    ClaudeOAuthAdapter(
        runner=runner, opener=after, home=tmp_path, configs=[config]
    ).snapshot(NOW + 400)
    assert len(after.requests) == 1
