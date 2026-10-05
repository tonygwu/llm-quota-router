"""Byte-level goldens for the v1 contract, frozen at 61d9525.

The key-set test in ``test_cli.py`` proves no key was renamed or dropped. These prove
more: with no new flag, every value ``pick``, ``launch-plan`` and ``status`` print is the
value they printed before capability routing existed. digital-twin treats any change to
``contract_version`` as "router unavailable" and falls back silently, and verbatim-index
matches on reason wording, so a quiet drift here would not fail loudly anywhere else.

Two things vary run to run and are masked: the temp directory every path is rooted in,
and ``generated_at``. Nothing else is.

To regenerate after an *intended* contract change, run with
``QUOTA_ROUTER_REGEN_GOLDEN=1`` and review the diff of ``tests/fixtures/golden/``. A
missing golden fails; it is never written implicitly.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from quota_router import cli, select_account
from tests.test_cli import (
    FIVE_HOURS,
    NOW,
    _NO_CONFIG_FILES,
    _agy_config,
    account,
    real_capture,
    run,
    window,
)

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden"
REGEN_ENV = "QUOTA_ROUTER_REGEN_GOLDEN"

#: Keys that only a capability request may add. Their absence at every depth is the
#: backward-compatibility promise for callers that never pass the new flag.
CAPABILITY_ONLY_KEYS = frozenset({"capability", "model"})


@pytest.fixture()
def env(tmp_path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PATH": "/usr/bin:/bin",
    }


def _normalize(text: str, tmp_path: Path) -> Any:
    payload = json.loads(text.replace(str(tmp_path), "<TMP>"))
    if isinstance(payload, dict) and "generated_at" in payload:
        payload["generated_at"] = "<GENERATED_AT>"
    return payload


def _assert_golden(name: str, payload: Any) -> None:
    path = GOLDEN_DIR / f"{name}.json"
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if os.environ.get(REGEN_ENV) == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        return
    assert path.exists(), f"missing golden {path}; regenerate with {REGEN_ENV}=1 and review"
    assert rendered == path.read_text(encoding="utf-8"), f"{name} drifted from its golden"


def _keys_at_every_depth(node: Any) -> set[str]:
    if isinstance(node, dict):
        found = set(node)
        for value in node.values():
            found |= _keys_at_every_depth(value)
        return found
    if isinstance(node, list):
        found: set[str] = set()
        for item in node:
            found |= _keys_at_every_depth(item)
        return found
    return set()


def _pick(argv, env, tmp_path, **kwargs) -> Any:
    code, out, err = run(argv, env, cwd=_NO_CONFIG_FILES, **kwargs)
    assert code == cli.EXIT_OK, err
    payload = _normalize(out, tmp_path)
    assert payload["contract_version"] == 1
    assert not (_keys_at_every_depth(payload) & CAPABILITY_ONLY_KEYS)
    return payload


def _exhausted() -> tuple:
    return (
        account("claude", window("five_hour", 1.0, length_s=FIVE_HOURS, resets_in_s=9000)),
        account("claude_b", window("five_hour", 1.0, length_s=FIVE_HOURS, resets_in_s=600)),
        account("claude_c", window("five_hour", 1.0, length_s=FIVE_HOURS, resets_in_s=4000)),
    )


def _explode(**kwargs):
    raise RuntimeError("usage read exploded")


# ======================================================================================
# pick
# ======================================================================================


def test_pick_without_capability_is_byte_identical(env, tmp_path):
    payload = _pick(["pick", "--dry-run"], env, tmp_path, snapshots=real_capture())
    _assert_golden("pick_real_capture", payload)


def test_pick_with_model_class_is_byte_identical(env, tmp_path):
    payload = _pick(
        ["pick", "--dry-run", "--model", "fable"], env, tmp_path, snapshots=real_capture()
    )
    _assert_golden("pick_model_fable", payload)


def test_pick_degraded_shape_is_byte_identical(env, tmp_path):
    payload = _pick(
        ["pick", "--dry-run"], env, tmp_path, deps=cli.Deps(load_snapshots=_explode)
    )
    _assert_golden("pick_degraded", payload)


def test_pick_all_exhausted_shape_is_byte_identical(env, tmp_path):
    payload = _pick(["pick", "--dry-run"], env, tmp_path, snapshots=_exhausted())
    _assert_golden("pick_all_exhausted", payload)


def test_pick_with_no_snapshots_is_byte_identical(env, tmp_path):
    payload = _pick(["pick", "--dry-run"], env, tmp_path, snapshots=())
    _assert_golden("pick_no_snapshots", payload)


# ======================================================================================
# launch-plan and status
# ======================================================================================


def test_launch_plan_is_byte_identical(env, tmp_path):
    code, out, err = run(
        ["launch-plan", "antigravity_claude_b", "--config", _agy_config(tmp_path), "--json"],
        env,
        cwd=_NO_CONFIG_FILES,
    )
    assert code == cli.EXIT_OK, err
    _assert_golden("launch_plan_antigravity_claude_b", _normalize(out, tmp_path))


def test_status_json_is_byte_identical(env, tmp_path):
    code, out, err = run(
        ["status", "--json"], env, snapshots=real_capture(), cwd=_NO_CONFIG_FILES
    )
    assert code == cli.EXIT_OK, err
    _assert_golden("status_real_capture", _normalize(out, tmp_path))


# ======================================================================================
# The Python API
# ======================================================================================


def test_select_account_without_capability_matches_the_pick_golden(env, tmp_path):
    selection = select_account(
        env=env,
        now_s=NOW,
        deps=cli.Deps(load_snapshots=lambda **kw: (list(real_capture()), [])),
        record=False,
        cwd=_NO_CONFIG_FILES,
    )
    payload = _normalize(json.dumps(selection.to_dict()), tmp_path)
    # Compare only: the CLI test owns this golden, so a regeneration can never let the
    # library's output overwrite the CLI's.
    golden = json.loads((GOLDEN_DIR / "pick_real_capture.json").read_text(encoding="utf-8"))
    assert payload == golden
