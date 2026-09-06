"""Measuring k -- the weekly:session capacity ratio -- from the history log.

WHY THIS EXISTS
---------------
``k`` converts a percentage of a five-hour budget into a percentage of a weekly
one. Every PSE-normalized routing decision depends on it, and until now it was a
hand-computed constant (11.9, from four intervals in a scratch script, +/-2).

The estimator itself is arithmetic: accumulate the five-hour bar's *increments*
between resets, divide by the weekly bar's consumption over the same span. What
makes it non-trivial is that both failure modes are silent and both bias the
answer LOW:

* **Quantization.** Both bars are integer percentages. Over a few hours the weekly
  denominator is a few percent, so a 1% quantum is a large relative error. Over a
  week it is ~75% and the same arithmetic is precise. Time fixes this by itself.
* **Undersampling.** If the five-hour bar rises and then resets between two
  samples, that rise is invisible forever. Run against a sparse log this produced
  k values of 4.0, 0.4 and 3.3 for the three accounts -- all far below the true
  value, all plausible-looking. An estimator that cannot detect this is worse than
  no estimator, because it answers confidently and wrongly.

So the contract is: report an interval, never a bare point estimate; refuse
outright when the evidence cannot support one; and say which of the two problems
is responsible.
"""

from __future__ import annotations

import pytest

from quota_router.history import estimate_weekly_to_session

HOUR = 3600.0
FIVE_HOUR = 5 * HOUR


def rec(t: float, **accounts: tuple[float, float]) -> dict:
    """One history record: ``rec(t, claude=(session_used, weekly_used))``."""
    return {
        "t": t,
        "accounts": [
            {
                "id": account_id,
                "windows": [
                    {"key": "5h", "used_fraction": session},
                    {"key": "7d", "used_fraction": weekly},
                ],
            }
            for account_id, (session, weekly) in accounts.items()
        ],
    }


def series(
    k: float,
    *,
    samples: int,
    step_s: float,
    burn_per_step: float | None = None,
    weekly_target: float = 0.80,
) -> list[dict]:
    """A synthetic log for one account burning at a constant rate with known ``k``.

    The five-hour bar climbs by ``burn_per_step`` each step and resets every five
    hours; the weekly bar climbs by ``burn_per_step / k`` and never resets.

    ``burn_per_step`` is DERIVED by default so neither bar saturates. Clamping at
    100% silently truncates the denominator while the numerator keeps accumulating,
    which inflates the recovered k -- a fixture bug that looks exactly like an
    estimator bug.
    """
    if burn_per_step is None:
        burn_per_step = weekly_target * k / samples
    per_window = (FIVE_HOUR / step_s) * burn_per_step
    assert per_window <= 1.0, f"fixture would saturate the session bar ({per_window:.2f})"
    assert samples * burn_per_step / k <= 1.0, "fixture would saturate the weekly bar"

    out = []
    session = 0.0
    weekly = 0.0
    window_started = 0.0
    for i in range(samples):
        t = i * step_s
        if t - window_started >= FIVE_HOUR:
            window_started = t
            session = 0.0
        out.append(rec(t, claude=(session, weekly)))
        session += burn_per_step
        weekly += burn_per_step / k
    return out


# ======================================================================================
# It recovers a known ratio
# ======================================================================================


def test_it_recovers_a_known_k_from_a_densely_sampled_series() -> None:
    log = series(12.0, samples=200, step_s=15 * 60)
    est = estimate_weekly_to_session(log)["claude"]

    assert est.k is not None, est.reason
    assert est.k == pytest.approx(12.0, rel=0.10), (
        f"expected ~12, got {est.k} over {est.samples} samples"
    )
    assert est.low <= est.k <= est.high


def test_the_interval_brackets_the_truth_and_narrows_with_evidence() -> None:
    short = estimate_weekly_to_session(
        series(12.0, samples=60, step_s=15 * 60, weekly_target=0.25)
    )["claude"]
    long = estimate_weekly_to_session(
        series(12.0, samples=400, step_s=15 * 60)
    )["claude"]

    assert short.high - short.low > long.high - long.low, (
        "more evidence must produce a tighter interval; "
        f"short={short.low:.1f}-{short.high:.1f} long={long.low:.1f}-{long.high:.1f}"
    )
    assert long.low <= 12.0 <= long.high


# ======================================================================================
# The two silent failure modes
# ======================================================================================


def test_a_five_hour_reset_is_not_read_as_negative_consumption() -> None:
    """A rollover is a drop in the bar. Counting it would make k negative or absurd."""
    log = series(12.0, samples=300, step_s=15 * 60)
    est = estimate_weekly_to_session(log)["claude"]
    assert est.k is not None and est.k > 0
    assert est.session_resets_seen >= 3, (
        f"the fixture should span several windows, saw {est.session_resets_seen}"
    )


