"""A group's forecast is one budget, shared only by candidates that can serve."""

from dataclasses import replace

import pytest

from quota_router import scoring
from quota_router.select import select
from quota_router.types import AccountSnapshot, Window

NOW = 1_800_000_000.0
HOUR = 3600.0


def pool(name, *, provider="antigravity", capacity=2.43, used=0.01, hours=144):
    return AccountSnapshot(
        id=name, provider=provider, capacity_override=capacity, confidence=1.0,
        windows=(
            Window("5h", 0.0, 5 * HOUR, NOW + 5 * HOUR, NOW),
            Window("7d", used, 168 * HOUR, NOW + hours * HOUR, NOW),
        ),
    )


def config(*snaps, rate=0.07):
    return {
        "accounts": {s.id: {"weekly_to_session": 4.0} for s in snaps},
        "rate_pse_per_hour": rate,
        "min_remaining_floor": 0.02,
    }


def test_two_gemini_pools_cannot_each_claim_all_the_demand():
    a, b = pool("antigravity_gemini"), pool("antigravity_gemini_b", capacity=2.45)
    rows = {r.account_id: r for r in scoring.rank([a, b], NOW, config(a, b))}
    assert rows[a.id].rate_pse_per_hour == pytest.approx(0.07 * 9.6228 / 19.3248)
    assert rows[b.id].rate_pse_per_hour == pytest.approx(0.07 * 9.702 / 19.3248)
    assert rows[a.id].score == pytest.approx(4.6034557377)
    assert rows[b.id].score == pytest.approx(4.6413442623)
    assert sum(r.rate_pse_per_hour for r in rows.values()) == pytest.approx(0.07)


def test_allocation_uses_calibrated_remaining_stock_after_holds():
    a, b = pool("claude_a", provider="claude", capacity=1.0, used=0.2), pool(
        "claude_b", provider="claude", capacity=0.25, used=0.1,
    )
    a = replace(a, windows=(a.windows[0], replace(a.windows[1], held_fraction=0.1,
                                               reserved_fraction=0.2)))
    cfg = config(a, b)
    cfg["accounts"][b.id]["weekly_to_session"] = 8.0
    rows = {r.account_id: r for r in scoring.rank([a, b], NOW, cfg)}
    # 4 * 1 * (1 - .2 - .1 - .2) = 2; 8 * .25 * .9 = 1.8.
    assert rows[a.id].rate_pse_per_hour == pytest.approx(0.07 * 2 / 3.8)
    assert rows[b.id].rate_pse_per_hour == pytest.approx(0.07 * 1.8 / 3.8)
    assert scoring.rank([a, b], NOW, cfg) == scoring.rank([b, a], NOW, cfg)


@pytest.mark.parametrize("rate", [0.0, 0.04, 0.5])
def test_group_ceiling_applies_before_sharing_and_singletons_keep_the_rate(rate):
    a, b = pool("antigravity_gemini"), pool("antigravity_gemini_b")
    cfg = config(a, b, rate=rate)
    capped = min(rate, scoring.MAX_SUSTAINED_RATE_PSE_PER_HOUR)
    assert [r.rate_pse_per_hour for r in scoring.rank([a, b], NOW, cfg)] == pytest.approx(
        [capped / 2, capped / 2]
    )
    assert scoring.rank([a], NOW, cfg)[0].rate_pse_per_hour == pytest.approx(capped)


def test_default_rate_is_shared_too():
    a, b = pool("claude_a", provider="claude"), pool("claude_b", provider="claude")
    assert sum(r.rate_pse_per_hour for r in scoring.rank([a, b], NOW)) == pytest.approx(
        scoring.DEFAULT_RATE_PSE_PER_HOUR
    )


@pytest.mark.parametrize("exclusion", ["unavailable", "cooldown", "floor", "spent_session"])
def test_selection_redistributes_only_after_eligibility(exclusion):
    a, b = pool("antigravity_gemini"), pool("antigravity_gemini_b", capacity=20.0)
    kwargs = {}
    if exclusion == "unavailable":
        b = replace(b, available=False)
    elif exclusion == "cooldown":
        kwargs["exhausted_until"] = {b.id: NOW + HOUR}
    elif exclusion == "floor":
        b = replace(b, windows=(b.windows[0], replace(b.windows[1], used_fraction=0.99)))
    else:
        b = replace(b, windows=(replace(b.windows[0], used_fraction=1.0), b.windows[1]))
    decision = select([a, b], NOW, config(a, b), **kwargs)
    assert decision.chosen == a.id
    assert len(decision.ranked) == 1
    assert decision.ranked[0].rate_pse_per_hour == pytest.approx(0.07)
    assert decision.excluded[0].account_id == b.id


def test_no_weekly_stock_does_not_absorb_a_share():
    a, b = pool("antigravity_gemini"), pool("antigravity_gemini_b")
    b = replace(b, windows=(b.windows[0],))
    rows = {r.account_id: r for r in scoring.rank([a, b], NOW, config(a, b))}
    assert rows[a.id].rate_pse_per_hour == pytest.approx(0.07)
    assert rows[b.id].rate_pse_per_hour is None


def test_empty_or_spent_fields_do_not_divide_by_zero():
    assert scoring.rank([], NOW) == []
    a = pool("antigravity_gemini", used=1.0)
    assert select([a], NOW, config(a)).chosen is None


def test_fine_groups_and_provider_aliases_share_only_their_own_budget():
    a = pool("antigravity_gemini")
    b = pool("antigravity_claude")
    c = pool("claude_a", provider="claude")
    d = pool("anthropic_api_a", provider="anthropic_api")
    cfg = config(a, b, c, d)
    del cfg["rate_pse_per_hour"]
    cfg["demand_group_by_account"] = {a.id: "antigravity_gemini", b.id: "antigravity_claude"}
    cfg["rate_pse_per_hour_by_group"] = {
        "antigravity_gemini": 0.07, "antigravity_claude": 0.01, "claude": 0.16,
    }
    rows = {r.account_id: r for r in scoring.rank([a, b, c, d], NOW, cfg)}
    assert {k: r.rate_pse_per_hour for k, r in rows.items()} == pytest.approx({
        a.id: 0.07, b.id: 0.01, c.id: 0.08, d.id: 0.08,
    })


def test_conflicting_legacy_provider_rates_in_one_group_are_rejected():
    a, b = pool("claude_a", provider="claude"), pool("anthropic_api_a", provider="anthropic_api")
    cfg = {"rate_pse_per_hour_by_provider": {"claude": 0.1, "anthropic_api": 0.05}}
    with pytest.raises(ValueError, match="conflicting.*claude"):
        scoring.rank([a, b], NOW, cfg)


@pytest.mark.parametrize("bad", [-1, float("nan"), True, "fast"])
def test_invalid_group_rate_is_not_silently_defaulted(bad):
    a = pool("claude_a", provider="claude")
    with pytest.raises(ValueError, match="rate_pse_per_hour_by_group"):
        scoring.rank([a], NOW, {"rate_pse_per_hour_by_group": {"claude": bad}})
