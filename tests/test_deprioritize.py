"""Tests for ``[accounts.<id>] deprioritize = true``.

An operator may want routing to prefer one account over another for a while, for a
reason the quota numbers cannot see. The key says so without hardcoding an account:

* a deprioritized account ranks below every eligible account that is not deprioritized,
  whatever its score, and whatever the sticky incumbent or the switch margin says;
* it stays eligible, so it is chosen when nothing else is eligible;
* the default is false, so an account without the key routes exactly as before.

Nothing here reads the operator's machine. Snapshots are built in memory and every config
file lives in the test's temp dir.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from quota_router import cli, select_account
from quota_router.config import ConfigError, load_config
from quota_router.types import SOURCE_LIVE, AccountSnapshot, Window
from tests.test_cli import NOW, pick, run

DAY = 86400.0

SECOND_CODEX = '[accounts.codex_b]\nprovider = "codex"\nconfig_dir = "~/.codex-b"\n'
DEPRIORITIZE_CODEX = "\n[accounts.codex]\ndeprioritize = true\n"
NOTE_LINE = "deprioritized: codex"


def weekly(used: float, days_to_reset: float) -> Window:
    return Window(
        key="7d",
        used_fraction=used,
        length_s=7 * DAY,
        resets_at_s=NOW + days_to_reset * DAY,
        observed_at_s=NOW,
    )


def codex(used: float = 0.28, days: float = 6.6) -> AccountSnapshot:
    return AccountSnapshot(id="codex", windows=(weekly(used, days),), tier="pro", source=SOURCE_LIVE)


def codex_b(used: float = 0.0, days: float = 7.0) -> AccountSnapshot:
    return AccountSnapshot(id="codex_b", windows=(weekly(used, days),), tier="pro", source=SOURCE_LIVE)


@pytest.fixture()
def env(tmp_path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PATH": "/usr/bin:/bin",
    }


def write_config(env: dict[str, str], body: str) -> Path:
    path = Path(env["XDG_CONFIG_HOME"]) / "quota-router" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


PICK = ["pick", "--only", "codex,codex_b", "--dry-run"]


# ======================================================================================
# Configuration: per account, default false, refuse what cannot be right
# ======================================================================================


def test_deprioritize_is_read_per_account(env, tmp_path) -> None:
    path = tmp_path / "dep.toml"
    path.write_text("[accounts.codex]\ndeprioritize = true\n", encoding="utf-8")
    cfg = load_config(env=env, explicit_path=path)
    assert cfg.account("codex").deprioritize is True
    assert cfg.account("claude").deprioritize is False
    assert cfg.account("codex").to_dict()["deprioritize"] is True


def test_no_account_is_deprioritized_unless_the_operator_says_so(env) -> None:
    cfg = load_config(env=env)
    assert {a.id: a.deprioritize for a in cfg.accounts.values()} == {
        a.id: False for a in cfg.accounts.values()
    }


@pytest.mark.parametrize("value", ['"true"', "1", "0", '"yes"'])
def test_a_non_boolean_deprioritize_is_refused(env, tmp_path, value: str) -> None:
    path = tmp_path / "bad.toml"
    path.write_text(f"[accounts.codex]\ndeprioritize = {value}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="deprioritize"):
        load_config(env=env, explicit_path=path)


def test_deprioritize_on_an_account_the_router_cannot_read_is_refused(env, tmp_path) -> None:
    """A mistyped id would otherwise create a phantom account and change nothing."""
    path = tmp_path / "typo.toml"
    path.write_text("[accounts.codx]\ndeprioritize = true\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="codx") as caught:
        load_config(env=env, explicit_path=path)
    assert "codx" in str(caught.value) and "deprioritize" in str(caught.value)


# ======================================================================================
# Routing
# ======================================================================================


def test_without_the_key_the_first_account_wins(env) -> None:
    """The control: with these inputs codex wins on its own numbers (it resets first)."""
    write_config(env, SECOND_CODEX)
    payload = pick(PICK, env, snapshots=[codex(), codex_b()])
    assert payload["decision"]["account"] == "codex", payload["decision"]
    assert "deprioritiz" not in payload["decision"]["reason"]


def test_a_deprioritized_account_loses_even_when_it_would_win(env) -> None:
    write_config(env, SECOND_CODEX + DEPRIORITIZE_CODEX)
    payload = pick(PICK, env, snapshots=[codex(), codex_b()])
    assert payload["decision"]["account"] == "codex_b", payload["decision"]
    assert [row["account"] for row in payload["ranked"]] == ["codex_b", "codex"]
    assert payload["decision"]["meets_policy"] is True
    reason = payload["decision"]["reason"]
    assert "codex" in reason and "deprioritized" in reason, reason


def test_a_deprioritized_account_is_chosen_when_nothing_else_is_eligible(env) -> None:
    write_config(env, SECOND_CODEX + DEPRIORITIZE_CODEX)
    payload = pick(
        PICK + ["--min-remaining", "0.5"],
        env,
        snapshots=[codex(), codex_b(used=0.9)],
    )
    assert payload["decision"]["account"] == "codex", payload["decision"]
    assert payload["decision"]["meets_policy"] is True
    assert "deprioritized" in payload["decision"]["reason"], payload["decision"]
    assert [row["account"] for row in payload["excluded"]] == ["codex_b"]


def _seat_codex(env) -> None:
    """Record one real pick of codex so it is the sticky incumbent for this field."""
    first = pick(
        ["pick", "--only", "codex,codex_b"], env, snapshots=[codex(), codex_b()]
    )
    assert first["decision"]["account"] == "codex", first["decision"]


def test_the_sticky_incumbent_holds_the_seat_without_the_key(env) -> None:
    """The control for the next test: the dwell keeps codex where codex_b now leads."""
    write_config(env, SECOND_CODEX)
    _seat_codex(env)
    payload = pick(PICK, env, snapshots=[codex(), codex_b(days=1.0)])
    assert payload["decision"]["account"] == "codex", payload["decision"]
    assert payload["decision"]["sticky"] is True


def test_a_deprioritized_sticky_incumbent_does_not_keep_the_pick(env) -> None:
    write_config(env, SECOND_CODEX)
    _seat_codex(env)
    write_config(env, SECOND_CODEX + DEPRIORITIZE_CODEX)
    payload = pick(PICK, env, snapshots=[codex(), codex_b()])
    assert payload["decision"]["account"] == "codex_b", payload["decision"]
    assert payload["decision"]["sticky"] is False
    assert "deprioritized" in payload["decision"]["reason"], payload["decision"]


def _select_with_codex_seated(cfg):
    """Pure select(): codex_b leads clearly, codex is a long-seated incumbent."""
    from quota_router.select import SelectionState, StickyEntry, select, sticky_key

    state = SelectionState(
        entries={
            sticky_key(["codex", "codex_b"], None): StickyEntry(
                account_id="codex", calls=10, since_s=NOW - 10_000, last_s=NOW
            )
        }
    )
    return select([codex(), codex_b(days=1.0)], NOW, cfg, state=state, record=False)


def test_a_released_incumbent_is_not_credited_when_it_would_have_lost_anyway() -> None:
    """Found on real data: the reason blamed the key for a pick the scores already made."""
    control = _select_with_codex_seated({})
    assert control.chosen == "codex_b" and control.sticky_applied is False, control.reason

    decision = _select_with_codex_seated({"deprioritized_accounts": ["codex"]})
    assert decision.chosen == "codex_b"
    assert "deprioritized" not in decision.reason, decision.reason


def test_the_explain_headline_does_not_credit_the_key_when_the_winner_leads_on_score(env) -> None:
    """Found on real data: 'because codex is deprioritized, not on score' beside a higher score."""
    write_config(env, SECOND_CODEX + DEPRIORITIZE_CODEX)
    code, _, err = run(PICK + ["--explain"], env, snapshots=[codex(), codex_b(days=1.0)])
    assert code == 0
    headline = err.splitlines()[0]
    assert headline.startswith("codex_b won"), headline
    assert "because codex is deprioritized" not in headline, headline
    assert NOTE_LINE in err, err


def test_the_library_api_agrees_with_pick(env) -> None:
    """`cl` and `cdx` route through select_account, so this is what they will see."""
    write_config(env, SECOND_CODEX + DEPRIORITIZE_CODEX)
    snaps = [codex(), codex_b()]
    selection = select_account(
        only=["codex", "codex_b"],
        env=env,
        now_s=NOW,
        deps=cli.Deps(load_snapshots=lambda **kw: (list(snaps), [])),
        record=False,
    )
    assert selection.account == "codex_b"


# ======================================================================================
# What the operator sees
# ======================================================================================


def test_pick_explain_names_the_deprioritized_account_and_the_row(env) -> None:
    write_config(env, SECOND_CODEX + DEPRIORITIZE_CODEX)
    code, _, err = run(PICK + ["--explain"], env, snapshots=[codex(), codex_b()])
    assert code == 0
    assert NOTE_LINE in err, err
    headline = err.splitlines()[0]
    assert headline.startswith("codex_b won") and "deprioritized" in headline, headline
    row = next(line for line in err.splitlines() if re.match(r"2\s+codex\s", line))
    assert "deprioritized" in row, row


def test_explain_names_the_deprioritized_account(env) -> None:
    write_config(env, SECOND_CODEX + DEPRIORITIZE_CODEX)
    code, out, _ = run(
        ["explain", "--only", "codex,codex_b"], env, snapshots=[codex(), codex_b()]
    )
    assert code == 0
    assert NOTE_LINE in out, out


def test_explain_says_nothing_about_it_without_the_key(env) -> None:
    write_config(env, SECOND_CODEX)
    code, out, _ = run(
        ["explain", "--only", "codex,codex_b"], env, snapshots=[codex(), codex_b()]
    )
    assert code == 0
    assert "deprioritiz" not in out, out


def test_status_names_the_deprioritized_account(env) -> None:
    write_config(env, SECOND_CODEX + DEPRIORITIZE_CODEX)
    code, out, _ = run(["status"], env, snapshots=[codex(), codex_b()])
    assert code == 0
    assert NOTE_LINE in out, out


def test_status_json_carries_deprioritize_per_account(env) -> None:
    write_config(env, SECOND_CODEX + DEPRIORITIZE_CODEX)
    code, out, _ = run(["status", "--json"], env, snapshots=[codex(), codex_b()])
    assert code == 0
    accounts = {a["id"]: a for a in json.loads(out)["accounts"]}
    assert accounts["codex"]["deprioritized"] is True
    assert accounts["codex_b"]["deprioritized"] is False