def test_undersampling_is_detected_and_refused_rather_than_answered_low() -> None:
    """The failure that produced k=0.4 on real data.

    Sampling every three hours against a five-hour window means the bar can rise
    and reset unseen. The lost rise is invisible, so the numerator is too small and
    k comes out far below truth -- confidently, and with no outward sign.
    """
    sparse = series(12.0, samples=30, step_s=3 * HOUR, burn_per_step=0.30)
    est = estimate_weekly_to_session(sparse)["claude"]

    assert est.undersampled is True
    assert est.k is None, (
        f"an undersampled series must refuse a point estimate, got k={est.k}"
    )
    assert "gap" in (est.reason or "").lower(), est.reason


def test_a_thin_denominator_is_refused_even_when_densely_sampled() -> None:
    """Quantization, the other failure. A 1% quantum against 2% consumed is noise."""
    thin = series(12.0, samples=8, step_s=5 * 60, burn_per_step=0.01)
    est = estimate_weekly_to_session(thin)["claude"]
    assert est.k is None
    assert "weekly" in (est.reason or "").lower(), est.reason


# ======================================================================================
# Per-account, because tiers and promotions differ
# ======================================================================================


def test_accounts_are_estimated_independently() -> None:
    log = []
    a = series(12.0, samples=200, step_s=15 * 60)
    b = series(6.0, samples=200, step_s=15 * 60)
    for ra, rb in zip(a, b):
        merged = dict(ra)
        merged["accounts"] = [
            ra["accounts"][0],
            {**rb["accounts"][0], "id": "claude_b"},
        ]
        log.append(merged)

    est = estimate_weekly_to_session(log)
    assert est["claude"].k == pytest.approx(12.0, rel=0.10)
    assert est["claude_b"].k == pytest.approx(6.0, rel=0.10)


def test_an_account_with_no_usable_data_reports_why_rather_than_guessing() -> None:
    est = estimate_weekly_to_session([rec(0.0, claude=(0.1, 0.1))])["claude"]
    assert est.k is None
    assert est.reason


def test_sparse_history_is_skipped_rather_than_poisoning_a_dense_tail() -> None:
    """Old ad-hoc samples must not block an estimate once real sampling starts.

    The real log began as irregular manual runs with 5-hour gaps and only later
    gained a 15-minute poller. Judging the whole span by its worst gap would refuse
    every estimate until the sparse head aged out a week later -- so the poller's
    first useful day would be wasted.

    A pair spanning a huge gap is not evidence: we cannot know what happened inside
    it. Excluding such pairs from BOTH the numerator and the denominator keeps the
    ratio unbiased and lets the dense tail speak for itself.
    """
    sparse_head = [
        rec(i * 5 * HOUR, claude=(0.1 * (i % 3), 0.01 * i)) for i in range(4)
    ]
    dense_tail = series(12.0, samples=200, step_s=15 * 60)
    offset = sparse_head[-1]["t"] + 5 * HOUR
    for r in dense_tail:
        r["t"] += offset
        for s in r["accounts"]:
            for w in s["windows"]:
                if w["key"] == "7d":
                    w["used_fraction"] = min(1.0, w["used_fraction"] + 0.04)

    est = estimate_weekly_to_session(sparse_head + dense_tail)["claude"]
    assert est.k is not None, f"the dense tail should be usable; got {est.reason}"
    assert est.k == pytest.approx(12.0, rel=0.12), est.k
    assert est.dropped_pairs >= 3, (
        f"the sparse head's pairs should have been excluded, dropped={est.dropped_pairs}"
    )


def test_source_switching_does_not_manufacture_consumption() -> None:
    """The bug that produced 124pp of weekly burn in seven hours.

    The log records whichever snapshot won each invocation, and sources disagree:
    the statusline cache lags the live endpoint. Alternating between them makes the
    bar oscillate (0.37 -> 0.40 -> 0.37 -> 0.40), and an estimator that sums only
    the *increases* counts that same 3pp on every flip. Observed 15 times in a
    seven-hour log, inflating both terms with pure noise.

    Only same-source pairs are comparable.
    """
    log = []
    for i in range(20):
        # live and cache disagree by 3pp; the winner alternates every sample.
        live = i % 2 == 0
        log.append(
            {
                "t": i * 900.0,
                "accounts": [
                    {
                        "id": "claude",
                        "source": "live" if live else "cache",
                        "windows": [
                            {"key": "5h", "used_fraction": 0.10},
                            {"key": "7d", "used_fraction": 0.40 if live else 0.37},
                        ],
                    }
                ],
            }
        )
    est = estimate_weekly_to_session(log)["claude"]
    assert est.weekly_consumed_pp == pytest.approx(0.0, abs=0.01), (
        "an oscillation between sources is not consumption; "
        f"counted {est.weekly_consumed_pp}pp"
    )


