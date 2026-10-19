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

from quota_router import calibrations, cli, joint, replay, scoring, waste
from quota_router.config import ConfigError, load_config
from quota_router.scoring import stocks_from_snapshot
from quota_router.types import AccountSnapshot
from tests.test_cli import FIVE_HOURS, NOW, _NO_CONFIG_FILES, account, run, window
from tests.test_waste import SEVEN_DAYS, T0, _run, _snapshot

MEASURED = 0.088


def _env(tmp_path) -> dict[str, str]:
    """An environment rooted in the test's temp dir.

    Every ``load_config`` call here passes it, with ``cwd=_NO_CONFIG_FILES``.
    ``load_config(env={})`` falls back to the real home directory and reads the
    operator's live config (see ``_NO_CONFIG_FILES`` in ``tests/test_cli.py``); three
    of this file's tests failed on the operator's machine the moment the live config
    set ``capacity = 0.088``, while CI, which has no live config, stayed green.
    """
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
    cfg = load_config(env=_env(tmp_path), cwd=_NO_CONFIG_FILES, explicit_path=path)
    assert cfg.accounts["antigravity_claude"].capacity == pytest.approx(MEASURED)
    assert cfg.capacities() == {"antigravity_claude": pytest.approx(MEASURED)}


def test_an_account_without_the_key_has_no_configured_capacity(tmp_path) -> None:
    path = _config(tmp_path, "[accounts.claude_c]\nweekly_to_session = 9.8\n")
    cfg = load_config(env=_env(tmp_path), cwd=_NO_CONFIG_FILES, explicit_path=path)
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
        load_config(env=_env(tmp_path), cwd=_NO_CONFIG_FILES, explicit_path=path)


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
    the 0.088 pool has much less stock and receives proportionally less demand;
    its PSE at risk shrinks and it ranks last.
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
    assert control["claude_b"] == pytest.approx(6.25 - 0.02 * 72.0)
    # One 0.04 PSE/h budget is shared over 6.25 + 0.55 PSE of stock.
    assert sized["claude_b"] == pytest.approx(6.25 - 0.04 * 6.25 / 6.8 * 72.0)
    assert sized["claude_c"] == pytest.approx(0.55 - 0.04 * 0.55 / 6.8 * 72.0)
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


# ======================================================================================
# Historical sizes require explicit beliefs (BACKLOG #24)
# ======================================================================================


def _row(
    account_id,
    *,
    wasted,
    scale,
    kind=waste.KIND_SCHEDULED,
    reset_at_s=T0 + SEVEN_DAYS,
    window="7d",
    scoped=False,
    observed=True,
):
    return waste.ResetRecord(
        account=account_id,
        window=window,
        reset_at_s=reset_at_s,
        scheduled_reset_at_s=reset_at_s,
        observed=observed,
        scoped=scoped,
        remaining_fraction=0.5,
        wasted_pse=wasted,
        tier="unknown",
        tier_scale=scale,
        k=6.25,
        kind=kind,
        provider="antigravity",
    )


def _capacity_belief():
    return {"version": 1, "id": "ag-capacity-belief", "kind": "belief",
            "recorded_at": "2026-08-10T00:00:00Z", "reconstructed": False,
            "evidence_at": None, "scope": "account", "account": "antigravity_claude",
            "key": "weekly_pool", "valid_from": None, "valid_to": None,
            "status": "resolved", "value": {"weekly_to_session": 2.0, "window_size": MEASURED},
            "candidates": [], "reason": "Operator judgment that this pool size held throughout",
            "measurement_ids": [], "disputed_measurement_ids": [], "decided_by": "operator"}


def test_summarize_applies_explicit_historical_belief_and_keeps_logged_figures() -> None:
    """Current beliefs apply to raw remainders; current config cannot rescale history."""
    rows = (
        _row("antigravity_claude", wasted=2.0, scale=1.0),
        _row("antigravity_claude", wasted=1.0, scale=1.0, kind=waste.KIND_EARLY,
             reset_at_s=T0 + 2 * SEVEN_DAYS),
        _row("antigravity_claude", wasted=0.044, scale=MEASURED, reset_at_s=T0 + 3 * SEVEN_DAYS),
        _row("antigravity_claude", wasted=0.7, scale=1.0, window="fable", scoped=True),
        _row("antigravity_claude", wasted=9.0, scale=1.0, observed=False,
             reset_at_s=T0 + 4 * SEVEN_DAYS),
        _row("claude", wasted=3.0, scale=1.0),
        _row("codex", wasted=1.5, scale=None),
    )
    original = [row.to_dict() for row in rows]
    plain = waste.summarize(rows)
    revised = waste.summarize(rows, ledger=[_capacity_belief()])
    assert "rescaled" not in revised and "totals_at_capacity" not in revised
    assert plain["totals"]["wasted_pse"] == pytest.approx(2.0 + 0.044 + 3.0 + 1.5)
    assert revised["logged_totals"]["totals"] == plain["totals"]
    assert revised["totals"]["wasted_pse"] == pytest.approx(2 * 0.5 * 2 * MEASURED + 3.0 + 1.5)
    assert revised["early_totals"]["forfeited_pse"] == pytest.approx(0.5 * 2 * MEASURED)
    # Old Fable rows lack the weekly raw reading and cannot be safely recomputed.
    assert revised["scoped_totals"]["wasted_pse"] == pytest.approx(0.7)
    assert revised["unrecomputable_rows"] >= 1
    assert [row.to_dict() for row in rows] == original


