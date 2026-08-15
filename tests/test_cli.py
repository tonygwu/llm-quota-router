"""Tests for :mod:`quota_router.cli` (and the config/model-class/history/explain layers
it is the only reachable entry point for).

``main()`` is always called directly -- no subprocess, no real clock, no reading the
operator's actual machine -- and assertions are made on parsed JSON and exit codes.

The contracts under test, in order of how expensive they are to get wrong:

1. ``pick`` emits valid JSON and exits 0 on **every** path except flag/config errors.
2. ``exec.env`` never carries a proxy variable, whatever the environment or config says.
3. The two regimes stay separate -- under scarcity a small account must not win by having
   its negative slack shrunk toward zero.
4. ``ranked`` is a usable retry order, and ``--exclude`` holds even on the fallback path.
5. ``exec`` passes the child's exit code through unchanged.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from quota_router import cli
from quota_router.config import BANNED_EXEC_ENV
from quota_router.explain import explain_decision
from quota_router.model_classes import classify, demand_multiplier, resolve
from quota_router.types import (
    QUOTA_ROUTER_CONTRACT_VERSION,
    REGIME_A,
    REGIME_B,
    AccountSnapshot,
    Decision,
    ScoreBreakdown,
    Window,
    WindowSlack,
)

#: The instant the committed fixture was captured (``usageFetchedAt`` in
#: ``tests/fixtures/cswap_real.json``), so window offsets below mean what they meant
#: on the operator's machine.
NOW = 1_786_733_212.0  # 2026-08-14T18:46:52Z
FIVE_HOURS = 18_000.0
SEVEN_DAYS = 604_800.0


# ======================================================================================
# Builders
# ======================================================================================


def window(
    key: str,
    used: float,
    *,
    length_s: float = SEVEN_DAYS,
    resets_in_s: float = 3600.0,
    expected_used: float | None = None,
    applies_to: set[str] | None = None,
    observed_at_s: float = NOW,
) -> Window:
    return Window(
        key=key,
        used_fraction=used,
        length_s=length_s,
        resets_at_s=NOW + resets_in_s,
        observed_at_s=observed_at_s,
        expected_used_fraction=expected_used,
        applies_to=frozenset(applies_to) if applies_to else None,
    )


def account(account_id: str, *windows: Window, tier: str = "max_20x", **kwargs) -> AccountSnapshot:
    return AccountSnapshot(
        id=account_id, windows=windows, tier=tier, source="cswap", **kwargs
    )


def real_capture() -> tuple[AccountSnapshot, ...]:
    """The three accounts exactly as captured from ``cswap list --json``.

    Percentages are *used* percent; ``sevenDay``/``scoped`` carry the upstream pacing
    baseline, ``fiveHour`` does not (it is derived from the reset time instead).
    """

    def build(account_id, tier, five, five_reset, seven, seven_expected, seven_reset, fable):
        return account(
            account_id,
            window("five_hour", five, length_s=FIVE_HOURS, resets_in_s=five_reset),
            window(
                "seven_day",
                seven,
                resets_in_s=seven_reset,
                expected_used=seven_expected,
            ),
            window(
                "scoped:fable",
                fable,
                resets_in_s=seven_reset,
                expected_used=seven_expected,
                applies_to={"fable"},
            ),
            tier=tier,
        )

    return (
        build("claude", "max_20x", 0.12, 720, 0.60, 0.546, 273_600, 0.80),
        build("claude_b", "max_20x", 0.68, 13_320, 0.57, 0.344, 397_200, 0.88),
        build("claude_c", "max_5x", 1.00, 4_320, 1.00, 0.957, 25_920, 1.00),
    )


class Runner:
    """Stand-in for ``subprocess.run`` that records calls."""

    def __init__(self, returncode: int = 0, error: BaseException | None = None) -> None:
        self.returncode = returncode
        self.error = error
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.error is not None:
            raise self.error
        return type("Completed", (), {"returncode": self.returncode})()


@pytest.fixture()
def env(tmp_path) -> dict[str, str]:
    """An environment rooted entirely inside the test's temp dir."""
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PATH": "/usr/bin:/bin",
    }


def run(argv, env, *, snapshots=None, deps=None, now_s=NOW, cwd=None):
    """Call ``main()`` and return ``(exit_code, stdout, stderr)``."""
    if deps is None:
        deps = cli.Deps(load_snapshots=lambda **kw: (list(snapshots or ()), []))
    out, err = io.StringIO(), io.StringIO()
    code = main_code = cli.main(
        argv, env=env, stdout=out, stderr=err, now_s=now_s, deps=deps, cwd=cwd
    )
    return main_code, out.getvalue(), err.getvalue()


def pick(argv, env, **kwargs) -> dict:
    """Run a pick-shaped command and parse its JSON payload."""
    code, out, _ = run(argv, env, **kwargs)
    assert code == cli.EXIT_OK, f"expected exit 0, got {code}: {out}"
    return json.loads(out)


# ======================================================================================
# The JSON contract
# ======================================================================================


def test_pick_payload_matches_the_golden_schema(env):
    payload = pick(["pick"], env, snapshots=real_capture())

    assert set(payload) == {
        "contract_version",
        "decision",
        "exec",
        "ranked",
        "excluded",
        "degraded",
        "warnings",
        "generated_at",
    }
    assert set(payload["decision"]) == {
        "account",
        "provider",
        "reason",
        "score",
        "binding_window",
        "regime",
        "fits",
        "sticky",
    }
    assert set(payload["exec"]) == {"env"}
    assert set(payload["ranked"][0]) == {
        "account",
        "score",
        "min_slack",
        "binding_window",
        "remaining",
        "eligible",
        "source",
        "age_s",
        "fits",
    }
    assert set(payload["excluded"][0]) == set(payload["ranked"][0]) | {"reason"}
    assert isinstance(payload["degraded"], list)
    assert isinstance(payload["warnings"], list)
    assert payload["generated_at"].endswith("Z")