def test_a_physically_impossible_k_is_refused_however_tight_its_interval() -> None:
    """k < 1 means a weekly budget smaller than a five-hour one. It cannot happen.

    The weekly window contains the session window, so k >= 1 by construction. An
    estimate below that is not a measurement, it is proof the input was
    contaminated -- and a narrow interval around a wrong number is more dangerous
    than a wide one, because the adoption gate was checking only width.
    """
    log = []
    for i in range(40):
        log.append(
            {
                "t": i * 900.0,
                "accounts": [
                    {
                        "id": "claude",
                        "source": "live",
                        "windows": [
                            # session barely moves while weekly climbs: impossible
                            {"key": "5h", "used_fraction": 0.10 + 0.0002 * i},
                            {"key": "7d", "used_fraction": 0.10 + 0.01 * i},
                        ],
                    }
                ],
            }
        )
    est = estimate_weekly_to_session(log)["claude"]
    assert est.k is None, f"k={est.k} is below the physical floor and must be refused"
    assert "impossible" in (est.reason or "").lower(), est.reason


def test_a_fourth_claude_account_is_canonical_not_a_fallback() -> None:
    """Adding a subscription must not depend on a prefix heuristic to be routable.

    ``provider_for_account_id`` already guessed claude_d correctly, but a guess is
    not a registration: without a builtin config entry and a default config-dir
    name the account is never discovered, so it is invisible to routing however
    well its provider resolves.
    """
    from quota_router.config import load_config
    from quota_router.providers.claude_cli_config import DEFAULT_CLAUDE_CONFIG_DIR_NAMES
    from quota_router.types import ACCOUNT_IDS, provider_for_account_id

    assert "claude_d" in ACCOUNT_IDS
    assert provider_for_account_id("claude_d") == "claude"
    assert DEFAULT_CLAUDE_CONFIG_DIR_NAMES["claude_d"] == ".claude-d"

    cfg = load_config(env={})
    account = cfg.accounts.get("claude_d")
    assert account is not None, "claude_d must be a builtin account"
    assert account.config_dir and account.config_dir.endswith(".claude-d")
    # A slot account, unlike the default, IS selected by naming its directory.
    assert account.exec_env().get("CLAUDE_CONFIG_DIR", "").endswith(".claude-d")


def test_the_adoption_bar_is_reachable_within_a_single_window() -> None:
    """A gate that demands more weekly burn than a window can produce never fires.

    One full five-hour window is 100pp of session, which is only 100/k pp of
    weekly -- about 17pp at k=6, and just 8pp at k=12. An absolute threshold of
    20pp weekly is therefore unreachable at ANY plausible k without spanning a
    reset, which is the contamination a clean measurement is trying to avoid.

    Precision is what actually matters, and it is bounded by the 1pp quantum on
    the denominator: ~14% relative at 7pp, ~6% at a full window. So the bar is
    relative interval width, which a single clean window can clear.
    """
    from quota_router.history import adoption_ready

    # 8pp of weekly: real, but ~29% wide.
    assert adoption_ready(k=6.2, low=5.40, high=7.23, max_gap_s=400) is False
    # One full window's worth: ~15%, which is all the precision the decision needs.
    assert adoption_ready(k=6.2, low=5.78, high=6.68, max_gap_s=400) is True
    # Precise but sampled too sparsely to trust the increments.
    assert adoption_ready(k=6.2, low=5.78, high=6.68, max_gap_s=3600) is False
    assert adoption_ready(k=None, low=None, high=None, max_gap_s=60) is False


def test_both_sources_contribute_instead_of_the_weaker_being_discarded() -> None:
    """Same-source SEGMENTS, not a single dominant source.

    Comparing readings across sources manufactures consumption, because the
    statusline cache lags the live endpoint by a constant offset -- so the first fix
    was to compare only same-source pairs, implemented as "pick the source with the
    most readings and drop the rest".

    That starved claude_c. Its access token expires whenever the account goes
    unused, so its readings alternate between live and cache, and discarding the
    minority source left 60-90 minute holes in what looked like a dense series. The
    density guard then refused every estimate, and the account swung between k=4.0
    and k=9.4 depending on the window.

    The offset only invalidates CROSS-source comparisons. Each source's own series
    is internally consistent, so both can contribute -- comparing live to the
    previous live reading, and cache to the previous cache reading, never one to the
    other.
    """
    log = []
    session = weekly = 0.0
    k = 8.0
    burn = 0.02
    for i in range(120):
        # Sources alternate every sample. Under the old rule half the data was
        # thrown away and the survivors were 30 minutes apart.
        live = i % 2 == 0
        offset = 0.0 if live else -0.03  # the cache lags by a constant
        log.append(
            {
                "t": i * 900.0,
                "accounts": [
                    {
                        "id": "claude_c",
                        "source": "live" if live else "cache",
                        "windows": [
                            {"key": "5h", "used_fraction": max(0.0, session + offset)},
                            {"key": "7d", "used_fraction": max(0.0, weekly + offset / k)},
                        ],
                    }
                ],
            }
        )
        session += burn
        weekly += burn / k

    est = estimate_weekly_to_session(log)["claude_c"]
    assert est.k is not None, est.reason
    assert est.k == pytest.approx(k, rel=0.15), est.k

    # The accuracy was never the problem -- one source alone still recovers k. What
    # discarding half the readings costs is DENSITY: the survivors are twice as far
    # apart, and the adoption guard refuses anything sampled more sparsely than
    # 15 minutes. Samples arrive every 900s; using both sources must see that,
    # not the 1800s spacing of one.
    assert est.max_gap_s == pytest.approx(900.0, abs=1.0), (
        f"both sources should be used, giving 900s spacing; got {est.max_gap_s}s "
        f"which is the spacing of a single source"
    )
    assert est.samples > 100, (
        f"discarding a source halves the usable pairs; got {est.samples}"
    )


