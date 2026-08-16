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