def test_contract_version_is_stamped(env):
    payload = pick(["pick"], env, snapshots=real_capture())
    assert payload["contract_version"] == QUOTA_ROUTER_CONTRACT_VERSION == 1


def test_pick_output_is_a_single_json_document(env):
    _, out, _ = run(["pick"], env, snapshots=real_capture())
    assert json.loads(out)  # would raise on trailing prose or a second document


def test_ranked_is_a_retry_order_not_just_a_winner(env):
    payload = pick(["pick"], env, snapshots=real_capture())
    scores = [row["score"] for row in payload["ranked"]]
    assert len(payload["ranked"]) >= 2
    assert scores == sorted(scores, reverse=True)
    assert payload["ranked"][0]["account"] == payload["decision"]["account"]


def test_excluded_entries_carry_a_reason(env):
    payload = pick(["pick"], env, snapshots=real_capture())
    excluded = {row["account"]: row["reason"] for row in payload["excluded"]}
    assert "claude_c" in excluded, "the exhausted account should be reported, not dropped"
    assert excluded["claude_c"]


def test_ranked_rows_report_provenance_and_age(env):
    payload = pick(["pick"], env, snapshots=real_capture())
    row = payload["ranked"][0]
    assert row["source"] == "cswap"
    assert row["age_s"] == pytest.approx(0.0)


# ======================================================================================
# The no-proxy hard rail
# ======================================================================================


def test_exec_env_never_contains_proxy_variables(env):
    payload = pick(["pick"], env, snapshots=real_capture())
    exec_env = payload["exec"]["env"]
    assert exec_env, "a winning claude account must still get its config dir"
    for banned in BANNED_EXEC_ENV:
        assert banned not in exec_env
    assert set(exec_env) == {"CLAUDE_CONFIG_DIR"}


def test_exec_env_stays_clean_even_when_the_environment_is_proxied(env):
    env = {**env, "ANTHROPIC_BASE_URL": "http://127.0.0.1:9999", "ANTHROPIC_AUTH_TOKEN": "x"}
    payload = pick(["pick"], env, snapshots=real_capture())
    assert "ANTHROPIC_BASE_URL" not in json.dumps(payload["exec"])
    assert "ANTHROPIC_AUTH_TOKEN" not in json.dumps(payload["exec"])
    assert any("ANTHROPIC_BASE_URL" in warning for warning in payload["warnings"])


def test_config_may_not_inject_a_proxy_variable(env, tmp_path):
    config = tmp_path / "proxy.toml"
    config.write_text(
        '[accounts.claude]\nconfig_dir = "~/.claude"\n'
        '[accounts.claude.env]\nANTHROPIC_BASE_URL = "http://127.0.0.1:9999"\n'
    )
    code, _, err = run(["pick", "--config", str(config)], env, snapshots=real_capture())
    assert code == cli.EXIT_USAGE
    assert "never proxies" in err


def test_exec_removes_inherited_proxy_variables_from_the_child(env):
    runner = Runner()
    env = {**env, "ANTHROPIC_BASE_URL": "http://127.0.0.1:9999", "ANTHROPIC_AUTH_TOKEN": "x"}
    deps = cli.Deps(
        load_snapshots=lambda **kw: (list(real_capture()), []), run=runner
    )
    code, _, _ = run(["exec", "--", "claude", "-p", "hi"], env, deps=deps)

    assert code == 0
    _, kwargs = runner.calls[0]
    child_env = kwargs["env"]
    for banned in BANNED_EXEC_ENV:
        assert banned not in child_env
    assert child_env["CLAUDE_CONFIG_DIR"].endswith(".claude")


# ======================================================================================
# Degradation: pick always answers
# ======================================================================================


def test_oracle_failure_still_produces_a_decision(env):
    def explode(**kwargs):
        raise RuntimeError("cswap exploded")

    payload = pick(["pick"], env, deps=cli.Deps(load_snapshots=explode))
    assert payload["contract_version"] == 1
    assert any("cswap exploded" in warning for warning in payload["warnings"])
    assert payload["degraded"]


def test_no_usage_data_at_all_is_still_valid_json_and_exit_zero(env):
    payload = pick(["pick"], env, snapshots=())
    assert payload["decision"]["account"] is None
    assert payload["decision"]["fits"] is False
    assert payload["degraded"]


def test_missing_decision_layer_degrades_instead_of_crashing(env, monkeypatch):
    monkeypatch.setattr(cli, "_load_engine", lambda: (None, None, "scoring unavailable"))
    payload = pick(["pick"], env, snapshots=real_capture(), deps=cli.Deps())
    assert payload["decision"]["account"] is not None
    assert any("unavailable" in warning for warning in payload["warnings"])


def test_a_raising_decision_layer_degrades_instead_of_crashing(env):
    def explode(*args, **kwargs):
        raise ZeroDivisionError("boom")

    deps = cli.Deps(
        rank=explode, select=explode, load_snapshots=lambda **kw: (list(real_capture()), [])
    )
    payload = pick(["pick"], env, deps=deps)
    assert any("boom" in warning for warning in payload["warnings"])
    assert payload["decision"]["account"] is not None


