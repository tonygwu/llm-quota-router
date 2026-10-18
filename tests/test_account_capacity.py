"""A per-account ``capacity`` in the config replaces the tier's ratio, everywhere at once.

Capacity is the size of one five-hour window in PSE. Until this key existed it came only
from the plan tier, so an Antigravity pool (tier ``unknown`` -> 1.0) was scored, projected
and recorded in the waste series as if it were a Max-20x account, about eleven times its
measured size (BACKLOG #9, ``docs/measurements/2026-10-12-antigravity-capacity.md``).

The configured value is applied to the snapshot once, in ``_prepare``, before anything
reads it and before history records it. Scoring, the joint projection, ``status`` and the
waste writer all read the snapshot's capacity, and the waste writer reads it back out of
history, so one application point is what makes them agree. The tier stays as reported.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from quota_router import cli, joint, waste
from quota_router.config import ConfigError, load_config
from quota_router.scoring import stocks_from_snapshot
from quota_router.types import AccountSnapshot
from tests.test_cli import FIVE_HOURS, NOW, _NO_CONFIG_FILES, account, run, window
from tests.test_waste import SEVEN_DAYS, T0, _run, _snapshot

MEASURED = 0.088


def _env(tmp_path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PATH": "/usr/bin:/bin",
    }


def _config(tmp_path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


# ======================================================================================
# The config key
# ======================================================================================


def test_a_configured_capacity_round_trips_from_toml(tmp_path) -> None:
    path = _config(
        tmp_path, '[accounts.antigravity_claude]\nprovider = "antigravity"\ncapacity = 0.088\n'
    )
    cfg = load_config(env={}, explicit_path=path)
    assert cfg.accounts["antigravity_claude"].capacity == pytest.approx(MEASURED)
    assert cfg.capacities() == {"antigravity_claude": pytest.approx(MEASURED)}


def test_an_account_without_the_key_has_no_configured_capacity(tmp_path) -> None:
    path = _config(tmp_path, "[accounts.claude_c]\nweekly_to_session = 9.8\n")
    cfg = load_config(env={}, explicit_path=path)
    assert cfg.accounts["claude_c"].capacity is None
    assert cfg.capacities() == {}


@pytest.mark.parametrize("raw", ["0", "-0.1", "inf", "nan", '"0.088"', "true"])
def test_a_capacity_that_is_not_a_positive_finite_number_is_refused(tmp_path, raw) -> None:
    """A capacity of zero sizes the pool at nothing; a negative one is not a size at all.

    Refused at load time, as loudly as a k below 1, because the value is the denominator
    of every PSE figure for the account and a bad one would mis-size the whole pool.
    """
    path = _config(tmp_path, f"[accounts.claude_c]\ncapacity = {raw}\n")
    with pytest.raises(ConfigError, match="capacity"):
        load_config(env={}, explicit_path=path)


# ======================================================================================
# The snapshot
# ======================================================================================


def test_an_override_replaces_the_tier_ratio_and_keeps_the_tier() -> None:
    snap = account("claude_c", window("7d", 0.0, resets_in_s=3600.0), tier="max_5x")
    assert snap.capacity == pytest.approx(0.25)

    sized = replace(snap, capacity_override=MEASURED)

    assert sized.capacity == pytest.approx(MEASURED)
    assert sized.tier == "max_5x", "the tier is reported, not changed"
    payload = sized.to_dict()
    assert payload["tier"] == "max_5x"
    assert payload["capacity"] == pytest.approx(MEASURED)


@pytest.mark.parametrize("bad", [0.0, -1.0, float("inf"), float("nan")])
def test_an_override_must_be_a_positive_finite_number(bad) -> None:
    with pytest.raises(ValueError, match="capacity_override"):
        AccountSnapshot(id="claude", capacity_override=bad)


def test_the_override_sizes_the_weekly_pool_in_the_scorer_and_the_projection() -> None:
    """``weekly capacity = k x tier_scale``: the one number both layers must share."""
    snap = replace(
        account(
            "claude_c",
            window("5h", 0.0, length_s=FIVE_HOURS, resets_in_s=3600.0),
            window("7d", 0.0, resets_in_s=3 * 86400.0),
        ),
        capacity_override=MEASURED,
    )

    stocks = stocks_from_snapshot(snap, NOW, snap.capacity, None)
    assert stocks is not None
    assert stocks.weekly_remaining == pytest.approx(6.25 * MEASURED)

    stock = joint.subscription_stock(snap, now_s=NOW, capacity=snap.capacity, plan=None, refills=True)
    assert stock is not None
    assert stock.weekly_cap == pytest.approx(6.25 * MEASURED)
    assert stock.session_cap == pytest.approx(MEASURED)


# ======================================================================================
# End to end: pick, status and the waste row
# ======================================================================================


def test_a_configured_capacity_reaches_a_real_pick(tmp_path) -> None:
    """Two identical fresh accounts; one configured to the measured size.

    Built like ``test_a_configured_k_reaches_a_real_pick``: the fixture goes through
    ``pick`` itself, so the key has to survive the loader, the snapshot and the scorer.
    The control run, with no config, scores the two accounts identically. With the key,
    the 0.088 pool is too small to show any PSE at risk, so it scores zero and ranks last.
    """
    path = _config(tmp_path, "[accounts.claude_c]\ncapacity = 0.088\n")

    def fresh(account_id):
        return account(
            account_id,
            window("5h", 0.0, length_s=FIVE_HOURS, resets_in_s=3600.0),
            window("7d", 0.0, resets_in_s=3 * 24 * 3600.0),
        )

    def scores(argv):
        code, out, err = run(
            argv,
            _env(tmp_path),
            snapshots=(fresh("claude_b"), fresh("claude_c")),
            cwd=_NO_CONFIG_FILES,
        )
        assert code == cli.EXIT_OK, err
        payload = json.loads(out)
        return {row["account"]: row["score"] for row in payload["ranked"]}, payload

    control, _ = scores(["pick", "--dry-run", "--json"])
    assert control["claude_c"] == pytest.approx(control["claude_b"]), control
    assert control["claude_b"] > 0.0, "the control must show PSE at risk at capacity 1.0"

    sized, payload = scores(["pick", "--config", str(path), "--dry-run", "--json"])
    assert sized["claude_b"] == pytest.approx(control["claude_b"]), "the other account is untouched"
    # Nothing is at risk in a 0.088 pool over this horizon, so the score falls to the
    # earliest-deadline tie-break term, which is of the order of 1e-8.
    assert sized["claude_c"] < 1e-6, (
        f"the configured capacity never reached the scorer: {sized}"
    )
    assert payload["decision"]["account"] == "claude_b"


def test_a_configured_capacity_is_stamped_on_the_waste_row(tmp_path) -> None:
    """``status`` records the snapshot; the waste writer reads capacity back from history.

    Without the override the same two readings write ``tier_scale 1.0`` and
    ``wasted_pse 3.4375`` (``test_status_records_a_reset_it_can_see``). With it the row
    carries the configured size and the PSE figure shrinks with it; the tier is unchanged.
    """
    env = _env(tmp_path)
    path = _config(tmp_path, "[accounts.claude]\ncapacity = 0.088\n")
    reset_at = T0 + SEVEN_DAYS
    before, after = reset_at - 600.0, reset_at + 600.0

    code, out, err = _run(
        ["status", "--config", str(path), "--json"], env, [_snapshot(0.45, reset_at, before)], before
    )
    assert code == cli.EXIT_OK, err
    shown = json.loads(out)["accounts"][0]
    assert shown["tier"] == "max_20x"
    assert shown["capacity"] == pytest.approx(MEASURED), "status must show the configured size"

    _run(["status", "--config", str(path)], env, [_snapshot(0.01, reset_at + SEVEN_DAYS, after)], after)

    rows, _ = waste.read_records(env=env)
    assert [r.reset_at_s for r in rows] == [reset_at]
    assert rows[0].tier == "max_20x"
    assert rows[0].tier_scale == pytest.approx(MEASURED)
    assert rows[0].wasted_pse == pytest.approx(0.55 * 6.25 * MEASURED)
