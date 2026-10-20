"""The fixed-shape config sections name a key they do not know, as ``[engine]`` does.

A typo in ``[hysteresis]``, ``[staleness]``, ``[eligibility]``, ``[pileup]``,
``[oracle]``, ``[exec]`` or ``[model_classes]`` used to load with no warning and leave
the router on its default (BACKLOG #43). The same check found that the three documented
``[hysteresis]`` aliases never took effect: the builtin layer always supplies the
canonical name, and the canonical name was read first.
"""

from __future__ import annotations

import inspect
import re

import pytest

from quota_router import config as config_mod
from quota_router.config import load_config
from tests.test_cli import _NO_CONFIG_FILES

#: One misspelled key per section, and the default it must leave in place.
TYPOS = {
    "hysteresis": ("swich_margin_ratio = 9.0", lambda c: c.hysteresis.switch_margin_ratio, 1.15),
    "staleness": ("max_age = 1.0", lambda c: c.staleness.max_age_s, 300.0),
    "eligibility": ("min_remainig = 0.5", lambda c: c.eligibility.min_remaining, 0.02),
    "pileup": ("window = 1.0", lambda c: c.pileup.window_s, 60.0),
    "oracle": ("timeout = 1", lambda c: c.oracle.timeout_ms, 5000),
    "exec": ("command = { claude = 'x' }", lambda c: c.exec.commands["claude"], "claude"),
    "model_classes": ("multiplier = { opus = 9.0 }", lambda c: c.model_multipliers, None),
}

#: Every key each section's loader reads, each with a value it accepts.
EVERY_SECTION_KEY = """
[hysteresis]
enabled = true
switch_margin_ratio = 1.2
switch_margin_abs = 0.03
min_dwell_calls = 2
max_dwell_s = 600.0

[staleness]
max_age_s = 200.0
confidence_floor = 0.4
cache_max_age_s = 1800.0
max_staleness_s = 600.0

[eligibility]
min_remaining = 0.05

[pileup]
enabled = true
window_s = 30.0
calls_per_window = 100.0
max_records = 50

[oracle]
command = ""
timeout_ms = 1000
use_cache_on_failure = false

[exec]
commands = { claude = "claude" }
env_vars = { claude = "CLAUDE_CONFIG_DIR" }

[model_classes]
multipliers = { opus = 1.0 }
patterns = { opus = "opus" }
"""

#: The local name each section's table has inside ``_build``.
RAW_NAMES = {
    "hysteresis": "hysteresis_raw",
    "staleness": "staleness_raw",
    "eligibility": "eligibility_raw",
    "pileup": "pileup_raw",
    "oracle": "oracle_raw",
    "exec": "exec_raw",
    "model_classes": "model_raw",
}


def _load(tmp_path, body: str, *, env=None):
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return load_config(
        env=env or {"XDG_CONFIG_HOME": _NO_CONFIG_FILES},
        cwd=_NO_CONFIG_FILES,
        explicit_path=str(path),
    )


@pytest.mark.parametrize("section", sorted(TYPOS))
def test_a_misspelled_key_is_named_with_the_known_ones(tmp_path, section) -> None:
    line, read, default = TYPOS[section]
    config = _load(tmp_path, f"[{section}]\n{line}\n")
    typo = line.split(" =")[0]
    warning, = [w for w in config.warnings if typo in w]
    assert warning.startswith(f"unknown key [{section}] {typo} ignored (known: ")
    if default is not None:
        assert read(config) == default


def test_every_misspelled_key_in_a_section_is_named(tmp_path) -> None:
    config = _load(tmp_path, "[pileup]\nwindow = 1.0\nrecords = 5\n")
    unknown = [w for w in config.warnings if w.startswith("unknown key [pileup] ")]
    assert [w.split()[3] for w in unknown] == ["records", "window"]


def test_every_known_section_key_loads_without_a_warning(tmp_path) -> None:
    config = _load(tmp_path, EVERY_SECTION_KEY)
    assert not [w for w in config.warnings if "unknown key" in w], config.warnings
    assert config.hysteresis.switch_margin_abs == pytest.approx(0.03)
    assert config.pileup.max_records == 50