def test_a_locked_state_file_does_not_stop_a_pick(env, tmp_path):
    fcntl = pytest.importorskip("fcntl")
    from quota_router.state import StateStore

    store = StateStore(env=env)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(store.lock_path, "a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        payload = pick(["pick"], env, snapshots=real_capture())
    finally:
        handle.close()

    assert payload["decision"]["account"] is not None
    assert any("busy" in warning for warning in payload["warnings"])


def test_all_candidates_exhausted_returns_the_earliest_to_reset(env):
    snapshots = (
        account("claude", window("five_hour", 1.0, length_s=FIVE_HOURS, resets_in_s=9000)),
        account("claude_b", window("five_hour", 1.0, length_s=FIVE_HOURS, resets_in_s=600)),
        account("claude_c", window("five_hour", 1.0, length_s=FIVE_HOURS, resets_in_s=4000)),
    )
    payload = pick(["pick"], env, snapshots=snapshots)

    assert payload["decision"]["account"] == "claude_b"
    assert payload["decision"]["fits"] is False
    assert payload["ranked"][0]["fits"] is False


def test_the_exhausted_fallback_still_honors_exclude(env):
    """``--exclude`` is policy: it must hold even when the fallback path takes over."""
    snapshots = (
        account("claude", window("five_hour", 1.0, length_s=FIVE_HOURS, resets_in_s=9000)),
        account("claude_b", window("five_hour", 1.0, length_s=FIVE_HOURS, resets_in_s=600)),
    )
    payload = pick(["pick", "--exclude", "claude_b"], env, snapshots=snapshots)
    assert payload["decision"]["account"] == "claude"


def test_an_unexpected_internal_failure_still_emits_valid_json(env, monkeypatch):
    monkeypatch.setattr(
        cli, "_pick_payload", lambda prepared: (_ for _ in ()).throw(KeyError("nope"))
    )
    code, out, _ = run(["pick"], env, snapshots=real_capture())
    assert code == cli.EXIT_OK
    payload = json.loads(out)
    assert payload["contract_version"] == 1
    assert payload["decision"]["account"] is None
    assert payload["warnings"]


# ======================================================================================
# Flag and config errors -- the only non-zero exits for pick
# ======================================================================================


def test_unknown_flag_exits_two(env):
    code, _, _ = run(["pick", "--nope"], env, snapshots=real_capture())
    assert code == cli.EXIT_USAGE


def test_non_numeric_min_remaining_exits_two(env):
    code, _, _ = run(["pick", "--min-remaining", "lots"], env, snapshots=real_capture())
    assert code == cli.EXIT_USAGE


def test_missing_named_config_exits_two(env, tmp_path):
    code, _, err = run(
        ["pick", "--config", str(tmp_path / "absent.toml")], env, snapshots=real_capture()
    )
    assert code == cli.EXIT_USAGE
    assert "not found" in err


def test_invalid_toml_exits_two(env, tmp_path):
    config = tmp_path / "bad.toml"
    config.write_text("this is not = = toml")
    code, _, err = run(["pick", "--config", str(config)], env, snapshots=real_capture())
    assert code == cli.EXIT_USAGE
    assert "invalid TOML" in err


def test_wrongly_typed_config_value_exits_two(env, tmp_path):
    config = tmp_path / "bad.toml"
    config.write_text('[eligibility]\nmin_remaining = "most of it"\n')
    code, _, err = run(["pick", "--config", str(config)], env, snapshots=real_capture())
    assert code == cli.EXIT_USAGE
    assert "min_remaining" in err


def test_out_of_range_config_value_exits_two(env, tmp_path):
    config = tmp_path / "bad.toml"
    config.write_text("[eligibility]\nmin_remaining = 4.0\n")
    code, _, _ = run(["pick", "--config", str(config)], env, snapshots=real_capture())
    assert code == cli.EXIT_USAGE


def test_unparseable_now_exits_two(env):
    code, _, err = run(["pick", "--now", "yesterday"], env, snapshots=real_capture())
    assert code == cli.EXIT_USAGE
    assert "--now" in err


def test_no_subcommand_prints_help_and_exits_two(env):
    code, out, _ = run([], env)
    assert code == cli.EXIT_USAGE
    assert "quotapick" in out


# ======================================================================================
# The two regimes
# ======================================================================================


def test_surplus_regime_spends_the_quota_most_at_risk(env):
    """Regime A: prefer the pool whose quota will expire unused, not the fullest one."""
    snapshots = (
        account(
            "claude",
            window("seven_day", 0.10, expected_used=0.50, resets_in_s=200_000),
        ),
        account(
            "claude_b",
            window("seven_day", 0.45, expected_used=0.50, resets_in_s=200_000),
        ),
    )
    payload = pick(["pick"], env, snapshots=snapshots)
    assert payload["decision"]["regime"] == REGIME_A
    assert payload["decision"]["account"] == "claude"


def test_scarcity_does_not_let_a_small_account_win_on_shrunken_slack(env):
    """The regression the two-regime split exists to prevent.

    ``claude`` is behind pace by 0.20 with 40% left; ``claude_c`` is behind by 0.50 with
    10% left on a quarter-size plan. Scaling slack by capacity would score claude_c at
    ``-0.125`` against claude's ``-0.200`` and hand the call to the *emptier, smaller*
    pool. Regime B ranks on ``min_remaining`` instead, so the account that can actually
    serve the call wins.
    """
    snapshots = (
        account("claude", window("seven_day", 0.60, expected_used=0.40), tier="max_20x"),
        account("claude_c", window("seven_day", 0.90, expected_used=0.40), tier="max_5x"),
    )
    payload = pick(["pick"], env, snapshots=snapshots)

    assert payload["decision"]["regime"] == REGIME_B
    assert payload["decision"]["account"] == "claude"
    ranked = {row["account"]: row for row in payload["ranked"]}
    assert ranked["claude"]["min_slack"] == pytest.approx(-0.20)
    assert ranked["claude_c"]["min_slack"] == pytest.approx(-0.50)
    assert ranked["claude"]["score"] > ranked["claude_c"]["score"]


def test_the_real_capture_is_a_scarcity_decision(env):
    """Every account in the captured fixture is ahead of pace somewhere."""
    payload = pick(["pick"], env, snapshots=real_capture())
    assert payload["decision"]["regime"] == REGIME_B


# ======================================================================================
# Model classes
# ======================================================================================


def test_model_class_gates_which_window_binds(env):
    fable = pick(["pick", "--model", "fable"], env, snapshots=real_capture())
    opus = pick(["pick", "--model", "claude-opus-4-8"], env, snapshots=real_capture())

    assert fable["decision"]["binding_window"] == "scoped:fable"
    assert opus["decision"]["binding_window"] != "scoped:fable", (
        "a window scoped to fable must be skipped for an opus call"
    )


def test_model_flag_accepts_a_real_model_string_and_a_class_name(env):
    by_id = pick(["pick", "--model", "claude-fable-5[1m]"], env, snapshots=real_capture())
    by_class = pick(["pick", "--model", "fable"], env, snapshots=real_capture())
    assert by_id["decision"]["binding_window"] == by_class["decision"]["binding_window"]
    assert by_id["decision"]["account"] == by_class["decision"]["account"]


def test_unrecognized_model_keeps_every_window_applicable(env):
    """Guessing wrong must never *remove* a constraint, so an unknown model gates nothing."""
    payload = pick(["pick", "--model", "totally-new-model-9"], env, snapshots=real_capture())
    assert any("matched no model-class pattern" in w for w in payload["warnings"])
    assert payload["decision"]["binding_window"] == "scoped:fable"


def test_classify_recognizes_the_scoped_class_and_its_neighbours():
    assert classify("claude-fable-5[1m]") == "fable"
    assert classify("claude-fable-5") == "fable"
    assert classify("claude-opus-4-8") == "opus"
    assert classify("claude-3-5-sonnet-20241022") == "sonnet"
    assert classify("claude-haiku-4-5") == "haiku"
    assert classify("gpt-5.4") == "gpt"
    assert classify("us.anthropic.claude-opus-4-8-v1:0") == "opus"
    assert classify("something-unheard-of") is None


def test_resolve_prefers_an_exact_class_name_over_a_pattern():
    assert resolve("fable").source == "class"
    assert resolve("claude-fable-5[1m]").source == "pattern"


def test_demand_multiplier_scales_cheap_classes_down():
    assert demand_multiplier("opus") == 1.0
    assert demand_multiplier("haiku") == 0.01
    assert demand_multiplier("something-unknown") == 1.0, "unknown must assume expensive"


# ======================================================================================
# Filters
# ======================================================================================


def test_only_restricts_the_candidate_set(env):
    payload = pick(["pick", "--only", "claude_b"], env, snapshots=real_capture())
    assert payload["decision"]["account"] == "claude_b"
    assert {row["account"] for row in payload["ranked"]} == {"claude_b"}


def test_only_accepts_a_comma_separated_list(env):
    payload = pick(["pick", "--only", "claude,claude_b"], env, snapshots=real_capture())
    assert {row["account"] for row in payload["ranked"]} <= {"claude", "claude_b"}


def test_exclude_removes_an_account(env):
    payload = pick(["pick", "--exclude", "claude"], env, snapshots=real_capture())
    assert payload["decision"]["account"] != "claude"
    assert "claude" in {row["account"] for row in payload["excluded"]}


def test_min_remaining_can_exclude_everyone_without_failing(env):
    """A policy floor is not exhaustion, and ``fits`` must keep saying which one it was.

    With a 99% floor nothing is *eligible*, but claude still has 20% of its Fable window
    and can genuinely serve the call. Reporting ``fits=False`` here would tell the
    consumer to stop retrying an account that works.
    """
    payload = pick(["pick", "--min-remaining", "0.99"], env, snapshots=real_capture())

    assert len(payload["excluded"]) == 3
    assert all("min-remaining 99.0%" in row["reason"] for row in payload["excluded"])
    assert payload["decision"]["account"] is not None
    assert payload["decision"]["fits"] is True


def test_a_genuinely_exhausted_field_reports_fits_false(env):
    payload = pick(["pick"], env, snapshots=(
        account("claude", window("five_hour", 1.0, length_s=FIVE_HOURS, resets_in_s=600)),
    ))
    assert payload["decision"]["fits"] is False


def test_a_missing_decision_layer_does_not_claim_healthy_accounts_are_spent(env, monkeypatch):
    monkeypatch.setattr(cli, "_load_engine", lambda: (None, None, "scoring unavailable"))
    payload = pick(["pick"], env, snapshots=real_capture(), deps=cli.Deps())
    assert payload["decision"]["fits"] is True, (
        "the scoring layer being absent says nothing about whether the account works"
    )
    assert "out of quota" not in payload["decision"]["reason"]


# ======================================================================================
# State: stickiness, pileup, dry run
# ======================================================================================


def test_dry_run_writes_no_state(env, tmp_path):
    pick(["pick", "--dry-run"], env, snapshots=real_capture())
    state_files = list((tmp_path / "state").rglob("state.json"))
    assert state_files == []


def test_a_pick_records_state_for_the_next_invocation(env, tmp_path):
    pick(["pick"], env, snapshots=real_capture())
    state = json.loads(next((tmp_path / "state").rglob("state.json")).read_text())
    assert state["sticky"]["*"]["account"]
    assert state["reservations"]


def test_pileup_spreads_concurrent_callers_across_pools(env, tmp_path):
    """Two picks in the same second must not both land on the same account.

    ``calls_per_window = 2`` makes one call worth half a window, which is the scale at
    which a reservation is visible; dwell is disabled so hysteresis is not what is being
    measured.
    """
    config = tmp_path / "pileup.toml"
    config.write_text(
        "[pileup]\ncalls_per_window = 2\nwindow_s = 600\n"
        "[hysteresis]\nmin_dwell_calls = 0\n"
    )
    snapshots = (
        account("claude", window("seven_day", 0.50, expected_used=0.50)),
        account("claude_b", window("seven_day", 0.52, expected_used=0.50)),
    )
    first = pick(["pick", "--config", str(config)], env, snapshots=snapshots)
    second = pick(["pick", "--config", str(config)], env, snapshots=snapshots)

    assert first["decision"]["account"] == "claude"
    assert second["decision"]["account"] == "claude_b"
    assert any("pileup" in warning for warning in second["warnings"])


def test_hysteresis_keeps_the_incumbent_across_invocations(env, tmp_path):
    """A near-tie must not flap: the dwell holds the seat for the first few calls."""
    snapshots = (
        account("claude", window("seven_day", 0.50, expected_used=0.50)),
        account("claude_b", window("seven_day", 0.505, expected_used=0.50)),
    )
    first = pick(["pick"], env, snapshots=snapshots)
    flipped = tuple(reversed(snapshots))
    second = pick(["pick"], env, snapshots=flipped)
    assert first["decision"]["account"] == second["decision"]["account"] == "claude"


def test_a_fallback_pick_does_not_erase_the_selection_state(env, tmp_path):
    """A degraded decision skips the selection layer; it must not wipe what it skipped."""
    pick(["pick"], env, snapshots=real_capture())
    state_file = next((tmp_path / "state").rglob("state.json"))
    before = json.loads(state_file.read_text())["selection"]
    assert before, "the first pick should have recorded selection state"

    exhausted = (
        account("claude", window("five_hour", 1.0, length_s=FIVE_HOURS, resets_in_s=600)),
    )
    pick(["pick"], env, snapshots=exhausted, now_s=NOW + 1)

    assert json.loads(state_file.read_text())["selection"] == before


def test_no_sticky_ignores_the_incumbent(env):
    snapshots = (
        account("claude", window("seven_day", 0.50, expected_used=0.50)),
        account("claude_b", window("seven_day", 0.30, expected_used=0.50)),
    )
    pick(["pick", "--only", "claude"], env, snapshots=snapshots)
    payload = pick(["pick", "--no-sticky"], env, snapshots=snapshots)
    assert payload["decision"]["account"] == "claude_b"
    assert payload["decision"]["sticky"] is False


# ======================================================================================
# exec
# ======================================================================================


def test_exec_passes_the_child_exit_code_through_unchanged(env):
    for expected in (0, 1, 42, 130):
        runner = Runner(returncode=expected)
        deps = cli.Deps(
            load_snapshots=lambda **kw: (list(real_capture()), []), run=runner
        )
        code, _, _ = run(["exec", "--", "claude", "-p", "hi"], env, deps=deps)
        assert code == expected


def test_exec_spawns_the_requested_command_with_the_account_config_dir(env):
    runner = Runner()
    deps = cli.Deps(load_snapshots=lambda **kw: (list(real_capture()), []), run=runner)
    run(["exec", "--", "claude", "-p", "hello"], env, deps=deps)

    argv, kwargs = runner.calls[0]
    assert argv == ["claude", "-p", "hello"]
    assert kwargs["env"]["CLAUDE_CONFIG_DIR"].endswith(".claude")


def test_exec_defaults_to_the_providers_own_cli(env):
    runner = Runner()
    deps = cli.Deps(load_snapshots=lambda **kw: (list(real_capture()), []), run=runner)
    run(["exec", "--", "-p", "hello"], env, deps=deps)
    argv, _ = runner.calls[0]
    assert argv == ["claude", "-p", "hello"]


def test_exec_reports_a_router_failure_when_the_binary_is_missing(env):
    runner = Runner(error=FileNotFoundError("claude"))
    deps = cli.Deps(load_snapshots=lambda **kw: (list(real_capture()), []), run=runner)
    code, _, err = run(["exec", "--", "claude"], env, deps=deps)
    assert code == cli.EXIT_ROUTER_FAILURE
    assert "command not found" in err


def test_exec_reports_a_router_failure_when_nothing_can_serve_the_call(env):
    runner = Runner()
    deps = cli.Deps(load_snapshots=lambda **kw: ([], []), run=runner)
    code, _, err = run(["exec", "--", "claude"], env, deps=deps)
    assert code == cli.EXIT_ROUTER_FAILURE
    assert runner.calls == []
    assert "no account" in err


def test_exec_dry_run_spawns_nothing(env):
    runner = Runner()
    deps = cli.Deps(load_snapshots=lambda **kw: (list(real_capture()), []), run=runner)
    code, out, _ = run(["exec", "--dry-run", "--json", "--", "claude"], env, deps=deps)
    assert code == cli.EXIT_OK
    assert runner.calls == []
    payload = json.loads(out)
    assert payload["argv"] == ["claude"]
    for banned in BANNED_EXEC_ENV:
        assert banned not in payload["env"]


# ======================================================================================
# status / explain / calibrate
# ======================================================================================


def test_status_reports_every_window(env):
    code, out, _ = run(["status"], env, snapshots=real_capture())
    assert code == cli.EXIT_OK
    assert "claude_c" in out
    assert "5h" in out and "7d" in out and "fable" in out


def test_status_json_carries_the_full_snapshot(env):
    code, out, _ = run(["status", "--json"], env, snapshots=real_capture())
    payload = json.loads(out)
    assert code == cli.EXIT_OK
    assert {a["id"] for a in payload["accounts"]} == {"claude", "claude_b", "claude_c"}
    assert payload["accounts"][0]["windows"]


def test_status_does_not_record_a_pick(env, tmp_path):
    run(["status"], env, snapshots=real_capture())
    history = list((tmp_path / "state").rglob("history.jsonl"))
    assert history, "status should still record the snapshot it observed"
    records = [json.loads(line) for line in history[0].read_text().splitlines()]
    assert all(record["chosen"] is None for record in records)


def test_explain_names_the_binding_window_and_the_regime(env):
    code, out, _ = run(["explain"], env, snapshots=real_capture())
    assert code == cli.EXIT_OK
    assert "binds" in out
    assert "regime" in out


def test_explain_json_is_the_decision_contract(env):
    code, out, _ = run(["explain", "--json"], env, snapshots=real_capture())
    payload = json.loads(out)
    assert code == cli.EXIT_OK
    assert payload["contract_version"] == 1
    assert payload["chosen"]
    assert payload["ranked"]


def test_calibrate_says_so_when_there_is_no_evidence(env):
    code, out, _ = run(["calibrate"], env, snapshots=real_capture())
    assert code == cli.EXIT_OK
    assert "history" in out


def test_calibrate_estimates_calls_per_window_from_recorded_picks(env):
    """Six recorded picks against 10% of a window observed burning -> ~60 calls fit.

    Only five of the six picks have a *following* snapshot to show their burn, so the
    estimate is biased low by design (see :func:`quota_router.history.calibrate`); the
    reservation adjustment must not appear in the denominator at all.
    """
    used = 0.0
    for index in range(6):
        snapshots = (
            account("claude", window("five_hour", used, length_s=FIVE_HOURS, resets_in_s=9000)),
        )
        run(["pick"], env, snapshots=snapshots, now_s=NOW + index)
        used += 0.02

    code, out, _ = run(["calibrate", "--json"], env, snapshots=(), now_s=NOW + 100)
    payload = json.loads(out)
    assert code == cli.EXIT_OK
    assert payload["accounts"]["claude"]["consumed"] == pytest.approx(0.10)
    assert payload["accounts"]["claude"]["demand"] == pytest.approx(6.0)
    assert payload["accounts"]["claude"]["calls_per_window"] == pytest.approx(60.0)


def test_history_records_observed_usage_not_the_routers_own_adjustment(env, tmp_path):
    """Pileup reservations are a routing device; the log has to stay ground truth."""
    config = tmp_path / "pileup.toml"
    config.write_text("[pileup]\ncalls_per_window = 2\nwindow_s = 600\n")
    snapshots = (account("claude", window("five_hour", 0.40, length_s=FIVE_HOURS)),)

    run(["pick", "--config", str(config)], env, snapshots=snapshots)
    run(["pick", "--config", str(config)], env, snapshots=snapshots, now_s=NOW + 1)

    history = next((tmp_path / "state").rglob("history.jsonl"))
    records = [json.loads(line) for line in history.read_text().splitlines()]
    for record in records:
        assert record["accounts"][0]["windows"][0]["used_fraction"] == pytest.approx(0.40)


# ======================================================================================
# Configuration
# ======================================================================================


def test_runs_with_no_configuration_at_all(env):
    """Zero-config is a requirement: the builtin layer must be sufficient."""
    payload = pick(["pick"], env, snapshots=real_capture())
    assert payload["decision"]["account"]
    assert payload["exec"]["env"]["CLAUDE_CONFIG_DIR"]


def test_layers_apply_in_order_with_the_named_file_winning(env, tmp_path):
    xdg = Path(env["XDG_CONFIG_HOME"]) / "quota-router"
    xdg.mkdir(parents=True)
    (xdg / "config.toml").write_text("[eligibility]\nmin_remaining = 0.10\n")

    project = tmp_path / "project"
    project.mkdir()
    (project / ".quota-router.toml").write_text("[eligibility]\nmin_remaining = 0.20\n")

    override = tmp_path / "over.toml"
    override.write_text("[eligibility]\nmin_remaining = 0.30\n")

    # claude_b has 15% left, so the threshold that was actually in force is legible in
    # whether it survived -- and, when it did not, in the reason it was given.
    snapshots = (
        account("claude", window("seven_day", 0.50, expected_used=0.50)),
        account("claude_b", window("seven_day", 0.85, expected_used=0.50)),
    )

    from_xdg = pick(["pick"], env, snapshots=snapshots, cwd=str(tmp_path))
    from_project = pick(["pick"], env, snapshots=snapshots, cwd=str(project))
    from_flag = pick(
        ["pick", "--config", str(override)], env, snapshots=snapshots, cwd=str(project)
    )

    def reason_for(payload, account_id):
        return next(r["reason"] for r in payload["excluded"] if r["account"] == account_id)

    assert {r["account"] for r in from_xdg["ranked"]} == {"claude", "claude_b"}
    assert "min-remaining 20.0%" in reason_for(from_project, "claude_b")
    assert "min-remaining 30.0%" in reason_for(from_flag, "claude_b")


def test_a_cli_flag_beats_every_config_file(env, tmp_path):
    config = tmp_path / "floor.toml"
    config.write_text("[eligibility]\nmin_remaining = 0.30\n")
    snapshots = (
        account("claude", window("seven_day", 0.50, expected_used=0.50)),
        account("claude_b", window("seven_day", 0.85, expected_used=0.50)),
    )
    payload = pick(
        ["pick", "--config", str(config), "--min-remaining", "0.05"], env, snapshots=snapshots
    )
    assert {r["account"] for r in payload["ranked"]} == {"claude", "claude_b"}


def test_deep_merge_keeps_untouched_builtin_values(env, tmp_path):
    config = tmp_path / "partial.toml"
    config.write_text("[providers]\ncodex = 0.1\n")
    payload = pick(["pick", "--config", str(config)], env, snapshots=real_capture())
    assert payload["exec"]["env"]["CLAUDE_CONFIG_DIR"], "builtin accounts must survive"


def test_account_config_dir_is_expanded(env, tmp_path):
    config = tmp_path / "dirs.toml"
    config.write_text('[accounts.claude]\nconfig_dir = "$HOME/custom-claude"\n')
    payload = pick(["pick", "--only", "claude", "--config", str(config)], env,
                   snapshots=real_capture())
    assert payload["exec"]["env"]["CLAUDE_CONFIG_DIR"] == f"{env['HOME']}/custom-claude"


def test_a_disabled_account_is_never_routed_to(env, tmp_path):
    config = tmp_path / "off.toml"
    config.write_text("[accounts.claude]\nenabled = false\n")
    payload = pick(["pick", "--config", str(config)], env, snapshots=real_capture())
    assert payload["decision"]["account"] != "claude"


def test_unknown_config_section_warns_but_does_not_fail(env, tmp_path):
    config = tmp_path / "future.toml"
    config.write_text("[telepathy]\nenabled = true\n")
    payload = pick(["pick", "--config", str(config)], env, snapshots=real_capture())
    assert any("telepathy" in warning for warning in payload["warnings"])


def test_tier_capacity_override_is_reported_as_not_applied(env, tmp_path):
    """Capacity comes from the shared tier table; an override here would be a lie."""
    config = tmp_path / "tiers.toml"
    config.write_text("[tiers]\nmax_5x = 0.9\n")
    payload = pick(["pick", "--config", str(config)], env, snapshots=real_capture())
    assert any("not applied" in warning for warning in payload["warnings"])


def test_now_accepts_epoch_and_iso_forms(env):
    snapshots = real_capture()
    by_epoch = pick(["pick", "--now", str(NOW)], env, snapshots=snapshots, now_s=0.0)
    by_iso = pick(["pick", "--now", "2026-08-14T18:46:52Z"], env, snapshots=snapshots, now_s=0.0)
    assert by_epoch["decision"]["account"] == by_iso["decision"]["account"]
    assert by_epoch["generated_at"] == by_iso["generated_at"]


# ======================================================================================
# History
# ======================================================================================


def test_a_pick_appends_the_snapshot_to_history(env, tmp_path):
    pick(["pick", "--model", "fable"], env, snapshots=real_capture())
    history = next((tmp_path / "state").rglob("history.jsonl"))
    record = json.loads(history.read_text().splitlines()[-1])

    assert record["chosen"]
    assert record["model_class"] == "fable"
    assert {a["id"] for a in record["accounts"]} == {"claude", "claude_b", "claude_c"}


def test_history_is_replayed_when_the_oracle_goes_away(env):
    """Headless accounts have no other observability, so the log is also the cache."""
    pick(["pick"], env, snapshots=real_capture())

    def explode(**kwargs):
        raise RuntimeError("cswap is gone")

    payload = pick(["pick"], env, deps=cli.Deps(load_snapshots=explode), now_s=NOW + 60)
    assert payload["decision"]["account"] is not None
    assert any("replaying the history snapshot" in w for w in payload["warnings"])
    assert all(row["source"] == "cache" for row in payload["ranked"])


def test_a_stale_cache_is_not_believed(env):
    pick(["pick"], env, snapshots=real_capture())

    def explode(**kwargs):
        raise RuntimeError("cswap is gone")

    payload = pick(
        ["pick"], env, deps=cli.Deps(load_snapshots=explode), now_s=NOW + 86_400
    )
    assert payload["decision"]["account"] is None
    assert any("too old" in warning for warning in payload["warnings"])


def test_history_rotates_on_a_day_boundary(env, tmp_path):
    pick(["pick"], env, snapshots=real_capture())
    history = next((tmp_path / "state").rglob("history.jsonl"))

    pick(["pick"], env, snapshots=real_capture(), now_s=NOW + 86_400)

    rotated = sorted(history.parent.glob("history-*.jsonl"))
    assert len(rotated) == 1
    assert "2026-08-14" in rotated[0].name
    assert len(history.read_text().splitlines()) == 1, "the live file restarts after rotation"


def test_history_rotates_on_the_size_cap(env, tmp_path):
    from quota_router.history import append_snapshots

    target = tmp_path / "history.jsonl"
    for index in range(4):
        append_snapshots(
            real_capture(), now_s=NOW + index, path=target, max_bytes=1_000, chosen="claude"
        )
    assert list(tmp_path.glob("history-*.jsonl")), "an oversized log must be rotated away"
    assert target.stat().st_size < 10_000


def test_history_prunes_rotated_files_past_the_retention_window(env, tmp_path):
    import os

    from quota_router.history import append_snapshots

    target = tmp_path / "history.jsonl"
    stale = tmp_path / "history-2020-01-01.jsonl"
    stale.write_text("{}\n")
    os.utime(stale, (NOW - 90 * 86_400, NOW - 90 * 86_400))

    # Pruning is deliberately coupled to rotation (rotation fires daily, so retention
    # is enforced daily). Trigger it the way production does -- by appending on a later
    # logical day -- rather than by back-dating an mtime: rotation keys off the day of
    # the DATA now, so a doctored mtime no longer causes one.
    append_snapshots(real_capture(), now_s=NOW, path=target)
    append_snapshots(real_capture(), now_s=NOW + 86_400, path=target, keep_days=30)

    assert not stale.exists()


def test_history_skips_a_truncated_final_line(env, tmp_path):
    from quota_router.history import append_snapshots, iter_records

    target = tmp_path / "history.jsonl"
    append_snapshots(real_capture(), now_s=NOW, path=target, chosen="claude")
    with open(target, "a", encoding="utf-8") as handle:
        handle.write('{"v": 1, "t": 123, "accou')  # a crash mid-append

    records = list(iter_records(target))
    assert len(records) == 1


def test_history_write_failure_does_not_stop_a_pick(env, tmp_path):
    """A full disk must cost observability, not the caller's invocation."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    payload = pick(["pick"], {**env, "QUOTA_ROUTER_HISTORY": str(blocker / "h.jsonl")},
                   snapshots=real_capture())
    assert payload["decision"]["account"] is not None


def test_the_env_var_config_layer_is_honored(env, tmp_path):
    config = tmp_path / "from-env.toml"
    config.write_text("[eligibility]\nmin_remaining = 0.50\n")
    payload = pick(
        ["pick"], {**env, "QUOTA_ROUTER_CONFIG": str(config)}, snapshots=real_capture()
    )
    assert all("min-remaining 50.0%" in row["reason"] for row in payload["excluded"])


def test_a_missing_env_var_config_is_an_error_not_a_shrug(env, tmp_path):
    code, _, err = run(
        ["pick"],
        {**env, "QUOTA_ROUTER_CONFIG": str(tmp_path / "gone.toml")},
        snapshots=real_capture(),
    )
    assert code == cli.EXIT_USAGE
    assert "not found" in err


def test_state_location_override_is_honored(env, tmp_path):
    target = tmp_path / "custom-state.json"
    pick(["pick"], {**env, "QUOTA_ROUTER_STATE": str(target)}, snapshots=real_capture())
    assert target.exists()


# ======================================================================================
# explain rendering
# ======================================================================================


def test_explain_renders_the_canonical_one_liner():
    """The line an operator gets when they ask why: window, arithmetic, margin."""
    binding = WindowSlack(
        key="seven_day",
        applicable=True,
        remaining_fraction=0.61,
        expected_used_fraction=0.83,
        slack=0.44,
        time_to_reset_s=4.1 * 3600.0,
        length_s=SEVEN_DAYS,
        binding=True,
    )
    winner = ScoreBreakdown(
        account_id="claude_b",
        score=0.44,
        min_slack=0.44,
        binding_window="seven_day",
        regime=REGIME_A,
        per_window=(binding,),
    )
    runner_up = ScoreBreakdown(
        account_id="claude", score=-0.04, min_slack=-0.04, binding_window="five_hour"
    )
    decision = Decision(chosen="claude_b", ranked=(winner, runner_up), regime=REGIME_A)

    assert explain_decision(decision, margin=0.15) == (
        "claude_b won: 7d binds (0.44 slack = 0.61 remaining - 0.17 expected over 4.1h "
        "to reset); beat claude by 48% > 15% margin"
    )


def test_explain_says_when_the_incumbent_was_kept():
    winner = ScoreBreakdown(account_id="claude", min_slack=0.30, binding_window="five_hour")
    challenger = ScoreBreakdown(
        account_id="claude_b", min_slack=0.33, binding_window="five_hour"
    )
    decision = Decision(
        chosen="claude",
        ranked=(winner, challenger),
        regime=REGIME_A,
        sticky_applied=True,
    )
    line = explain_decision(decision, margin=0.15)
    assert "kept (sticky)" in line
    assert "claude_b led by 3% < 15% margin" in line


def test_explain_reports_a_scarcity_decision_differently():
    row = WindowSlack(
        key="scoped:fable",
        remaining_fraction=0.12,
        expected_used_fraction=0.55,
        slack=-0.33,
        time_to_reset_s=7200.0,
        binding=True,
    )
    breakdown = ScoreBreakdown(
        account_id="claude",
        min_slack=-0.33,
        min_remaining=0.12,
        binding_window="scoped:fable",
        regime=REGIME_B,
        per_window=(row,),
    )
    decision = Decision(chosen="claude", ranked=(breakdown,), regime=REGIME_B)
    line = explain_decision(decision)
    assert "fable binds" in line
    assert "0.12 remaining" in line
    assert "nobody has surplus" in line, "regime B is 'no surplus', not 'nobody is behind'"


def test_explain_does_not_quote_a_margin_that_did_not_decide_anything():
    """Winning on provider weight while *behind* on the objective needs saying plainly."""
    winner = ScoreBreakdown(
        account_id="claude", score=0.39, min_slack=-0.06, min_remaining=0.39, regime=REGIME_B
    )
    runner_up = ScoreBreakdown(
        account_id="codex", score=0.28, min_slack=-0.37, min_remaining=0.395, regime=REGIME_B
    )
    decision = Decision(chosen="claude", ranked=(winner, runner_up), regime=REGIME_B)

    line = explain_decision(decision, margin=-0.03)
    assert "beat codex on weight x capacity" in line
    assert "margin" not in line, "the margin was not the test that ran"


def test_explain_handles_having_no_choice_at_all():
    decision = Decision(chosen=None, reason="everything is spent")
    assert "no account could serve this call" in explain_decision(decision)


# ======================================================================================
# Live -- the real machine. Deselected by default; run with `pytest -m live`.
# ======================================================================================


@pytest.mark.live
def test_live_pick_against_the_real_oracle(tmp_path):
    """End to end on the operator's own machine, reading real quota.

    Reads only: the providers layer's single permitted invocation is ``cswap list
    --json``. State and history are redirected into the test's temp dir so a live run
    never disturbs the operator's real router state.
    """
    import os

    live_env = {
        **os.environ,
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["pick", "--dry-run"], env=live_env, stdout=out, stderr=err)
    payload = json.loads(out.getvalue())

    assert code == cli.EXIT_OK
    assert payload["contract_version"] == QUOTA_ROUTER_CONTRACT_VERSION
    assert payload["ranked"], "the real machine should have at least one routable account"
    for banned in BANNED_EXEC_ENV:
        assert banned not in payload["exec"]["env"]


def test_explain_reports_the_same_exclusions_as_pick(env):
    """`explain` and `pick` must agree on the excluded set.

    Regression: `pick` rendered ``decision.excluded + prepared.cli_excluded`` while
    `explain` rendered only ``decision.excluded``, so an account dropped at the
    CLI/config layer (eligibility floor, --only/--exclude) vanished from the human
    view while still appearing in the JSON audit trail. A reader who cannot see WHY
    an account is absent stops trusting the output.
    """
    snaps = real_capture()

    def accounts(argv):
        payload = pick(argv, env, snapshots=snaps)
        # `pick` emits the contract shape (key "account"); `explain --json` dumps the
        # raw Decision (key "account_id"). The regression is about WHICH accounts are
        # reported, not how the key is spelled.
        return {
            row.get("account") or row.get("account_id")
            for row in payload.get("excluded", [])
        }

    assert accounts(["explain", "--json", "--model", "fable"]) == accounts(
        ["pick", "--json", "--model", "fable"]
    )
