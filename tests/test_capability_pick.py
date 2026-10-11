"""``pick --capability`` and ``select_account(capability=...)``.

The capability table is built by the real refresher over injected I/O
(``tests/test_capability.py`` fixtures); snapshots are injected the way every other CLI
test injects them. Nothing here reads the machine.
"""

from __future__ import annotations

import json
import time

import pytest

from quota_router import capability_refresh as refresh_mod
from quota_router import cli, select_account
from quota_router.providers.codex_app_server import CodexAppServerAdapter
from quota_router.state import StateStore
from quota_router.types import SOURCE_ASSUMED, AccountSnapshot
from tests.test_capability import (  # noqa: F401 - fixtures
    MIN,
    FakeIO,
    _config_file,
    _refresh,
    agy_on_path,
    env,
    home,
)
from tests.test_cli import FIVE_HOURS, NOW, _NO_CONFIG_FILES, account, run, window
from tests.test_contract_golden import _assert_golden, _normalize


def _claude(account_id, *, five=0.2, fable=0.2):
    return account(
        account_id,
        window("five_hour", five, length_s=FIVE_HOURS, resets_in_s=3600),
        window("seven_day", 0.3, resets_in_s=200_000),
        window("scoped:fable", fable, resets_in_s=200_000, applies_to={"fable"}),
    )


def _codex(account_id, *, five=0.3, extra=()):
    return account(
        account_id,
        window("five_hour", five, length_s=FIVE_HOURS, resets_in_s=1800),
        window("seven_day", 0.3, resets_in_s=300_000),
        *extra,
        tier="pro",
    )


def _antigravity(account_id):
    return AccountSnapshot(
        id=account_id, windows=(), tier="unknown", source=SOURCE_ASSUMED, confidence=0.0
    )


def _fleet(**overrides):
    snaps = {
        "claude": _claude("claude"),
        "claude_b": _claude("claude_b"),
        "codex": _codex("codex"),
        "codex_b": _codex("codex_b"),
        "antigravity_gemini": _antigravity("antigravity_gemini"),
        "antigravity_gemini_b": _antigravity("antigravity_gemini_b"),
        "antigravity_claude": _antigravity("antigravity_claude"),
    }
    snaps.update(overrides)
    return tuple(snaps.values())


@pytest.fixture()
def table(env, tmp_path, agy_on_path):
    """A fresh capability table, written by the real refresher at NOW."""
    _refresh(env, tmp_path, FakeIO(), now_s=NOW)
    return _config_file(tmp_path)