def test_the_fixture_covers_every_known_key() -> None:
    import tomllib

    parsed = tomllib.loads(EVERY_SECTION_KEY)
    assert set(parsed) == set(config_mod._SECTION_KEYS)
    for section, keys in config_mod._SECTION_KEYS.items():
        assert set(parsed[section]) == keys, section


def test_the_known_keys_are_exactly_the_keys_the_loader_reads() -> None:
    """A key read but not known would warn on every use; one known but never read would
    be accepted and ignored. Reads are found as ``raw.get("key"``, ``raw["key"]`` or
    ``"key" in raw``, with either quote, as the account-key scan finds them. A key read
    through a variable, or passed with the table to a helper, is not seen."""
    source = inspect.getsource(config_mod._build)
    assert set(RAW_NAMES) == set(config_mod._SECTION_KEYS)
    for section, raw in RAW_NAMES.items():
        read = set(
            re.findall(rf'\b{raw}(?:\.get\(|\[)\s*["\']([A-Za-z0-9_]+)["\']', source)
        )
        read |= set(re.findall(rf'["\']([A-Za-z0-9_]+)["\']\s+(?:not\s+)?in\s+{raw}\b', source))
        assert read == config_mod._SECTION_KEYS[section], section


@pytest.mark.parametrize(
    ("alias", "value", "read"),
    [
        ("switch_margin_multiplier", 1.5, lambda h: h.switch_margin_ratio),
        ("switch_margin", 0.1, lambda h: h.switch_margin_abs),
        ("ttl_s", 60.0, lambda h: h.max_dwell_s),
    ],
)
def test_a_documented_hysteresis_alias_takes_effect(tmp_path, alias, value, read) -> None:
    config = _load(tmp_path, f"[hysteresis]\n{alias} = {value}\n")
    assert read(config.hysteresis) == pytest.approx(value)
    assert not [w for w in config.warnings if alias in w], config.warnings


def test_an_alias_beside_its_canonical_name_is_named_and_loses(tmp_path) -> None:
    config = _load(tmp_path, "[hysteresis]\nswitch_margin_ratio = 1.3\nswitch_margin_multiplier = 1.9\n")
    assert config.hysteresis.switch_margin_ratio == pytest.approx(1.3)
    warning, = [w for w in config.warnings if "switch_margin_multiplier" in w]
    assert "switch_margin_ratio" in warning and "ignored" in warning


def test_an_alias_in_a_later_file_overrides_the_canonical_name_in_an_earlier_one(
    tmp_path,
) -> None:
    user = tmp_path / "xdg" / "quota-router" / "config.toml"
    user.parent.mkdir(parents=True)
    user.write_text("[hysteresis]\nswitch_margin_ratio = 1.3\n", encoding="utf-8")
    config = _load(
        tmp_path,
        "[hysteresis]\nswitch_margin_multiplier = 1.7\n",
        env={"XDG_CONFIG_HOME": str(tmp_path / "xdg")},
    )
    assert config.sources == (str(user), str(tmp_path / "config.toml"))
    assert config.hysteresis.switch_margin_ratio == pytest.approx(1.7)


@pytest.mark.parametrize(
    ("line", "alias"),
    [('ttl_s = "soon"', "ttl_s"), ("switch_margin = 2.0", "switch_margin")],
)
def test_a_bad_alias_value_is_reported_under_the_alias(tmp_path, line, alias) -> None:
    """The operator wrote the alias, so an error naming only the canonical key would point
    at text that is not in their file."""
    with pytest.raises(config_mod.ConfigError) as caught:
        _load(tmp_path, f"[hysteresis]\n{line}\n")
    assert f"(set as {alias} in {tmp_path / 'config.toml'})" in str(caught.value)


def test_a_later_canonical_name_clears_an_earlier_alias_origin(tmp_path) -> None:
    user = tmp_path / "xdg" / "quota-router" / "config.toml"
    user.parent.mkdir(parents=True)
    user.write_text("[hysteresis]\nttl_s = 60\n", encoding="utf-8")
    with pytest.raises(config_mod.ConfigError) as caught:
        _load(
            tmp_path,
            '[hysteresis]\nmax_dwell_s = "soon"\n',
            env={"XDG_CONFIG_HOME": str(tmp_path / "xdg")},
        )
    assert "max_dwell_s must be" in str(caught.value)
    assert "set as" not in str(caught.value)