def test_a_disabled_account_still_uses_its_explicit_belief_in_the_waste_report(tmp_path) -> None:
    """Routing eligibility cannot remove an account's evidence or recorded resets."""
    env = _env(tmp_path)
    waste.append_records([_row("antigravity_claude", wasted=2.0, scale=1.0)], env=env)
    original = waste.waste_path(env).read_bytes()
    calibrations.append_records([_capacity_belief()], env=env)
    path = _config(
        tmp_path,
        '[accounts.antigravity_claude]\nprovider = "antigravity"\nenabled = false\n'
        "capacity = 0.088\n",
    )
    code, out, _ = _run(["waste", "--config", str(path), "--json"], env, [], T0)
    assert code == cli.EXIT_OK
    payload = json.loads(out)
    assert payload["totals"]["wasted_pse"] == pytest.approx(0.5 * 2 * MEASURED)
    assert payload["logged_totals"]["totals"]["wasted_pse"] == 2.0
    assert payload["rows"][0]["current"]["ledger_ids"] == ["ag-capacity-belief"]
    assert waste.waste_path(env).read_bytes() == original


# ======================================================================================
# Replay sizes its own snapshot the way history recorded the pool
# ======================================================================================


def test_replay_scores_a_pool_at_the_capacity_history_recorded() -> None:
    """``replay`` rebuilds a snapshot per tick for the scorer inside ``select``. Its stock
    and metric take the pool's size from the recorded ``capacity``; the snapshot must
    carry the same number, or the policies it compares disagree on the pool by the very
    factor this item fixes.
    """
    from tests.test_replay import COUNT_FROM, DAY, STEP, _series

    records = _series(
        "claude_x",
        start=T0,
        end=COUNT_FROM + DAY,
        used_at=lambda t: 0.5,
        resets_at=lambda t: COUNT_FROM + 3 * DAY,
        tier="unknown",
        capacity=MEASURED,
    )
    specs, _trace = replay.reconstruct(
        records,
        group="claude",
        plans={},
        plan_ends={},
        start_s=COUNT_FROM,
        end_s=COUNT_FROM + DAY,
        bin_s=STEP,
    )
    (spec,) = specs
    pool = replay._start(spec, COUNT_FROM)

    snap = replay._snapshot(pool, COUNT_FROM)
    stock = replay._stock(pool, COUNT_FROM)

    assert spec.tier_scale == pytest.approx(MEASURED)
    assert snap.tier == "unknown"
    assert snap.capacity == pytest.approx(stock.session_cap) == pytest.approx(MEASURED)
    assert scoring.stocks_from_snapshot(snap, COUNT_FROM, snap.capacity, None).weekly_remaining == (
        pytest.approx(stock.weekly_rem)
    )


def test_current_config_changes_alone_cannot_rescale_logged_waste(tmp_path) -> None:
    env = _env(tmp_path)
    write = waste.append_records(
        [_row("antigravity_claude", wasted=2.0, scale=1.0),
         _row("antigravity_claude", wasted=0.99, scale=1.0, reset_at_s=T0 + 2 * SEVEN_DAYS)],
        env=env,
    )
    assert write.appended == 2, write
    original = waste.waste_path(env).read_bytes()
    path = _config(
        tmp_path, '[accounts.antigravity_claude]\nprovider = "antigravity"\ncapacity = 0.088\n'
    )
    code, out, _ = _run(["waste", "--config", str(path)], env, [], T0)
    assert code == cli.EXIT_OK
    assert "2.99 PSE expired unused" in out
    assert "every row uses its logged basis" in out
    code, out, _ = _run(["waste", "--config", str(path), "--json"], env, [], T0)
    assert code == cli.EXIT_OK
    configured = json.loads(out)
    assert configured["totals"]["wasted_pse"] == pytest.approx(2.99)
    assert configured["totals"] == configured["logged_totals"]["totals"]
    assert all(row["current"]["status"] == "logged" for row in configured["rows"])
    assert "rescaled" not in configured and "totals_at_capacity" not in configured
    code, out, _ = _run(["waste", "--json"], env, [], T0)
    assert code == cli.EXIT_OK
    assert json.loads(out)["rows"] == configured["rows"]
    assert waste.waste_path(env).read_bytes() == original