# ======================================================================================
# A republished stale reading is not a fresh observation
# ======================================================================================


def obs_rec(t: float, observed_at_s: float, session: float, weekly: float) -> dict:
    """A history record that distinguishes when it was WRITTEN from when it was READ."""
    return {
        "t": t,
        "accounts": [
            {
                "id": "claude",
                "source": "cache",
                "windows": [
                    {
                        "key": "5h",
                        "used_fraction": session,
                        "observed_at_s": observed_at_s,
                    },
                    {
                        "key": "7d",
                        "used_fraction": weekly,
                        "observed_at_s": observed_at_s,
                    },
                ],
            }
        ],
    }


def test_a_republished_stale_reading_does_not_launder_an_18_hour_blind_spot() -> None:
    """The gap guard must run on the observation clock, not the record clock.

    Every record carries two timestamps: ``t``, when the router wrote the row, and
    ``observed_at_s``, when the vendor actually reported those numbers. The cache
    source republishes the SAME reading on every poll, so a bar that was last truly
    read 15 hours ago is re-emitted every 30 minutes under a fresh ``t``.

    Measuring gaps on ``t`` therefore reports a dense series that does not exist.
    When the cache finally refreshes, the pair straddling the refresh looks 30
    minutes wide and is accepted, when in truth it spans 18 hours and three session
    resets -- so its weekly movement enters the denominator with no matching session
    movement, which is precisely the one-way undersampling bias the guard exists to
    prevent.

    Observed live on ``claude_c``: six identical rows pinned at one 15.5-hour-old
    reading, then a refresh jumping 18.4 hours and +25pp of weekly consumption. That
    single laundered pair pulled the account's k from ~9.8 down to 6.2, and made
    nested calibration windows disagree in a way that looked like tier variation.
    """
    burn, k_true, step = 0.03, 8.0, 600.0

    # Six polls, all republishing one reading taken 15.5 hours earlier.
    stale_obs = -15.5 * HOUR
    log = [obs_rec(i * 1800.0, stale_obs, 1.0, 0.12) for i in range(6)]

    # The cache refreshes: the observation clock jumps 18.4h, the session bar has
    # reset unseen, and 25pp of weekly burn appears with nothing to attribute it to.
    log.append(obs_rec(10800.0, 10500.0, 0.0, 0.37))

    # Then a clean, densely observed run at a known k.
    session, weekly = 0.0, 0.37
    for i in range(25):
        t = 12000.0 + i * step
        log.append(obs_rec(t, t, session, weekly))
        session += burn
        weekly += burn / k_true

    est = estimate_weekly_to_session(log)["claude"]

    assert est.k is not None, f"the clean run should be usable; got {est.reason}"
    assert est.k == pytest.approx(k_true, rel=0.1), (
        f"k={est.k:.2f}: the 25pp of weekly burn across the laundered refresh was "
        f"counted, inflating the denominator to {est.weekly_consumed_pp:.0f}pp"
    )
    assert est.weekly_consumed_pp == pytest.approx(9.0, abs=1.0), (
        f"only the clean run's ~9pp is attributable; got "
        f"{est.weekly_consumed_pp:.0f}pp"
    )
    assert est.max_gap_s >= 18 * HOUR, (
        f"an 18-hour blind spot must be reported as one, not as the 30-minute "
        f"republish cadence; got {est.max_gap_s / 3600:.1f}h"
    )


# ======================================================================================
# The measured k has to reach the scorer
# ======================================================================================


