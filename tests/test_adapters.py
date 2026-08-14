"""Adapter tests: fixture-driven, hermetic, and with **zero subprocesses**.

Every process call in the package goes through an injected runner, so the only ``cswap``
that runs here is a function defined in this file. Filesystem sources are pointed at
``tmp_path``. The one test that touches the operator's real machine is marked ``live``
and is deselected by default (``pytest -m live`` to run it); even that one only reads
files -- it never spawns anything either.

The cswap fixture in ``tests/fixtures/cswap_real.json`` is a real capture with the
identities anonymized. Its numbers are load-bearing: ``pct``/``expectedPct`` pairs there
are what the hand-computed slacks below are derived from, and it contains the upstream
``aheadOfPace`` inconsistency (``pct=60`` vs ``expectedPct=54.6`` reported as ``false``)
that the router must ignore.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from quota_router.providers import (
    AntigravityAdapter,
    ClaudeCswapAdapter,
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
from quota_router.providers.claude_cswap import (
    ClaudeAccountConfig,
    discover_claude_configs,
)
from quota_router.providers.codex_sessions import window_key_for_minutes
from quota_router.types import (
    ACCOUNT_ANTIGRAVITY_CLAUDE,
    ACCOUNT_ANTIGRAVITY_GEMINI,
    ACCOUNT_CLAUDE,
    ACCOUNT_CLAUDE_B,
    ACCOUNT_CLAUDE_C,
    ACCOUNT_CODEX,
    SOURCE_ASSUMED,
    SOURCE_CACHE,
    SOURCE_CSWAP,
    TIER_MAX_5X,
    TIER_MAX_20X,
    TIER_PRO,
    TIER_UNKNOWN,
    Identity,
)

FIXTURES = Path(__file__).parent / "fixtures"
CSWAP_FIXTURE = FIXTURES / "cswap_real.json"

#: ``usageFetchedAt`` of the first fixture account (2026-08-14T18:46:52Z), plus 30s.
FIXTURE_FETCHED_S = 1786733212.0
NOW = FIXTURE_FETCHED_S + 30.0

#: The fixture's anonymized identities, in listing order.
FIXTURE_IDENTITIES = (
    ("acct1@example.com", "00000000-0000-4000-8000-000000000001"),
    ("acct2@example.com", "00000000-0000-4000-8000-000000000002"),
    ("acct3@example.com", "00000000-0000-4000-8000-000000000003"),
)


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


def fixture_text() -> str:
    return CSWAP_FIXTURE.read_text(encoding="utf-8")


def fixture_payload() -> dict[str, Any]:
    return json.loads(fixture_text())


def fixture_configs(
    *, tiers: tuple[str, str, str] = (TIER_MAX_20X, TIER_MAX_20X, TIER_MAX_5X)
) -> list[ClaudeAccountConfig]:
    """The identity -> account-id mapping the fixture expects."""
    account_ids = (ACCOUNT_CLAUDE, ACCOUNT_CLAUDE_B, ACCOUNT_CLAUDE_C)
    return [
        ClaudeAccountConfig(
            account_id=account_id,
            config_dir=Path(f"/nonexistent/{account_id}"),
            identity=Identity(email=email, organization_uuid=uuid),
            tier=tier,
        )
        for account_id, (email, uuid), tier in zip(account_ids, FIXTURE_IDENTITIES, tiers)
    ]


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


def test_every_adapter_satisfies_the_protocol() -> None:
    adapters = [
        ClaudeCswapAdapter(runner=FakeRunner("{}")),
        ClaudeStatuslineAdapter(paths={}),
        CodexSessionsAdapter(codex_home="/nonexistent"),
        AntigravityAdapter(which=lambda _binary: None),
    ]
    for adapter in adapters:
        assert isinstance(adapter, ProviderAdapter)
        assert isinstance(adapter.name, str) and adapter.name
        assert adapter.snapshot(NOW) is not None


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
    assert parse_iso8601("2026-08-14T18:46:52Z") == FIXTURE_FETCHED_S
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
# claude_cswap
# ======================================================================================


def test_cswap_runs_exactly_one_read_only_command() -> None:
    runner = FakeRunner(fixture_text())
    ClaudeCswapAdapter(runner=runner, configs=fixture_configs()).snapshot(NOW)

    assert len(runner.calls) == 1
    argv, kwargs = runner.calls[0]
    assert argv == ["cswap", "list", "--json"]
    # The mutating verbs must never appear -- cswap is an oracle, not the executor.
    assert not {"run", "switch", "auto", "add", "remove"} & set(argv)
    assert kwargs.get("shell") is None


def test_cswap_maps_the_real_fixture_onto_three_identified_accounts() -> None:
    snapshots = ClaudeCswapAdapter(
        runner=FakeRunner(fixture_text()), configs=fixture_configs()
    ).snapshot(NOW)

    assert [snapshot.id for snapshot in snapshots] == [
        ACCOUNT_CLAUDE,
        ACCOUNT_CLAUDE_B,
        ACCOUNT_CLAUDE_C,
    ]
    assert [snapshot.tier for snapshot in snapshots] == [TIER_MAX_20X, TIER_MAX_20X, TIER_MAX_5X]
    assert [snapshot.capacity for snapshot in snapshots] == [1.0, 1.0, 0.25]
    assert all(snapshot.source == SOURCE_CSWAP for snapshot in snapshots)
    assert snapshots[0].identity is not None
    assert snapshots[0].identity.email == "acct1@example.com"


def test_cswap_window_keys_lengths_and_model_gating() -> None:
    snapshot = ClaudeCswapAdapter(
        runner=FakeRunner(fixture_text()), configs=fixture_configs()
    ).snapshot(NOW)[0]

    assert [window.key for window in snapshot.windows] == ["5h", "7d", "fable"]
    five_hour, seven_day, fable = snapshot.windows
    assert five_hour.length_s == 5 * 3600
    assert seven_day.length_s == 7 * 86400
    assert fable.length_s == 7 * 86400
    assert five_hour.applies_to is None and seven_day.applies_to is None
    assert fable.applies_to == frozenset({"fable"})

    # A scoped window governs only its own class; asking about another class skips it.
    assert [w.key for w in snapshot.applicable_windows("fable")] == ["5h", "7d", "fable"]
    assert [w.key for w in snapshot.applicable_windows("sonnet")] == ["5h", "7d"]
    # ...and skipping it changes which window binds.
    assert snapshot.binding_window_key(NOW, "fable") == "fable"
    assert snapshot.binding_window_key(NOW, "sonnet") == "7d"


def test_cswap_slack_uses_expected_pct_and_ignores_the_broken_ahead_of_pace_flag() -> None:
    """The fixture's ``aheadOfPace`` is self-inconsistent; the numbers are not.

    Account 1 reports ``pct=60`` against ``expectedPct=54.6`` with
    ``aheadOfPace: false`` -- upstream says "behind pace" while its own numbers say the
    opposite. Slack must come from ``expectedPct - pct``.
    """
    payload = fixture_payload()
    assert payload["accounts"][0]["usage"]["sevenDay"]["aheadOfPace"] is False

    snapshot = ClaudeCswapAdapter(
        runner=FakeRunner(fixture_text()), configs=fixture_configs()
    ).snapshot(NOW)[0]
    seven_day = snapshot.window("7d")
    assert seven_day is not None
    assert seven_day.expected_used_fraction == pytest.approx(0.546)
    assert seven_day.slack(NOW) == pytest.approx(0.546 - 0.60)
    assert seven_day.ahead_of_pace(NOW) is True  # not what the flag claimed

    fable = snapshot.window("fable")
    assert fable is not None
    assert fable.slack(NOW) == pytest.approx(0.546 - 0.80)
    assert snapshot.min_slack(NOW, "fable") == pytest.approx(-0.254)


def test_cswap_five_hour_window_derives_its_baseline_from_the_reset_time() -> None:
    """``fiveHour`` has no ``expectedPct``; the uniform prior has to fill in."""
    snapshot = ClaudeCswapAdapter(
        runner=FakeRunner(fixture_text()), configs=fixture_configs()
    ).snapshot(NOW)[0]
    five_hour = snapshot.window("5h")
    assert five_hour is not None
    assert five_hour.expected_used_fraction is None

    time_to_reset = 1786734000.035216 - NOW
    elapsed = 1.0 - time_to_reset / (5 * 3600)
    assert five_hour.expected_used_at(NOW) == pytest.approx(elapsed)
    assert five_hour.slack(NOW) == pytest.approx((1 - 0.12) - (1 - elapsed))


def test_cswap_keys_by_identity_not_by_config_dir_or_number() -> None:
    """Renumbering and re-pointing directories must not move a quota pool."""
    payload = fixture_payload()
    payload["accounts"].reverse()
    for index, account in enumerate(payload["accounts"], start=1):
        account["number"] = index  # cswap renumbers on add/remove

    configs = [
        ClaudeAccountConfig(
            account_id=ACCOUNT_CLAUDE_C,
            config_dir=Path("/somewhere/else"),
            identity=Identity(*FIXTURE_IDENTITIES[2]),
            tier=TIER_MAX_5X,
        ),
        ClaudeAccountConfig(
            account_id=ACCOUNT_CLAUDE,
            config_dir=Path("/another/place"),
            identity=Identity(*FIXTURE_IDENTITIES[0]),
            tier=TIER_MAX_20X,
        ),
    ]
    snapshots = ClaudeCswapAdapter(
        runner=FakeRunner(json.dumps(payload)), configs=configs
    ).snapshot(NOW)

    by_id = {snapshot.id: snapshot for snapshot in snapshots}
    assert set(by_id) == {ACCOUNT_CLAUDE, ACCOUNT_CLAUDE_C}
    assert by_id[ACCOUNT_CLAUDE].identity is not None
    assert by_id[ACCOUNT_CLAUDE].identity.email == "acct1@example.com"
    assert by_id[ACCOUNT_CLAUDE_C].tier == TIER_MAX_5X


def test_cswap_reads_tiers_from_both_claude_json_layouts(tmp_path: Path) -> None:
    """``~/.claude.json`` (sibling) and ``~/.claude-b/.claude.json`` (inside) both count."""
    write_claude_config(
        tmp_path, ".claude", email="acct1@example.com", uuid=FIXTURE_IDENTITIES[0][1],
        tier="default_claude_max_20x", sibling=True,
    )
    write_claude_config(
        tmp_path, ".claude-b", email="acct2@example.com", uuid=FIXTURE_IDENTITIES[1][1],
        tier="default_claude_max_20x",
    )
    write_claude_config(
        tmp_path, ".claude-c", email="acct3@example.com", uuid=FIXTURE_IDENTITIES[2][1],
        tier="default_claude_max_5x",
    )

    configs = {config.account_id: config for config in discover_claude_configs(home=tmp_path)}
    assert configs[ACCOUNT_CLAUDE].tier == TIER_MAX_20X
    assert configs[ACCOUNT_CLAUDE].source_path == tmp_path / ".claude.json"
    assert configs[ACCOUNT_CLAUDE_C].tier == TIER_MAX_5X
    assert configs[ACCOUNT_CLAUDE_C].source_path == tmp_path / ".claude-c" / ".claude.json"

    snapshots = ClaudeCswapAdapter(
        runner=FakeRunner(fixture_text()), home=tmp_path, env={}
    ).snapshot(NOW)
    assert {snapshot.id: snapshot.capacity for snapshot in snapshots} == {
        ACCOUNT_CLAUDE: 1.0,
        ACCOUNT_CLAUDE_B: 1.0,
        ACCOUNT_CLAUDE_C: 0.25,
    }


def test_cswap_unknown_tier_keeps_capacity_neutral_and_lowers_confidence(tmp_path: Path) -> None:
    write_claude_config(
        tmp_path, ".claude", email="acct1@example.com", uuid=FIXTURE_IDENTITIES[0][1], tier=None
    )
    snapshots = ClaudeCswapAdapter(
        runner=FakeRunner(fixture_text()), home=tmp_path, env={}
    ).snapshot(NOW)

    assert [snapshot.id for snapshot in snapshots] == [ACCOUNT_CLAUDE]
    assert snapshots[0].tier == TIER_UNKNOWN
    assert snapshots[0].capacity == 1.0  # neutral, per the contract
    assert snapshots[0].confidence < 1.0


@pytest.mark.parametrize(
    ("runner", "expected_fragment"),
    [
        (FakeRunner(raises=FileNotFoundError("cswap")), "binary not found"),
        (FakeRunner("", returncode=1, stderr="boom"), "exited 1"),
        (FakeRunner("not json at all"), "unparseable JSON"),
        (
            FakeRunner(raises=subprocess.TimeoutExpired(cmd="cswap", timeout=10.0)),
            "timed out",
        ),
    ],
)
def test_cswap_degrades_instead_of_raising(runner: FakeRunner, expected_fragment: str) -> None:
    adapter = ClaudeCswapAdapter(runner=runner, configs=fixture_configs())
    assert adapter.snapshot(NOW) == []
    assert any(expected_fragment in warning for warning in adapter.warnings)


def test_cswap_skips_accounts_it_cannot_speak_for() -> None:
    payload = fixture_payload()
    payload["accounts"][0]["usageStatus"] = "error"  # oracle could not fetch
    del payload["accounts"][1]["usage"]  # nothing to score
    # accounts[2] stays healthy

    adapter = ClaudeCswapAdapter(runner=FakeRunner(json.dumps(payload)), configs=fixture_configs())
    snapshots = adapter.snapshot(NOW)

    assert [snapshot.id for snapshot in snapshots] == [ACCOUNT_CLAUDE_C]
    joined = " ".join(adapter.warnings)
    assert "usageStatus" in joined
    assert "no usage block" in joined


def test_cswap_skips_an_identity_with_no_local_config_dir() -> None:
    """An account we cannot spawn a CLI for must not be offered as a candidate."""
    adapter = ClaudeCswapAdapter(
        runner=FakeRunner(fixture_text()), configs=fixture_configs()[:1]
    )
    snapshots = adapter.snapshot(NOW)

    assert [snapshot.id for snapshot in snapshots] == [ACCOUNT_CLAUDE]
    assert any("no local CLAUDE_CONFIG_DIR" in warning for warning in adapter.warnings)


def test_cswap_falls_back_to_a_declared_email_when_the_config_file_is_unreadable() -> None:
    """The operator wrote the email down in policy; that is still identity matching."""
    configs = [
        ClaudeAccountConfig(
            account_id=ACCOUNT_CLAUDE_B,
            config_dir=Path("/unreadable"),
            identity=None,  # .claude.json could not be read
            tier=TIER_MAX_20X,
            declared_email="acct2@example.com",
        )
    ]
    adapter = ClaudeCswapAdapter(runner=FakeRunner(fixture_text()), configs=configs)
    snapshots = adapter.snapshot(NOW)

    assert [snapshot.id for snapshot in snapshots] == [ACCOUNT_CLAUDE_B]
    assert snapshots[0].confidence == pytest.approx(1.0)  # a declared email is not a guess


def test_cswap_number_matching_is_the_last_resort_and_says_so() -> None:
    """Positions are reassigned; matching on one is degraded and must be visible."""
    configs = [
        ClaudeAccountConfig(
            account_id=ACCOUNT_CLAUDE_C,
            config_dir=Path("/unreadable"),
            identity=None,
            tier=TIER_MAX_5X,
            cswap_number=3,
        )
    ]
    adapter = ClaudeCswapAdapter(runner=FakeRunner(fixture_text()), configs=configs)
    snapshots = adapter.snapshot(NOW)

    assert [snapshot.id for snapshot in snapshots] == [ACCOUNT_CLAUDE_C]
    assert snapshots[0].confidence < 1.0
    assert snapshots[0].identity is not None
    assert snapshots[0].identity.email == "acct3@example.com"
    assert any("positional number" in warning for warning in adapter.warnings)


def test_cswap_prefers_identity_over_a_stale_number(tmp_path: Path) -> None:
    """A renumbered oracle must not move a pool: identity wins over ``cswap_number``."""
    configs = [
        ClaudeAccountConfig(
            account_id=ACCOUNT_CLAUDE,
            config_dir=tmp_path / ".claude",
            identity=Identity(*FIXTURE_IDENTITIES[0]),
            tier=TIER_MAX_20X,
            cswap_number=3,  # stale: this identity is number 1 in the fixture
        ),
    ]
    snapshots = ClaudeCswapAdapter(
        runner=FakeRunner(fixture_text()), configs=configs
    ).snapshot(NOW)

    assert len(snapshots) == 1
    assert snapshots[0].identity is not None
    assert snapshots[0].identity.email == "acct1@example.com"  # not acct3
    assert snapshots[0].confidence == pytest.approx(1.0)


def test_cswap_never_emits_two_snapshots_for_one_account_id() -> None:
    """One pool, one candidate. A duplicate id would double-count it in the ranking."""
    configs = [
        ClaudeAccountConfig(
            account_id=ACCOUNT_CLAUDE,
            config_dir=Path("/a"),
            identity=Identity(*FIXTURE_IDENTITIES[0]),
            cswap_number=2,
            declared_email="acct3@example.com",
        ),
        ClaudeAccountConfig(
            account_id=ACCOUNT_CLAUDE_B,
            config_dir=Path("/b"),
            identity=Identity(*FIXTURE_IDENTITIES[1]),
            cswap_number=1,
        ),
    ]
    snapshots = ClaudeCswapAdapter(
        runner=FakeRunner(fixture_text()), configs=configs
    ).snapshot(NOW)

    ids = [snapshot.id for snapshot in snapshots]
    assert len(ids) == len(set(ids))
    assert set(ids) == {ACCOUNT_CLAUDE, ACCOUNT_CLAUDE_B}


def test_cswap_clamps_an_out_of_range_percentage() -> None:
    payload = fixture_payload()
    payload["accounts"][0]["usage"]["sevenDay"]["pct"] = 130.0
    snapshot = ClaudeCswapAdapter(
        runner=FakeRunner(json.dumps(payload)), configs=fixture_configs()
    ).snapshot(NOW)[0]

    seven_day = snapshot.window("7d")
    assert seven_day is not None
    assert seven_day.used_fraction == 1.0
    assert seven_day.remaining_fraction == 0.0
    assert seven_day.is_exhausted


def test_cswap_survives_a_malformed_scoped_entry() -> None:
    payload = fixture_payload()
    payload["accounts"][0]["usage"]["scoped"].append({"name": "Broken", "pct": "???"})
    snapshot = ClaudeCswapAdapter(
        runner=FakeRunner(json.dumps(payload)), configs=fixture_configs()
    ).snapshot(NOW)[0]

    assert [window.key for window in snapshot.windows] == ["5h", "7d", "fable"]


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
    # Nine days old: confidence has decayed to the floor but the data is still offered.
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
        json.dumps({"exhausted_until": {ACCOUNT_ANTIGRAVITY_GEMINI: "2026-08-14T20:00:00Z"}}),
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

    ``ClaudeCswapAdapter`` gets an injected runner; every other adapter must not touch
    :mod:`subprocess` at all.
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
    adapters = [
        ClaudeCswapAdapter(runner=FakeRunner(fixture_text()), configs=fixture_configs()),
        ClaudeStatuslineAdapter(directory=tmp_path, home=tmp_path, env={}),
        CodexSessionsAdapter(codex_home=tmp_path, read_identity=False),
        AntigravityAdapter(which=lambda _binary: None),
    ]
    snapshots, _warnings = collect_snapshots(adapters, NOW)
    assert {snapshot.id for snapshot in snapshots} >= {ACCOUNT_CLAUDE, ACCOUNT_CODEX}


def test_collect_snapshots_prefers_the_oracle_over_the_cache(tmp_path: Path) -> None:
    """cswap wins over a fresher statusline: it carries the scoped windows too."""
    write_statusline(tmp_path / "claude-rate-limits.json", statusline_payload())
    adapters = [
        ClaudeCswapAdapter(runner=FakeRunner(fixture_text()), configs=fixture_configs()),
        ClaudeStatuslineAdapter(directory=tmp_path, home=tmp_path, env={}),
    ]
    snapshots, _warnings = collect_snapshots(adapters, NOW)
    by_id = {snapshot.id: snapshot for snapshot in snapshots}

    assert by_id[ACCOUNT_CLAUDE].source == SOURCE_CSWAP
    assert "fable" in {window.key for window in by_id[ACCOUNT_CLAUDE].windows}
    # And the fallback is used when the oracle produces nothing.
    fallback, _ = collect_snapshots(
        [
            ClaudeCswapAdapter(runner=FakeRunner(raises=FileNotFoundError()), configs=fixture_configs()),
            ClaudeStatuslineAdapter(directory=tmp_path, home=tmp_path, env={}),
        ],
        NOW,
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
        cswap_number: int | None = None,
        provider: str = "claude",
    ) -> None:
        self.id = account_id
        self.config_dir = config_dir
        self.tier = tier
        self.identity_email = identity_email
        self.cswap_number = cswap_number
        self.provider = provider


class FakePolicy:
    def __init__(self, *accounts: FakePolicyAccount) -> None:
        self._accounts = accounts

    def enabled_accounts(self) -> tuple[FakePolicyAccount, ...]:
        return self._accounts


def test_policy_supplies_config_dirs_overrides_and_fallbacks(tmp_path: Path) -> None:
    write_claude_config(
        tmp_path, "custom-a", email="acct1@example.com", uuid=FIXTURE_IDENTITIES[0][1],
        tier="default_claude_max_20x",
    )
    policy = FakePolicy(
        FakePolicyAccount(ACCOUNT_CLAUDE, config_dir=str(tmp_path / "custom-a")),
        # No config dir on disk: the declared tier is the manual override that case exists
        # for, and the declared email is how the identity still resolves.
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

    snapshots = ClaudeCswapAdapter(
        runner=FakeRunner(fixture_text()), configs=configs.values()
    ).snapshot(NOW)
    assert {snapshot.id: snapshot.capacity for snapshot in snapshots} == {
        ACCOUNT_CLAUDE: 1.0,
        ACCOUNT_CLAUDE_C: 0.25,
    }


def test_load_snapshots_is_the_one_call_the_cli_needs(tmp_path: Path) -> None:
    write_claude_config(
        tmp_path, ".claude", email="acct1@example.com", uuid=FIXTURE_IDENTITIES[0][1],
        tier="default_claude_max_20x", sibling=True,
    )
    write_claude_config(
        tmp_path, ".claude-c", email="acct3@example.com", uuid=FIXTURE_IDENTITIES[2][1],
        tier="default_claude_max_5x",
    )
    policy = FakePolicy(
        FakePolicyAccount(ACCOUNT_CLAUDE),
        FakePolicyAccount(ACCOUNT_CLAUDE_C),
    )
    runner = FakeRunner(fixture_text())

    snapshots, warnings = load_snapshots(
        now_s=NOW,
        config=policy,
        env={},
        runner=runner,
        home=tmp_path,
        accounts=[ACCOUNT_CLAUDE, ACCOUNT_CLAUDE_C, ACCOUNT_ANTIGRAVITY_GEMINI],
    )

    assert runner.calls[0][0] == ["cswap", "list", "--json"]
    assert {snapshot.id for snapshot in snapshots} == {
        ACCOUNT_CLAUDE,
        ACCOUNT_CLAUDE_C,
        ACCOUNT_ANTIGRAVITY_GEMINI,
    }
    assert all(isinstance(warning, str) for warning in warnings)
    # The account filter really filters.
    assert ACCOUNT_CLAUDE_B not in {snapshot.id for snapshot in snapshots}


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