# ======================================================================================
# Round-2 review: the override must not depend on routing eligibility
# ======================================================================================


def test_the_cache_path_records_the_configured_capacity_for_an_excluded_account(tmp_path) -> None:
    """``--only``/``--exclude`` disable the other accounts for routing. The cache fallback
    still replays every account in the last record and rebuilds each from its tier, and
    whatever it rebuilds is recorded again. A pool's size does not change with its
    eligibility, so the override must reach every configured account, or a cache-path
    pick writes the placeholder size back into history and the next waste row reads it.
    """
    from quota_router.history import history_path

    env = _env(tmp_path)
    path = _config(
        tmp_path, '[accounts.antigravity_claude]\nprovider = "antigravity"\ncapacity = 0.088\n'
    )

    def fresh(account_id):
        return account(
            account_id,
            window("5h", 0.0, length_s=FIVE_HOURS, resets_in_s=3600.0),
            window("7d", 0.0, resets_in_s=3 * 24 * 3600.0),
            tier="unknown",
        )

    code, _, err = run(
        ["status", "--config", str(path), "--json"],
        env,
        snapshots=(fresh("claude_b"), fresh("antigravity_claude")),
        cwd=_NO_CONFIG_FILES,
    )
    assert code == cli.EXIT_OK, err

    def oracle_down(**_kw):
        raise RuntimeError("oracle down")

    code, out, err = run(
        ["pick", "--config", str(path), "--only", "claude_b", "--dry-run", "--json"],
        env,
        deps=cli.Deps(load_snapshots=oracle_down),
        cwd=_NO_CONFIG_FILES,
    )
    assert code == cli.EXIT_OK, err
    payload = json.loads(out)
    assert payload["decision"]["account"] == "claude_b"
    assert any("replaying the history snapshot" in w for w in payload["warnings"]), payload["warnings"]

    last = json.loads(history_path(env).read_text(encoding="utf-8").splitlines()[-1])
    recorded = {entry["id"]: entry for entry in last["accounts"]}
    assert recorded["antigravity_claude"]["source"] == "cache"
    assert recorded["antigravity_claude"]["capacity"] == pytest.approx(MEASURED), (
        "the cache-path pick re-recorded the excluded pool at the tier's size"
    )


def test_an_unknown_tier_with_a_configured_capacity_is_not_called_neutral(tmp_path) -> None:
    """Two strings said the capacity was the neutral 1.0 whatever the config set."""
    env = _env(tmp_path)
    snap = account(
        "antigravity_claude",
        window("5h", 0.0, length_s=FIVE_HOURS, resets_in_s=3600.0),
        window("7d", 0.0, resets_in_s=3 * 24 * 3600.0),
        tier="unknown",
    )

    code, out, _ = run(["pick", "--dry-run", "--json"], env, snapshots=(snap,), cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_OK
    assert any("capacity assumed neutral" in w for w in json.loads(out)["warnings"])

    path = _config(
        tmp_path,
        '[accounts.antigravity_claude]\nprovider = "antigravity"\ntier = "agy_free"\ncapacity = 0.088\n',
    )
    code, out, err = run(
        ["pick", "--config", str(path), "--dry-run", "--json"], env, snapshots=(snap,), cwd=_NO_CONFIG_FILES
    )
    assert code == cli.EXIT_OK, err
    warnings = json.loads(out)["warnings"]
    assert not any("assumed neutral" in w for w in warnings), warnings
    assert not any("(1.0)" in w for w in warnings), warnings
    assert any("agy_free" in w and "capacity" in w for w in warnings), warnings


# ======================================================================================
# Round-3 review: no enabled-only default to fall back into, and no empty scale list
# ======================================================================================


def test_capacities_always_include_disabled_accounts(tmp_path) -> None:
    """A pool's size does not change with its eligibility, so there is no enabled-only
    form for a future caller to reach for and bring back the cache-path bug."""
    path = _config(
        tmp_path,
        '[accounts.antigravity_claude]\nprovider = "antigravity"\nenabled = false\ncapacity = 0.088\n',
    )
    cfg = load_config(env=_env(tmp_path), cwd=_NO_CONFIG_FILES, explicit_path=path)
    assert cfg.capacities() == {"antigravity_claude": pytest.approx(MEASURED)}


def test_waste_preserves_unknown_historical_scale_despite_current_config(tmp_path) -> None:
    env = _env(tmp_path)
    waste.append_records([_row("codex", wasted=1.5, scale=None)], env=env)
    path = _config(tmp_path, "[accounts.codex]\ncapacity = 0.5\n")
    code, out, _ = _run(["waste", "--config", str(path), "--json"], env, [], T0)
    assert code == cli.EXIT_OK
    row, = json.loads(out)["rows"]
    assert row["logged"]["basis"]["window_size"] is None
    assert row["current"]["values"]["window_size"] is None
    assert row["current"]["wasted_pse"] == 1.5
    assert row["current"]["status"] == "logged"