def test_a_per_account_k_overrides_the_default_in_the_scorer() -> None:
    """``calibrate`` said ADOPT for two releases with nowhere to adopt *to*.

    ``k`` converts a weekly percentage into absolute PSE, so an account whose real
    ratio is 9.8 has its weekly pool understated by 36% while the default 6.25 is
    applied to it. That is the whole point of measuring k per account -- and until
    now ``Plan`` was constructed only by its own default argument, never from
    config, so the measurement had no path into a routing decision.

    Guard the plumbing end to end: identical snapshots, differing only in the
    configured ratio, must produce different absolute weekly stocks.
    """
    from quota_router.config import AccountConfig, Config
    from quota_router.scoring import plan_for, stocks_from_snapshot
    from quota_router.types import AccountSnapshot, Window

    now = 1_000_000.0
    snap = AccountSnapshot(
        id="claude_c",
        provider="claude",
        tier="max_5x",
        windows=(
            Window(
                key="5h",
                used_fraction=0.0,
                resets_at_s=now + HOUR,
                length_s=FIVE_HOUR,
                observed_at_s=now,
            ),
            Window(
                key="7d",
                used_fraction=0.0,
                resets_at_s=now + 3 * 24 * HOUR,
                length_s=7 * 24 * HOUR,
                observed_at_s=now,
            ),
        ),
    )

    default_cfg = Config()
    tuned_cfg = Config(
        accounts={"claude_c": AccountConfig(id="claude_c", weekly_to_session=9.8)}
    )

    default_stocks = stocks_from_snapshot(
        snap, now, snap.capacity, plan_for(default_cfg, snap.id)
    )
    tuned_stocks = stocks_from_snapshot(
        snap, now, snap.capacity, plan_for(tuned_cfg, snap.id)
    )
    assert default_stocks is not None and tuned_stocks is not None

    # weekly capacity = k * tier_scale, and the account is untouched, so remaining
    # IS the capacity: 6.25 * 0.25 = 1.5625 against 9.8 * 0.25 = 2.45.
    assert default_stocks.weekly_remaining == pytest.approx(1.5625)
    assert tuned_stocks.weekly_remaining == pytest.approx(2.45), (
        f"the configured k never reached the scorer; got "
        f"{tuned_stocks.weekly_remaining:.4f}"
    )


def test_a_configured_k_round_trips_from_toml(tmp_path) -> None:
    """The value an operator writes has to survive the loader."""
    from quota_router.config import load_config

    path = tmp_path / "config.toml"
    path.write_text(
        "[accounts.claude_c]\nweekly_to_session = 9.8\n", encoding="utf-8"
    )
    cfg = load_config(env={}, explicit_path=path)
    assert cfg.accounts["claude_c"].weekly_to_session == pytest.approx(9.8)


