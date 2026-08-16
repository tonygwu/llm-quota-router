"""The Python API: ``select_account`` must agree with the CLI, by construction.

WHY THIS EXISTS
---------------
The README advertised ``from quota_router import select_account`` from the day it
was written. The function did not exist -- the decision logic grew inside the CLI
and the library surface never followed. Anyone who tried the documented import got
an ImportError.

The fix is deliberately *not* a second implementation. ``select_account`` drives
the same ``_prepare`` / ``_pick_payload`` path the CLI runs, so the two surfaces
cannot drift apart without both changing. These tests pin that agreement rather
than testing the wrapper's arithmetic twice.
"""

from __future__ import annotations

import json

import pytest

from quota_router import Selection, select_account
from quota_router import cli

from tests.test_cli import NOW, real_capture, run


def _deps(snapshots):
    return cli.Deps(load_snapshots=lambda **kw: (list(snapshots), []))


def _env(tmp_path):
    return {
        "HOME": str(tmp_path),
        "QUOTA_ROUTER_STATE": str(tmp_path / "state.json"),
        "QUOTA_ROUTER_HISTORY": str(tmp_path / "history.jsonl"),
    }


# ======================================================================================
# The contract with the CLI
# ======================================================================================


def test_select_account_returns_the_same_decision_as_the_cli(tmp_path) -> None:
    """Same inputs, same answer. This is the whole point of reusing the CLI path."""
    snaps = real_capture()
    env = _env(tmp_path)

    from_cli = json.loads(
        run(["pick", "--json", "--dry-run"], env, snapshots=snaps, now_s=NOW)[1]
    )
    from_lib = select_account(
        env=env, now_s=NOW, deps=_deps(snaps), record=False
    )

    assert from_lib.account == from_cli["decision"]["account"]
    assert from_lib.provider == from_cli["decision"]["provider"]
    assert from_lib.exec_env == from_cli["exec"]["env"]
    assert [r["account"] for r in from_lib.ranked] == [
        r["account"] for r in from_cli["ranked"]
    ]


def test_to_dict_is_the_cli_payload(tmp_path) -> None:
    """``Selection.to_dict()`` is the ``pick --json`` payload, not a lookalike."""
    snaps = real_capture()
    env = _env(tmp_path)

    from_cli = json.loads(
        run(["pick", "--json", "--dry-run"], env, snapshots=snaps, now_s=NOW)[1]
    )
    from_lib = select_account(env=env, now_s=NOW, deps=_deps(snaps), record=False)

    assert from_lib.to_dict() == from_cli


def test_the_contract_version_is_exposed(tmp_path) -> None:
    """Consumers pin on this; it has to be reachable without parsing JSON."""
    sel = select_account(
        env=_env(tmp_path), now_s=NOW, deps=_deps(real_capture()), record=False
    )
    assert sel.contract_version >= 1


# ======================================================================================
# The behaviours a library consumer depends on
# ======================================================================================


def test_only_restricts_the_candidate_set(tmp_path) -> None:
    sel = select_account(
        only=["claude_c"],
        env=_env(tmp_path),
        now_s=NOW,
        deps=_deps(real_capture()),
        record=False,
    )
    assert sel.account == "claude_c"


def test_exclude_removes_a_candidate(tmp_path) -> None:
    snaps = real_capture()
    unconstrained = select_account(
        env=_env(tmp_path), now_s=NOW, deps=_deps(snaps), record=False
    )
    narrowed = select_account(
        exclude=[unconstrained.account],
        env=_env(tmp_path),
        now_s=NOW,
        deps=_deps(snaps),
        record=False,
    )
    assert narrowed.account != unconstrained.account


def test_the_default_account_exec_env_omits_claude_config_dir(tmp_path) -> None:
    """Selecting account A means UNSETTING the variable, never naming its directory.

    Pointing CLAUDE_CONFIG_DIR at ``~/.claude`` makes the CLI scaffold a fresh empty
    account, because account A's config lives at ``~/.claude.json``, outside it. A
    library consumer that merges ``exec_env`` into ``os.environ`` would otherwise
    break the very account it was told to use.
    """
    sel = select_account(
        only=["claude"],
        env=_env(tmp_path),
        now_s=NOW,
        deps=_deps(real_capture()),
        record=False,
    )
    assert sel.account == "claude"
    assert "CLAUDE_CONFIG_DIR" not in sel.exec_env, sel.exec_env


def test_record_false_writes_no_state(tmp_path) -> None:
    """A caller that decides and then abandons must not book quota against anyone."""
    state = tmp_path / "state.json"
    select_account(
        env=_env(tmp_path), now_s=NOW, deps=_deps(real_capture()), record=False
    )
    assert not state.exists(), "record=False must not persist a reservation"


def test_record_true_persists_a_reservation(tmp_path) -> None:
    state = tmp_path / "state.json"
    select_account(
        env=_env(tmp_path), now_s=NOW, deps=_deps(real_capture()), record=True
    )
    assert state.exists(), "record=True must book the pick so concurrent callers see it"


def test_it_degrades_instead_of_raising_when_no_account_is_usable(tmp_path) -> None:
    """A router that cannot answer must never stop the caller from working."""
    sel = select_account(
        env=_env(tmp_path), now_s=NOW, deps=_deps([]), record=False
    )
    assert isinstance(sel, Selection)
    assert sel.account is None or isinstance(sel.account, str)
    assert isinstance(sel.exec_env, dict)


def test_the_documented_readme_snippet_actually_runs(tmp_path) -> None:
    """The exact shape the README tells people to write.

    This file exists because that snippet was fiction for as long as the README
    has existed. Pin it so it cannot become fiction again.
    """
    import os

    decision = select_account(
        model="fable", env=_env(tmp_path), now_s=NOW, deps=_deps(real_capture()), record=False
    )
    child_env = {**os.environ, **decision.exec_env}
    assert isinstance(child_env, dict)
    assert decision.account is not None
