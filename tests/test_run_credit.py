"""``quotapick run`` on an API credit pool, and the key that makes it possible.

What must hold:

* A credit pool competes only through the joint projection, only for ``run`` work, and
  only when its balance covers the expected cost times the admission margin.
* When the subscriptions would be drained anyway and the pool would expire unused, the
  job goes to the pool (the case the per-account objective gets wrong).
* The child runs with ``--bare`` (Anthropic auth strictly the API key; OAuth and the
  Keychain never read), an empty router-owned config dir, the key in its environment,
  and a per-call budget. The served auth must be the key.
* The ledger books a hold, then the spend; an exhaustion signal empties the pool.
* The key appears nowhere: not in stdout, the returned dict, history, state, the
  ledger, the forecast, or the text of any exception -- even when the child echoes it.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from quota_router import cli, credits, forecast
from quota_router.secrets import Secret
from tests.test_cli import FIVE_HOURS, NOW, _NO_CONFIG_FILES, account, run, window
from tests.test_run import MODEL, FakeRunner, _stream

HOUR = 3600.0
SENTINEL = "sk-ant-api03-SENTINEL-KEY-0123456789"


def _iso(epoch_s: float) -> str:
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _config(expires_s: float, extra: str = "") -> str:
    return (
        "[engine]\nusd_per_pse = 305\n\n"
        "[credit_pools.anthropic_api_d]\n"
        'provider = "anthropic_api"\n'
        'keychain_service = "anthropic-api-key-claude-d"\n'
        'linked_account = "claude_d"\n'
        "grant_usd = 200\n"
        f'expires = "{_iso(expires_s)}"\n'
        'renewal = "none"\n'
        "card_on_file = false\n" + extra
    )


@pytest.fixture()
def env(tmp_path):
    xdg = tmp_path / "config" / "quota-router"
    xdg.mkdir(parents=True)
    (xdg / "config.toml").write_text(_config(NOW + 100 * HOUR), encoding="utf-8")
    environ = {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PATH": "/usr/bin:/bin",
        "QUOTA_ROUTER_CLAUDE_BIN": "/opt/test/claude",
    }
    _forecast(environ)
    return environ


def _forecast(env, *, other=1.0, run_rate=0.0):
    group = forecast.GroupForecast(
        other=forecast.StreamRate(pse_per_h=other, fable_share=0.0),
        run=forecast.StreamRate(pse_per_h=run_rate, fable_share=0.0),
        coverage=1.0, accounts=("claude_b", "claude_d"), measured_pse=other * 168,
        manual_pse_per_h=0.0,
    )
    forecast.write_cache(
        forecast.Forecast(computed_at_s=NOW, window_s=7 * 86400.0, groups={"claude": group}),
        env=env,
    )


def _drained_subscriptions():
    """Two subscriptions that 1 PSE/h of other demand empties before their resets."""
    def acct(account_id, weekly, reset_h):
        return account(
            account_id,
            window("5h", 0.1, length_s=FIVE_HOURS, resets_in_s=2 * HOUR),
            window("7d", weekly, resets_in_s=reset_h * HOUR),
        )
    return (acct("claude_b", 0.9, 5), acct("claude_d", 0.9, 6))


def _secret_reader(calls):
    def read(service):
        calls.append(service)
        return Secret(SENTINEL)
    return read


def _run(env, request, runner, *, secrets_read=None, flags=(), snapshots=None):
    deps = cli.Deps(
        load_snapshots=lambda **kw: (list(snapshots or _drained_subscriptions()), []),
        run=runner,
        read_secret=_secret_reader(secrets_read if secrets_read is not None else []),
    )
    code, out, err = run(["run", *flags], env, deps=deps, cwd=_NO_CONFIG_FILES,
                         stdin=json.dumps(request))
    return code, json.loads(out), err


def _request(**kw):
    return {"model": MODEL, "prompt": "Say ok.", **kw}


def _ledger(env):
    lines, _ = credits.read(env)
    return lines


def test_a_pool_that_would_expire_unused_wins_over_drained_subscriptions(env) -> None:
    reads = []
    runner = FakeRunner((0, _stream(api_key_source="ANTHROPIC_API_KEY", cost=0.02), ""))
    code, payload, err = _run(env, _request(), runner, secrets_read=reads)
    assert code == cli.EXIT_OK, err
    assert payload["served"]["account"] == "anthropic_api_d"
    assert payload["served"]["pool_kind"] == "credit"
    assert payload["served"]["provider"] == "anthropic_api"
    assert payload["served"]["auth"] == "api_key"
    assert reads == ["anthropic-api-key-claude-d"]
    call = runner.calls[0]
    assert "--bare" in call["argv"]
    budget = float(call["argv"][call["argv"].index("--max-budget-usd") + 1])
    assert 0 < budget <= 10.0
    assert call["env"]["ANTHROPIC_API_KEY"] == SENTINEL
    home = Path(call["env"]["CLAUDE_CONFIG_DIR"])
    assert home.name == "anthropic_api_d" and home.parent.name == "credit-homes"
    kinds = [line["type"] for line in _ledger(env)]
    assert kinds == ["hold", "spend"]
    assert _ledger(env)[1]["usd"] == pytest.approx(0.02)


def test_allow_credit_false_keeps_the_job_on_a_subscription(env) -> None:
    runner = FakeRunner((0, _stream(), ""))
    _code, payload, _err = _run(env, _request(allow_credit=False), runner)
    assert payload["served"]["pool_kind"] == "subscription"
    assert "ANTHROPIC_API_KEY" not in runner.calls[0]["env"]


def test_a_pool_below_the_admission_bar_is_not_used(env) -> None:
    credits.append(env, {"t": NOW - 10, "pool": "anthropic_api_d", "type": "checkpoint",
                         "remaining_usd": 0.10})
    runner = FakeRunner((0, _stream(), ""))
    _code, payload, _err = _run(env, _request(expected_cost_usd=0.5), runner)
    assert payload["served"]["pool_kind"] == "subscription"


def test_without_a_forecast_credit_pools_sit_out_and_it_is_said(env) -> None:
    forecast.forecast_path(env).unlink()
    runner = FakeRunner((0, _stream(), ""))
    _code, payload, _err = _run(env, _request(), runner)
    assert payload["served"]["pool_kind"] == "subscription"
    assert any("credit pools" in w for w in payload["warnings"])


def test_exhaustion_empties_the_pool_and_the_job_moves_on(env) -> None:
    # A call refused for lack of credit costs nothing, so its hold is released.
    refused = _stream(is_error=True, subtype="error_during_execution", cost=0.0,
                      text="Your credit balance is too low to access the Anthropic API.",
                      api_key_source="ANTHROPIC_API_KEY")
    runner = FakeRunner((1, refused, ""), (0, _stream(), ""))
    _code, payload, _err = _run(env, _request(), runner)
    assert [a["outcome"] for a in payload["attempts"]] == ["exhausted", "ok"]
    assert payload["served"]["pool_kind"] == "subscription"
    lines = _ledger(env)
    assert [line["type"] for line in lines] == ["hold", "release", "exhausted"]
    config = cli.load_config(env=env, cwd=_NO_CONFIG_FILES)
    pool = config.credit_pools["anthropic_api_d"]
    assert credits.balance(pool, lines, now_s=NOW + 1).remaining_usd == 0.0


def test_a_subscription_auth_on_a_credit_run_is_refused(env) -> None:
    runner = FakeRunner((0, _stream(api_key_source="none"), ""), (0, _stream(), ""))
    _code, payload, _err = _run(env, _request(), runner)
    assert payload["attempts"][0]["outcome"] == "auth"
    assert "apiKeySource" in payload["attempts"][0]["detail"]


def test_only_a_pool_never_falls_back_to_a_subscription(env) -> None:
    refused = _stream(is_error=True, subtype="error_during_execution", cost=0.0,
                      text="Your credit balance is too low to access the Anthropic API.",
                      api_key_source="ANTHROPIC_API_KEY")
    runner = FakeRunner((1, refused, ""), (0, _stream(), ""))
    _code, payload, _err = _run(env, _request(), runner, flags=["--only", "anthropic_api_d"])
    assert len(runner.calls) == 1
    assert payload["ok"] is False
    assert [a["account"] for a in payload["attempts"]] == ["anthropic_api_d"]
    # No subscription served, so none is recorded as picked: a phantom pick would read
    # as load on that account in the next decision.
    history = Path(env["XDG_STATE_HOME"]) / "quota-router" / "history.jsonl"
    chosen = [json.loads(line).get("chosen") for line in history.read_text().splitlines()]
    assert not {"claude_b", "claude_d"} & set(chosen)


def test_excluding_the_pool_keeps_the_job_on_a_subscription_and_reads_no_key(env) -> None:
    reads = []
    runner = FakeRunner((0, _stream(), ""))
    _code, payload, err = _run(env, _request(), runner, secrets_read=reads,
                               flags=["--exclude", "anthropic_api_d"])
    assert payload["served"]["pool_kind"] == "subscription", err
    assert reads == []


def test_only_a_subscription_reads_no_key(env) -> None:
    reads = []
    runner = FakeRunner((0, _stream(), ""))
    _code, payload, err = _run(env, _request(), runner, secrets_read=reads,
                               flags=["--only", "claude_b"])
    assert payload["served"]["account"] == "claude_b", err
    assert reads == []


def _wasting_subscriptions():
    """Two subscriptions that reset in 5h and 6h with most of their week unspent."""
    def acct(account_id, reset_h):
        return account(
            account_id,
            window("5h", 0.1, length_s=FIVE_HOURS, resets_in_s=2 * HOUR),
            window("7d", 0.3, resets_in_s=reset_h * HOUR),
        )
    return (acct("claude_b", 5), acct("claude_d", 6))


def test_a_waste_tie_goes_to_the_credit_pool(env) -> None:
    # On 2026-10-18 a subscription whose week reset in 11h beat a pool expiring in 21h
    # on the deadline alone, although both avoided the whole job. Only `run` can spend a
    # pool; a subscription's projected waste rests on a forecast of cl and interactive use.
    _forecast(env, other=0.0)
    runner = FakeRunner((0, _stream(api_key_source="ANTHROPIC_API_KEY", cost=0.02), ""))
    _code, payload, err = _run(env, _request(), runner, snapshots=_wasting_subscriptions())
    assert payload["served"]["account"] == "anthropic_api_d", (payload["decision"], err)
    assert "ties claude_" in payload["decision"]["reason"]


def test_expected_cost_sizes_the_job(env) -> None:
    runner = FakeRunner((0, _stream(api_key_source="ANTHROPIC_API_KEY", cost=0.02), ""))
    _code, payload, _err = _run(env, _request(expected_cost_usd=3.05), runner)
    assert "of 0.0100 PSE" in payload["decision"]["reason"]


class _Scores:
    """Stands in for JointContext: waste avoided and the deadline tie-break, per id."""

    def __init__(self, rows):
        self.rows = rows  # id -> (avoided_pse, hours_to_deadline)

    def score(self, account_id):
        from types import SimpleNamespace
        row = self.rows.get(account_id)
        return None if row is None else SimpleNamespace(avoided=row[0])

    def ranked_score(self, account_id):
        row = self.rows.get(account_id)
        return None if row is None else row[0] + 0.5e-6 / (1.0 + row[1])


def test_a_subscription_that_avoids_more_waste_beats_a_pool() -> None:
    from quota_router.run import choose_credit_or_subscription
    scores = _Scores({"claude_b": (0.003, 50.0), "anthropic_api_d": (0.001, 5.0)})
    assert choose_credit_or_subscription(scores, "claude_b", ["anthropic_api_d"]) == (
        "claude_b", None)


def test_among_tied_pools_the_nearer_deadline_wins() -> None:
    from quota_router.run import choose_credit_or_subscription
    scores = _Scores({"claude_b": (0.003, 1.0), "anthropic_api_c": (0.003, 30.0),
                      "anthropic_api_d": (0.003, 10.0)})
    assert choose_credit_or_subscription(
        scores, "claude_b", ["anthropic_api_c", "anthropic_api_d"]) == (
        "anthropic_api_d", "claude_b")


def test_a_tie_at_zero_waste_keeps_the_nearer_deadline() -> None:
    # Nothing is wasted either way, so credit-first has no reason to apply; a pool with a
    # card on file would spend real money for no gain.
    from quota_router.run import choose_credit_or_subscription
    scores = _Scores({"claude_b": (0.0, 1.0), "anthropic_api_d": (0.0, 10.0)})
    assert choose_credit_or_subscription(scores, "claude_b", ["anthropic_api_d"]) == (
        "claude_b", None)
    # A pool that wins a zero tie on its nearer deadline did not win by credit-first, so
    # the reason must not say it did.
    scores = _Scores({"claude_b": (0.0, 10.0), "anthropic_api_d": (0.0, 1.0)})
    assert choose_credit_or_subscription(scores, "claude_b", ["anthropic_api_d"]) == (
        "anthropic_api_d", None)


def test_scores_a_hair_apart_are_a_tie() -> None:
    from quota_router.run import choose_credit_or_subscription
    scores = _Scores({"claude_b": (0.003, 1.0), "anthropic_api_d": (0.003 - 1e-9, 10.0)})
    assert choose_credit_or_subscription(scores, "claude_b", ["anthropic_api_d"]) == (
        "anthropic_api_d", "claude_b")


def test_with_no_subscription_valued_the_best_pool_wins() -> None:
    from quota_router.run import choose_credit_or_subscription
    scores = _Scores({"anthropic_api_d": (0.0, 10.0)})
    assert choose_credit_or_subscription(scores, None, ["anthropic_api_d"]) == (
        "anthropic_api_d", None)
    assert choose_credit_or_subscription(scores, "claude_x", ["anthropic_api_d"]) == (
        "anthropic_api_d", None)


# ======================================================================================
# The key
# ======================================================================================


def _everything_written(env) -> str:
    state = Path(env["XDG_STATE_HOME"])
    return "\n".join(p.read_text(errors="ignore") for p in state.rglob("*") if p.is_file())


def test_the_key_is_never_written_or_returned_even_when_the_child_echoes_it(env) -> None:
    echo = _stream(api_key_source="ANTHROPIC_API_KEY", text=f"the key is {SENTINEL}")
    runner = FakeRunner((0, echo, f"debug: {SENTINEL}"))
    code, payload, err = _run(env, _request(), runner)
    assert code == cli.EXIT_OK
    assert SENTINEL not in json.dumps(payload)
    assert SENTINEL not in err
    assert SENTINEL not in _everything_written(env)
    assert "<redacted>" in payload["text"]


def test_an_exception_carrying_the_key_is_scrubbed(env) -> None:
    runner = FakeRunner(OSError(f"spawn failed with {SENTINEL}"), (0, _stream(), ""))
    code, payload, err = _run(env, _request(), runner)
    assert code == cli.EXIT_OK
    assert SENTINEL not in json.dumps(payload)
    assert SENTINEL not in err
    assert SENTINEL not in _everything_written(env)


def test_a_secret_prints_as_redacted() -> None:
    secret = Secret(SENTINEL)
    assert SENTINEL not in repr(secret) and SENTINEL not in str(secret)
    assert SENTINEL not in f"{secret}"
    assert secret.reveal() == SENTINEL
    assert secret.scrub(f"a {SENTINEL} b") == "a <redacted> b"


def test_reading_a_missing_keychain_item_names_the_item_not_the_output() -> None:
    from quota_router.secrets import SecretError, read_api_key

    def runner(argv, **kwargs):
        assert argv[:3] == ["security", "find-generic-password", "-s"]
        return subprocess.CompletedProcess(argv, 44, "", f"leaky stderr {SENTINEL}")

    with pytest.raises(SecretError) as exc:
        read_api_key("anthropic-api-key-claude-z", runner=runner)
    assert "anthropic-api-key-claude-z" in str(exc.value)
    assert SENTINEL not in str(exc.value)