def test_a_k_below_one_is_rejected_rather_than_quietly_scoring(tmp_path) -> None:
    """k < 1 says the weekly pool is smaller than the session window it contains.

    That is not a preference to honor; it is a typo or a contaminated calibration
    run, and accepting it would shrink an account's weekly pool below one session
    window and make the router refuse work the account can plainly do. The
    estimator already refuses to *report* such a value; the loader must equally
    refuse to accept one written by hand.
    """
    from quota_router.config import ConfigError, load_config

    path = tmp_path / "config.toml"
    path.write_text(
        "[accounts.claude_c]\nweekly_to_session = 0.5\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="weekly_to_session"):
        load_config(env={}, explicit_path=path)


# ======================================================================================
# Pruning must not trust a clock it was handed
# ======================================================================================


def test_an_absurd_clock_cannot_delete_history(tmp_path) -> None:
    """A single ad-hoc command with a synthetic --now destroyed 38 hours of telemetry.

    ``_prune`` computes ``cutoff = now_s - keep_days * 86400`` and deletes every
    rotated file whose day precedes it. A probe run with ``now_s`` five months in the
    future therefore put the cutoff five months in the future too, and every real file
    fell behind it. Observed live: the calibration history this project's whole k
    measurement rests on was deleted by one debugging invocation.

    The existing defence is one-sided and says so proudly -- it distrusts the file's
    mtime because "deleting telemetry on that basis is silent data loss" -- while
    trusting the caller's clock without question. A clock far ahead of the newest data
    on disk is not a reason to delete everything; it is a reason to disbelieve the
    clock.
    """
    from quota_router.history import _prune

    live = tmp_path / "history.jsonl"
    for day in ("2026-08-14", "2026-08-15", "2026-08-16"):
        (tmp_path / f"history-{day}.jsonl").write_text("{}\n", encoding="utf-8")

    warnings: list[str] = []
    absurd = 1_800_000_000.0  # 2027-01-15, the value that did it
    removed = _prune(live, absurd, 30, warnings)

    survivors = sorted(p.name for p in tmp_path.glob("history-*.jsonl"))
    assert survivors == [
        "history-2026-08-14.jsonl",
        "history-2026-08-15.jsonl",
        "history-2026-08-16.jsonl",
    ], f"pruned real telemetry on a bogus clock; removed={removed}"


def test_pruning_still_works_on_a_sane_clock(tmp_path) -> None:
    """The guard must not turn retention off -- old files still go."""
    from quota_router.history import _prune

    live = tmp_path / "history.jsonl"
    (tmp_path / "history-2026-06-01.jsonl").write_text("{}\n", encoding="utf-8")  # old
    (tmp_path / "history-2026-08-15.jsonl").write_text("{}\n", encoding="utf-8")  # recent

    warnings: list[str] = []
    now = 1_786_900_000.0  # 2026-08-16
    _prune(live, now, 30, warnings)

    survivors = sorted(p.name for p in tmp_path.glob("history-*.jsonl"))
    assert survivors == ["history-2026-08-15.jsonl"], survivors


# ======================================================================================
# The adoption gate must be reachable by the process that produces the data
# ======================================================================================


def test_a_series_sampled_at_the_pollers_own_cadence_can_be_adopted() -> None:
    """The second unreachable gate in this file's history.

    ``ADOPTION_MAX_GAP_S`` was 900s and the usage poller's ``StartInterval`` is 900s,
    so the gate demanded data denser than the process producing it. launchd jitter
    alone put every real gap over the line, and live every account reported "worst
    sample gap 30m > 15m" while no estimate was ever adopted. Like the 20pp weekly
    bar before it, that reads as "not enough evidence yet" rather than as a bug, which
    is exactly why it survived.

    It also gated on the wrong statistic. The one-way sampling loss it cites is
    already folded into the interval via ``typical_step``, and any pair spanning more
    than ``MAX_GAP_FRACTION_OF_WINDOW`` of a window is dropped from both numerator and
    denominator before it can bias anything. What a single huge gap actually costs is
    *data*, and less data shows up as a wider interval -- which the width gate already
    catches. So the density question is about the series' typical rhythm, not its
    worst hiccup.
    """
    from quota_router.history import adoption_ready

    # A clean series at exactly the poller's cadence, with one 40-minute hiccup of the
    # kind an overnight sleep or a launchd stall produces.
    assert adoption_ready(k=6.3, low=5.9, high=6.6, max_gap_s=40 * 60, median_step_s=900.0), (
        "a series sampled at the poller's own interval must be adoptable; a single "
        "stall is a hole in the data, not a reason to distrust the rhythm"
    )


def test_a_genuinely_sparse_series_is_still_refused() -> None:
    """Loosening the worst-gap bound must not switch density checking off."""
    from quota_router.history import adoption_ready

    assert not adoption_ready(
        k=6.3, low=5.9, high=6.6, max_gap_s=3 * HOUR, median_step_s=3 * HOUR
    ), "a series whose TYPICAL step is three hours loses whole windows unseen"


def test_the_width_gate_still_binds_independently_of_density() -> None:
    from quota_router.history import adoption_ready

    assert not adoption_ready(k=6.3, low=4.0, high=9.0, max_gap_s=60.0, median_step_s=60.0)


# ======================================================================================
# The verdict has to agree with itself
# ======================================================================================


def test_a_refusal_never_prints_a_number_that_satisfies_its_own_bound() -> None:
    """"interval 15% wide, want <=15%" -- and then it refuses.

    The width was rounded to whole percent for display but compared at full
    precision, so 15.5% printed as 15% and failed a bound it appeared to meet. A tool
    that contradicts itself in the same sentence teaches the reader to distrust the
    parts that are right, which is a worse outcome than the rounding error.

    Asserted as a property over the message rather than on one example, so any future
    rewording that reintroduces the mismatch is caught.
    """
    import re

    from quota_router.cli import _adoption_verdict

    pattern = re.compile(r"interval ([\d.]+)% wide, want <=([\d.]+)%")
    # widths that straddle the bound, including the ones that round the wrong way
    for width_frac in (0.1449, 0.1499, 0.15, 0.1501, 0.1549, 0.1551, 0.16, 0.20):
        k = 6.3
        half = width_frac * k / 2.0
        verdict = _adoption_verdict(
            k=k, low=k - half, high=k + half, median_step_s=60.0, max_gap_s=60.0
        )
        m = pattern.search(verdict)
        if m is None:
            continue  # adopted, or refused for density -- covered elsewhere
        shown, bound = float(m.group(1)), float(m.group(2))
        assert shown > bound, (
            f"refused while printing {shown}% against a bound of {bound}%: the "
            f"message says the estimate qualifies and the verdict says it does not "
            f"(true width {100 * width_frac:.2f}%)"
        )


def test_a_density_refusal_reports_the_statistic_it_actually_tested() -> None:
    """The condition moved to the median step; the message still named the worst gap.

    Introduced by the fix that made this gate reachable at all. Reporting the worst
    gap while testing the typical one sends the reader to look at the wrong number --
    and on a series with one overnight hole those two differ by an order of magnitude.
    """
    from quota_router.cli import _adoption_verdict

    verdict = _adoption_verdict(
        k=6.3, low=6.25, high=6.35, median_step_s=3 * HOUR, max_gap_s=9 * HOUR
    )
    assert "180m" in verdict, f"should name the 180m typical step, got: {verdict}"
    assert "540m" not in verdict, f"named the worst gap it did not test: {verdict}"


def test_a_requested_window_is_honoured_on_the_clock_the_estimate_uses() -> None:
    """``--days N`` selected records by WRITE time while the series keys on OBSERVATION
    time, so a stale republished reading dragged data from outside the window in.

    Live, ``--days 0.5`` reported a span of 28 hours. Benign arithmetically -- the gap
    guard drops the offending pairs -- but the output invites the reader to trust a
    boundary the tool is not enforcing, and "the last twelve hours say k=4.7" is a
    different claim from "some readings from yesterday say so".
    """
    from quota_router.history import estimate_weekly_to_session

    # Everything is WRITTEN inside the window; the first batch was OBSERVED long before.
    log = []
    for i in range(6):
        t = 100_000.0 + i * 1800.0
        log.append(obs_rec(t, t - 30 * HOUR, 1.0, 0.10))      # republished, 30h stale
    session, weekly = 0.0, 0.40
    for i in range(25):
        t = 120_000.0 + i * 600.0
        log.append(obs_rec(t, t, session, weekly))
        session += 0.03
        weekly += 0.03 / 8.0

    cutoff = 120_000.0 - 2 * HOUR          # "the last few hours", on the observation clock
    est = estimate_weekly_to_session(log, since_s=cutoff)["claude"]

    assert est.span_s <= 12 * HOUR, (
        f"span {est.span_s / 3600:.1f}h exceeds the requested window: readings observed "
        f"before the cutoff were pulled in because only the write time was filtered"
    )


# ======================================================================================
# The tool must not recommend what its authors believe is harmful
# ======================================================================================


def test_calibrate_does_not_emit_a_paste_ready_calls_per_window_stanza(tmp_path) -> None:
    """It printed config you were meant to paste, for a number that should not be adopted.

    The estimator counts only picks the ROUTER made in the numerator, while the
    denominator moves for all consumption of that account -- including the operator's
    own interactive sessions. The confound is structural, so more data cannot resolve
    it, and it biases the estimate low, which makes every pileup reservation larger.
    Live, a batch already produced 79 reservations subtracting 39.5% of a window and
    self-throttled the router into refusing to route; halving calls_per_window would
    have doubled each of those.

    The number stays visible because it is informative. What is withheld is the
    instruction to adopt it: a tool that recommends an action its own authors consider
    harmful is worse than one that stays quiet.
    """
    from quota_router.history import Calibration

    from quota_router import cli as cli_mod

    text = cli_mod._calls_per_window_report({"claude_c": Calibration(
        account="claude_c", calls_per_window=100.0, demand=18.0,
        consumed=0.18, samples=3, window_key="5h", reason=None,
    )})

    assert "[accounts." not in text, f"still emits a paste-ready TOML stanza:\n{text}"
    assert "100" in text, "the estimate itself should still be reported"
    lowered = text.lower()
    assert "interactive" in lowered or "confound" in lowered, (
        f"the reason not to adopt it must travel with the number:\n{text}"
    )


def test_the_pileup_consequence_is_quantified_not_just_asserted(tmp_path) -> None:
    """"Adopting this is risky" is advice; a number is evidence."""
    from quota_router.history import Calibration

    from quota_router import cli as cli_mod

    text = cli_mod._calls_per_window_report({"claude_c": Calibration(
        account="claude_c", calls_per_window=100.0, demand=18.0,
        consumed=0.18, samples=3, window_key="5h", reason=None,
    )})
    # 1/100 of a window per pick, against the 0.5% the built-in default reserves.
    assert "1.0%" in text or "1%" in text, f"per-pick reservation not quantified:\n{text}"


def test_the_installer_polls_densely_enough_for_its_own_adoption_gate() -> None:
    """The scheduler's cadence and the calibration gate are one coupled decision.

    ``adoption_ready`` refuses any series whose typical step exceeds
    ``ADOPTION_MAX_STEP_S``. The poller IS what produces that series, so an installer
    default slower than the gate means no estimate can ever be adopted -- and nothing
    in either file would say so. The installed job runs at 900s while the installer
    defaulted to 1800s, so re-running it without the env var would have silently
    halved the sampling density that both the k estimate and the waste series depend
    on, and the only symptom would have been calibrate quietly never adopting again.

    Asserted across the language boundary on purpose: a shell default and a Python
    constant cannot be shared, so the only thing that keeps them honest is a test that
    reads both.
    """
    import re
    from pathlib import Path

    from quota_router.history import ADOPTION_MAX_STEP_S

    script = Path(__file__).resolve().parent.parent / "ops" / "install-launchd.sh"
    text = script.read_text(encoding="utf-8")
    match = re.search(r'INTERVAL="\$\{POLL_INTERVAL_S:-(\d+)\}"', text)
    assert match, "could not find the poll-interval default in ops/install-launchd.sh"
    default_s = int(match.group(1))

    assert default_s <= ADOPTION_MAX_STEP_S, (
        f"the installer polls every {default_s}s but calibration refuses any series "
        f"whose typical step exceeds {ADOPTION_MAX_STEP_S:.0f}s -- installing with the "
        f"default would guarantee no k estimate is ever adoptable"
    )


def test_an_unmeasurable_account_says_whose_floor_rejected_it() -> None:
    """The same config value behaves differently depending on whether you typed it.

    A floor the CALLER set rejects an account whose remaining quota cannot be verified;
    the identical built-in default does not, because the default is our sanity guard
    rather than a request. That distinction is defensible and it is also invisible --
    two setups that look identical in a config file diverge, and the exclusion reason
    gave the reader no way to tell which rule applied to them.

    Fixed in the cheapest place that carries the information: the reason itself.
    """
    from quota_router.cli import _unverifiable_against_floor_reason

    reason = _unverifiable_against_floor_reason(0.02)
    assert "2.0%" in reason
    lowered = reason.lower()
    assert "you set" in lowered or "configured" in lowered, (
        f"the reason must say the floor was caller-set, since the identical built-in "
        f"default would NOT have rejected this account: {reason}"
    )


def _print_installer_plist(tmp_path, *, quotapick: str, **env_overrides: str) -> dict:
    """Run ``ops/install-launchd.sh --print`` under a throwaway HOME and parse the plist."""
    import os
    import plistlib
    import subprocess
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "ops" / "install-launchd.sh"
    env = {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "QUOTAPICK_BIN": quotapick,
        **env_overrides,
    }
    completed = subprocess.run(
        ["/bin/bash", str(script), "--print"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return plistlib.loads(completed.stdout.encode("utf-8"))


def test_the_installer_polls_at_least_as_often_as_the_usage_cache_lives() -> None:
    """The poller exists to keep the usage cache warm; a cadence slower than the
    cache's lifetime leaves it cold most of the time.

    The adapter reuses a fetched payload for ``DEFAULT_USAGE_TTL_S``. At the old 900s
    default the cache was fresh for two minutes in fifteen, so almost every `cl`
    launch read live -- which is how four sequential reads came to race a
    three-second deadline on 2026-09-06. Same cross-language assertion as the
    calibration gate above, for the same reason.
    """
    import re
    from pathlib import Path

    from quota_router.providers.claude_oauth import DEFAULT_USAGE_TTL_S

    script = Path(__file__).resolve().parent.parent / "ops" / "install-launchd.sh"
    match = re.search(r'INTERVAL="\$\{POLL_INTERVAL_S:-(\d+)\}"', script.read_text())
    assert match
    default_s = int(match.group(1))

    assert default_s <= DEFAULT_USAGE_TTL_S, (
        f"the installer polls every {default_s}s but the usage cache lives "
        f"{DEFAULT_USAGE_TTL_S:.0f}s, so the cache is cold "
        f"{100 * (1 - DEFAULT_USAGE_TTL_S / default_s):.0f}% of the time"
    )


def test_the_poll_job_rotates_its_own_log_at_the_cap(tmp_path) -> None:
    """launchd appends every run's full JSON to one file and never rotates it.

    At ~19 KB a run that was 25 MB after six weeks at 900s; at 120s it would be
    ~13 MB a day. The job therefore trims itself: after each run, a log past the cap
    is moved aside as ``.1`` (one generation kept, so nothing is lost until the next
    rotation). Exercised by running the plist's own ProgramArguments the way launchd
    would -- stdout appended to the log path -- against a log already past the cap.
    """
    import subprocess

    cap = 1000
    plist = _print_installer_plist(tmp_path, quotapick="/bin/echo", POLL_LOG_MAX_BYTES=str(cap))
    args = plist["ProgramArguments"]
    log = tmp_path / "Library" / "Logs" / "llm-quota-router" / "usage-poll.log"
    assert plist["StandardOutPath"] == str(log)
    log.parent.mkdir(parents=True)
    log.write_text("x" * (cap + 1))

    with log.open("a") as out:
        subprocess.run(args, stdout=out, stderr=subprocess.STDOUT, check=True)

    rotated = log.with_name(log.name + ".1")
    assert rotated.exists(), f"a {cap + 1}-byte log was not rotated; args={args}"
    assert rotated.read_text().startswith("x" * cap)
    assert "status --json" in rotated.read_text(), "the run's own output precedes the rotation"
    assert not log.exists() or log.stat().st_size < cap

    # A log under the cap is left alone.
    log.write_text("y" * (cap // 2))
    with log.open("a") as out:
        subprocess.run(args, stdout=out, stderr=subprocess.STDOUT, check=True)
    assert log.read_text().startswith("y" * (cap // 2))
    assert rotated.read_text().startswith("x" * cap), "an under-cap run must not rotate"