def _pick(env, config, *argv, snapshots=None, now_s=NOW, dry_run=True):
    flags = ["pick", "--config", config, *(["--dry-run"] if dry_run else []), *argv]
    code, out, err = run(flags, env, snapshots=snapshots or _fleet(), now_s=now_s,
                         cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_OK, err
    return json.loads(out)


def _rows(payload, key="ranked"):
    return {row["account"]: row for row in payload[key]}


# ======================================================================================
# Shape (T20)
# ======================================================================================


def test_a_capability_pick_is_contract_v1_and_names_a_model_on_every_row(env, table):
    payload = _pick(env, table, "--capability", "premium")

    assert payload["contract_version"] == 1
    assert payload["capability"]["requested"] == "premium"
    assert payload["decision"]["model"]["id"] in {"claude-opus-5-5", "gpt-6-sol"}
    assert payload["ranked"]
    assert all(row["model"] and row["model"]["id"] for row in payload["ranked"])
    assert all("model" in row for row in payload["excluded"])


def test_the_same_pick_without_a_capability_carries_no_new_keys(env, table):
    payload = _pick(env, table)
    assert "capability" not in payload
    assert "model" not in payload["decision"]
    assert all("model" not in row for row in payload["ranked"] + payload["excluded"])


def test_a_capability_pick_without_effort_is_byte_identical_to_446de00(env, table, tmp_path):
    # Frozen before --effort existed: the flag is additive, so a caller that never
    # passes it must see the payload it saw at 446de00, value for value.
    code, out, err = run(
        ["pick", "--config", table, "--dry-run", "--capability", "premium"],
        env, snapshots=_fleet(), now_s=NOW, cwd=_NO_CONFIG_FILES,
    )
    assert code == cli.EXIT_OK, err
    _assert_golden("pick_capability_premium", _normalize(out, tmp_path))


def test_capabilities_json_without_effort_is_byte_identical_to_446de00(env, table, tmp_path):
    code, out, err = run(
        ["capabilities", "--config", table, "--json"], env, now_s=NOW, cwd=_NO_CONFIG_FILES
    )
    assert code == cli.EXIT_OK, err
    _assert_golden("capabilities_json", _normalize(out, tmp_path))


# ======================================================================================
# Per-account resolution (T8)
# ======================================================================================


def test_each_account_carries_its_own_model(env, table):
    rows = _rows(_pick(env, table, "--capability", "premium"))
    assert rows["claude"]["model"]["id"] == "claude-opus-5-5"
    # codex's config.toml names gpt-6.1-sol, which no list carries: never requested.
    assert rows["codex"]["model"]["id"] == "gpt-6-sol"
    assert rows["codex"]["model"]["listed"] is True
    assert rows["codex_b"]["model"]["id"] == "gpt-6-sol"


def test_claude_flavour_antigravity_requests_an_agy_claude_label(env, table):
    payload = _pick(env, table, "--capability", "premium")
    rows = {**_rows(payload, "excluded"), **_rows(payload)}
    assert rows["antigravity_claude"]["model"]["id"] == "Claude Opus 5.5 (High)"
    assert rows["antigravity_claude"]["model"]["provider"] == "antigravity"
    # agy lists no Haiku: fast leaves the pool out, with the list's reason.
    excluded = _rows(_pick(env, table, "--capability", "fast"), "excluded")
    assert excluded["antigravity_claude"]["reason"] == (
        "capability fast: agy lists no claude haiku model at high effort"
    )


# ======================================================================================
# Frontier (T10, T11)
# ======================================================================================


def test_frontier_moves_to_codex_when_every_fable_window_is_spent(env, table):
    snaps = _fleet(claude=_claude("claude", fable=1.0), claude_b=_claude("claude_b", fable=1.0))
    payload = _pick(env, table, "--capability", "frontier", snapshots=snaps)

    assert payload["decision"]["account"] in {"codex", "codex_b"}
    assert payload["decision"]["model"]["id"] == "gpt-6-astra"
    assert payload["decision"]["fits"] is True
    # Codex reports no Fable window. Under a capability it is not asked to.
    assert not [w for w in payload["warnings"] if w.startswith("codex") and "fable" in w]


def test_a_spent_fable_window_does_not_bind_premium(env, table):
    snaps = _fleet(claude=_claude("claude", fable=1.0))
    rows = _rows(_pick(env, table, "--capability", "premium", snapshots=snaps))
    assert rows["claude"]["fits"] is True
    assert rows["claude"]["model"]["id"] == "claude-opus-5-5"


def test_frontier_fully_exhausted_names_the_soonest_reset_and_never_downgrades(env, table):
    snaps = _fleet(
        claude=_claude("claude", fable=1.0),
        claude_b=_claude("claude_b", fable=1.0),
        codex=_codex("codex", five=1.0),
        codex_b=_codex("codex_b", five=1.0),
    )
    payload = _pick(env, table, "--capability", "frontier", snapshots=snaps)

    decision = payload["decision"]
    assert decision["fits"] is False
    assert decision["meets_policy"] is False
    assert decision["available_at"] is not None
    assert "every candidate is out of quota" in decision["reason"]
    assert decision["model"]["id"] in {"claude-fable-5-1", "gpt-6-astra"}
    assert decision["model"]["family"] in {"fable", "astra"}


# ======================================================================================
# Pinning (T12) and window matching by id (T13)
# ======================================================================================


def test_a_pinned_model_keeps_only_its_provider(env, table):
    payload = _pick(env, table, "--capability", "premium", "--model", "claude-opus-4-8")

    assert set(_rows(payload)) <= {"claude", "claude_b"}
    assert payload["decision"]["model"] == {
        **payload["decision"]["model"], "id": "claude-opus-4-8", "source": "pinned",
    }
    assert payload["capability"]["pinned"] == "claude-opus-4-8"
    excluded = _rows(payload, "excluded")
    assert "pinned to claude-opus-4-8, a claude model" in excluded["codex"]["reason"]


def test_a_pin_the_account_does_not_list_is_excluded(env, table):
    payload = _pick(env, table, "--capability", "premium", "--model", "claude-opus-9")
    assert payload["decision"]["account"] is None
    excluded = _rows(payload, "excluded")
    assert "claude does not list claude-opus-9" in excluded["claude"]["reason"]


def test_a_pin_only_a_config_default_names_is_excluded(env, table):
    payload = _pick(env, table, "--capability", "premium", "--model", "gpt-6.1-sol")
    assert payload["decision"]["account"] is None
    excluded = _rows(payload, "excluded")
    assert "codex does not list gpt-6.1-sol" in excluded["codex"]["reason"]


def test_a_codex_window_scoped_to_the_concrete_slug_binds_only_that_model(env, table):
    astra_limit = window(
        "codex_astra", 1.0, length_s=FIVE_HOURS, resets_in_s=900,
        applies_to={"codex_astra", "gpt-6-astra"},
    )
    snaps = _fleet(codex=_codex("codex", extra=(astra_limit,)))

    frontier = _pick(env, table, "--capability", "frontier", snapshots=snaps)
    assert _rows(frontier, "excluded")["codex"]["fits"] is False
    premium = _pick(env, table, "--capability", "premium", snapshots=snaps)
    assert _rows(premium)["codex"]["fits"] is True


# ======================================================================================
# --only (T23)
# ======================================================================================


def test_capability_composes_with_only(env, table):
    payload = _pick(env, table, "--capability", "premium", "--only", "claude,claude_b")
    assert set(_rows(payload)) == {"claude", "claude_b"}
    assert payload["decision"]["provider"] == "claude"
    assert {row["model"]["id"] for row in payload["ranked"]} == {"claude-opus-5-5"}


# ======================================================================================
# The table is the only input (T21, T22)
# ======================================================================================


def test_pick_never_reads_a_model_list(env, table, monkeypatch):
    calls: list[str] = []

    def slow(*args, **kwargs):
        calls.append("called")
        time.sleep(5)
        raise AssertionError("pick read a model list")

    for name in ("read_claude_listing", "read_codex_listing", "read_agy_listing", "refresh"):
        monkeypatch.setattr(refresh_mod, name, slow)
    monkeypatch.setattr(CodexAppServerAdapter, "model_list", slow)
    deps = cli.Deps(
        load_snapshots=lambda **kw: (list(_fleet()), []),
        capability_refresh=refresh_mod.RefreshDeps(codex_model_list=slow, run=slow),
    )

    started = time.monotonic()
    code, out, err = run(
        ["pick", "--config", table, "--dry-run", "--capability", "premium"],
        env, deps=deps, cwd=_NO_CONFIG_FILES,
    )
    elapsed = time.monotonic() - started

    assert code == cli.EXIT_OK, err
    assert json.loads(out)["decision"]["model"]["id"]
    assert calls == []
    assert elapsed < 1.0


def test_without_a_table_every_account_is_excluded_with_the_fix(env, tmp_path):
    payload = _pick(env, _config_file(tmp_path), "--capability", "premium")
    assert payload["decision"]["account"] is None
    reasons = {row["reason"] for row in payload["excluded"]}
    assert any("no capability table at" in r and "capabilities --refresh" in r for r in reasons)


def test_an_expired_table_is_not_used(env, table):
    payload = _pick(env, table, "--capability", "premium", now_s=NOW + 61 * MIN)
    claude = _rows(payload, "excluded")["claude"]
    assert "is the usage poller running?" in claude["reason"]


# ======================================================================================
# What a recorded pick spends (T18)
# ======================================================================================


def test_a_recorded_pick_reserves_at_the_chosen_models_weight(env, table):
    _pick(env, table, "--capability", "fast", "--only", "claude", dry_run=False)
    reservations = StateStore(env=env).load().reservations
    assert [(r.account, r.cost) for r in reservations] == [("claude", 0.01)]  # haiku

    _pick(env, table, "--capability", "frontier", "--only", "claude_b", dry_run=False)
    costs = {r.account: r.cost for r in StateStore(env=env).load().reservations}
    assert costs["claude_b"] == 1.0  # fable


# ======================================================================================
# The Python API (T4)
# ======================================================================================


def test_select_account_carries_the_capability_and_model(env, table):
    deps = cli.Deps(load_snapshots=lambda **kw: (list(_fleet()), []))
    plain = select_account(env=env, now_s=NOW, deps=deps, record=False, config=table,
                           cwd=_NO_CONFIG_FILES)
    assert plain.capability is None and plain.model is None

    chosen = select_account(env=env, now_s=NOW, deps=deps, record=False, config=table,
                            cwd=_NO_CONFIG_FILES, capability="frontier")
    assert chosen.capability == "frontier"
    assert chosen.model.id == chosen.to_dict()["decision"]["model"]["id"]
    assert chosen.model.family in {"fable", "astra"}


def test_select_account_rejects_an_unknown_capability(env):
    with pytest.raises(ValueError, match="unknown capability 'ultra'"):
        select_account(env=env, capability="ultra")


def test_pick_rejects_an_unknown_capability(env, table):
    code, _out, err = run(["pick", "--config", table, "--capability", "ultra"], env,
                          snapshots=_fleet(), cwd=_NO_CONFIG_FILES)
    assert code != cli.EXIT_OK
    assert "invalid choice: 'ultra'" in err


# ======================================================================================
# --effort: the app chooses the effort, the router translates it per account
# ======================================================================================

LOW_TO_MAX = ["low", "medium", "high", "xhigh", "max"]


def _efforts(payload):
    # Ranked and excluded alike: Antigravity pools are excluded as fallback-only (here for
    # carrying no usage windows) and still carry the model they would request.
    return {
        row["account"]: (row["model"]["id"], row["model"]["effort"])
        for row in payload["ranked"] + payload["excluded"]
        if row.get("model")
    }


def test_effort_xhigh_on_premium_is_exact_on_claude_and_codex_and_clamped_on_agy(env, table):
    rows = _efforts(_pick(env, table, "--capability", "premium", "--effort", "xhigh"))

    exact = {"requested": "xhigh", "applied": "xhigh", "clamped": False, "supported": LOW_TO_MAX}
    assert rows["claude"] == ("claude-opus-5-5", exact)
    assert rows["codex"] == ("gpt-6-sol", exact)
    # agy has no xhigh: the highest label at or below it, reported as a clamp.
    assert rows["antigravity_gemini"] == ("Gemini 3.1 Pro (High)", {
        "requested": "xhigh", "applied": "high", "clamped": True, "supported": ["low", "high"],
    })
    assert rows["antigravity_claude"] == ("Claude Opus 5.5 (High)", {
        "requested": "xhigh", "applied": "high", "clamped": True, "supported": ["low", "high"],
    })


def test_an_agy_model_id_becomes_the_label_at_the_applied_effort(env, table):
    payload = _pick(env, table, "--capability", "premium", "--effort", "medium")
    rows = _efforts(payload)
    assert rows["antigravity_gemini"][0] == "Gemini 3.1 Pro (Low)"
    assert rows["antigravity_gemini"][1]["applied"] == "low"
    assert rows["antigravity_claude"][0] == "Claude Opus 5.5 (Low)"
    # Claude Code and Codex take the effort as a flag; their id never changes.
    assert rows["claude"] == ("claude-opus-5-5", {
        "requested": "medium", "applied": "medium", "clamped": False, "supported": LOW_TO_MAX,
    })


def test_every_clamp_is_reported_in_the_warnings_too(env, table):
    payload = _pick(env, table, "--capability", "premium", "--effort", "xhigh")
    clamps = [w for w in payload["warnings"] if w.startswith("effort xhigh clamped")]
    assert clamps == [
        "effort xhigh clamped on 3 accounts: antigravity_claude -> high, "
        "antigravity_gemini -> high, antigravity_gemini_b -> high"
    ]
    assert not [w for w in _pick(env, table, "--capability", "premium", "--effort", "high")
                ["warnings"] if w.startswith("effort ")]


def test_effort_never_changes_which_account_wins(env, table):
    # Ranking is a quota decision. A clamped account is not demoted; it is reported.
    plain = _pick(env, table, "--capability", "premium")
    for level in LOW_TO_MAX:
        with_effort = _pick(env, table, "--capability", "premium", "--effort", level)
        assert with_effort["decision"]["account"] == plain["decision"]["account"]
        assert [r["account"] for r in with_effort["ranked"]] == [
            r["account"] for r in plain["ranked"]
        ]
        assert with_effort["decision"]["model"]["effort"]["requested"] == level


def test_a_model_that_takes_no_effort_is_excluded_with_the_reason(env, table):
    # The fixture catalog's main Haiku is claude-haiku-4-5, whose thinking is "none".
    payload = _pick(env, table, "--capability", "fast", "--effort", "max")
    excluded = _rows(payload, "excluded")
    assert "claude-haiku-4-5-20251001 takes no effort level" in excluded["claude"]["reason"]
    assert excluded["claude"]["model"] is None
    # Codex Luna stops at max, so max is exact there.
    assert _efforts(payload)["codex"] == ("gpt-6-luna", {
        "requested": "max", "applied": "max", "clamped": False, "supported": LOW_TO_MAX,
    })


def test_a_pinned_model_is_clamped_to_its_own_levels(env, table):
    payload = _pick(env, table, "--capability", "premium", "--model", "gpt-5.5",
                    "--effort", "max")
    assert _efforts(payload)["codex"] == ("gpt-5.5", {
        "requested": "max", "applied": "xhigh", "clamped": True,
        "supported": ["low", "medium", "high", "xhigh"],
    })


def test_a_table_without_effort_levels_excludes_rather_than_guesses(env, table):
    from quota_router import capability as cap_mod

    path = cap_mod.table_path(env)
    data = json.loads(path.read_text())
    data["accounts"]["codex"]["capabilities"]["premium"]["committed"].pop("effort_ids")
    path.write_text(json.dumps(data))

    payload = _pick(env, table, "--capability", "premium", "--effort", "high")
    reason = _rows(payload, "excluded")["codex"]["reason"]
    assert "records no effort levels for gpt-6-sol" in reason
    # Without --effort the same table still routes codex: the gap only matters under it.
    assert "codex" in _rows(_pick(env, table, "--capability", "premium"))


def test_effort_without_a_capability_is_a_usage_error(env, table):
    code, _out, err = run(["pick", "--config", table, "--effort", "high"], env,
                          snapshots=_fleet(), cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_USAGE
    assert "--effort needs --capability" in err


def test_pick_rejects_an_unknown_effort(env, table):
    code, _out, err = run(
        ["pick", "--config", table, "--capability", "premium", "--effort", "ultra"],
        env, snapshots=_fleet(), cwd=_NO_CONFIG_FILES,
    )
    assert code == cli.EXIT_USAGE
    assert "invalid choice: 'ultra'" in err


def test_capabilities_json_translates_the_effort_per_cell(env, table):
    code, out, err = run(["capabilities", "--config", table, "--json", "--effort", "xhigh"],
                         env, now_s=NOW, cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_OK, err
    rows = {row["account"]: row["capabilities"] for row in json.loads(out)["accounts"]}
    gemini = rows["antigravity_gemini"]["premium"]["model"]
    assert (gemini["id"], gemini["effort"]["applied"], gemini["effort"]["clamped"]) == (
        "Gemini 3.1 Pro (High)", "high", True,
    )
    assert rows["codex"]["premium"]["model"]["effort"]["applied"] == "xhigh"
    # A cell whose model takes no effort does not resolve under --effort.
    assert rows["claude"]["fast"]["model"] is None
    assert "takes no effort level" in rows["claude"]["fast"]["reason"]


def test_capabilities_text_lists_every_clamp(env, table):
    code, out, err = run(["capabilities", "--config", table, "--effort", "xhigh"],
                         env, now_s=NOW, cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_OK, err
    assert "antigravity_gemini premium: effort xhigh clamped to high (supports low, high)" in out
    assert "Gemini 3.1 Pro (High)" in out
    assert "codex premium: effort" not in out  # an exact level is not a clamp


def test_select_account_translates_the_effort(env, table):
    deps = cli.Deps(load_snapshots=lambda **kw: (list(_fleet()), []))
    chosen = select_account(env=env, now_s=NOW, deps=deps, record=False, config=table,
                            cwd=_NO_CONFIG_FILES, capability="premium", effort="xhigh")
    assert chosen.model.effort == chosen.to_dict()["decision"]["model"]["effort"]
    assert chosen.model.effort["requested"] == "xhigh"


def test_select_account_rejects_an_effort_without_a_capability(env):
    with pytest.raises(ValueError, match="effort needs a capability"):
        select_account(env=env, effort="high")
    with pytest.raises(ValueError, match="unknown effort 'ultra'"):
        select_account(env=env, capability="premium", effort="ultra")
