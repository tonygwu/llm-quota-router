"""The ``[engine]`` section: which objective ranks the field, and its projection knobs.

Switching objectives is a config change, not a code change, and the switch was shipped
and proven inert before the joint objective existed. An unknown objective is refused
rather than accepted and ignored: an operator who asks for one gets it or an error.
"""

from __future__ import annotations

import pytest

from quota_router import cli
from quota_router.config import ConfigError, EngineConfig, builtin_config, load_config
from tests.test_cli import _NO_CONFIG_FILES, real_capture, run


def _load(tmp_path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return load_config(
        env={"XDG_CONFIG_HOME": _NO_CONFIG_FILES}, cwd=_NO_CONFIG_FILES, explicit_path=path
    )


def test_defaults_keep_todays_objective() -> None:
    engine = builtin_config().engine
    assert engine == EngineConfig()
    assert engine.objective == "own_waste"
    assert engine.shadow is False
    assert engine.usd_per_pse is None


def test_every_key_parses(tmp_path) -> None:
    cfg = _load(
        tmp_path,
        "[engine]\n"
        'objective = "own_waste"\n'
        "shadow = true\n"
        "horizon_s = 1209600\n"
        "step_s = 1800\n"
        "forecast_max_age_s = 7200\n"
        "forecast_refresh_s = 600\n"
        "forecast_min_coverage = 0.75\n"
        "default_job_pse = 0.01\n"
        "tie_fraction = 0.2\n"
        "usd_per_pse = 305\n",
    )
    assert cfg.engine == EngineConfig(
        objective="own_waste",
        shadow=True,
        horizon_s=1209600.0,
        step_s=1800.0,
        forecast_max_age_s=7200.0,
        forecast_refresh_s=600.0,
        forecast_min_coverage=0.75,
        default_job_pse=0.01,
        tie_fraction=0.2,
        usd_per_pse=305.0,
    )
    assert not [w for w in cfg.warnings if "engine" in w]


def test_an_unknown_objective_is_a_config_error(tmp_path) -> None:
    with pytest.raises(ConfigError, match=r"\[engine\] objective.*own_waste"):
        _load(tmp_path, '[engine]\nobjective = "magic"\n')


def test_joint_is_accepted(tmp_path) -> None:
    assert _load(tmp_path, '[engine]\nobjective = "joint"\n').engine.objective == "joint"


@pytest.mark.parametrize(
    "body",
    [
        "step_s = 0",
        "horizon_s = 600\nstep_s = 3600",
        "forecast_min_coverage = 1.5",
        "default_job_pse = 0",
        "tie_fraction = -0.1",
        "usd_per_pse = 0",
        "forecast_max_age_s = -1",
        'shadow = "yes"',
    ],
)
def test_impossible_values_are_config_errors(tmp_path, body: str) -> None:
    with pytest.raises(ConfigError, match=r"\[engine\]"):
        _load(tmp_path, f"[engine]\n{body}\n")


def test_a_misspelled_key_is_reported_not_silently_dropped(tmp_path) -> None:
    cfg = _load(tmp_path, '[engine]\nobjectiv = "own_waste"\n')
    hits = [w for w in cfg.warnings if "objectiv" in w]
    assert hits, cfg.warnings
    assert "[engine]" in hits[0]


def test_the_section_is_known_so_it_is_not_reported_as_ignored(tmp_path) -> None:
    cfg = _load(tmp_path, '[engine]\nobjective = "own_waste"\n')
    assert not [w for w in cfg.warnings if "unknown config setting [engine]" in w]


def test_the_section_appears_in_to_dict() -> None:
    assert builtin_config().to_dict()["engine"]["objective"] == "own_waste"


def test_pick_exits_with_usage_on_a_bad_objective(tmp_path) -> None:
    xdg = tmp_path / "config"
    (xdg / "quota-router").mkdir(parents=True)
    (xdg / "quota-router" / "config.toml").write_text('[engine]\nobjective = "magic"\n')
    env = {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(xdg),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PATH": "/usr/bin:/bin",
    }
    code, _out, err = run(["pick", "--json"], env, snapshots=real_capture(), cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_USAGE
    assert "objective" in err
