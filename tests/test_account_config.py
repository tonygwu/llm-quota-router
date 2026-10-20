"""``[accounts.<id>]`` keys are a closed set, like ``[engine]`` and ``[credit_pools]``.

A typo in the one key that sizes a pool (``capacty = 0.088``, reproduced 2026-10-18)
used to load with no warning and leave the router on its default (BACKLOG #17).
"""

from __future__ import annotations

import inspect
import re

from quota_router import config as config_mod
from quota_router.config import load_config
from tests.test_cli import _NO_CONFIG_FILES

#: Every key ``_build_accounts`` reads, each with a value it accepts, across two
#: sections because ``macos_user`` and ``launch_wrapper`` belong to Antigravity alone.
EVERY_ACCOUNT_KEY = """
[accounts.claude_b]
provider = "claude"
config_dir = "/tmp/quota-router-test/.claude-b"
tier = "max_20x"
capacity = 0.5
enabled = true
identity_email = "someone@example.com"
weekly_to_session = 10.0
calls_per_window = 40
env_var = "CLAUDE_CONFIG_DIR"
command = "claude"
weekly_reset = "Fri 17:00 America/Los_Angeles"
manual_rate_per_day = 0.05
deprioritize = false
unmetered = "fallback"
plan_ends = "2026-12-01T00:00:00Z"
env = { EXTRA = "1" }

[accounts.antigravity_gemini_b]
provider = "antigravity"
macos_user = "someone"
launch_wrapper = "/usr/local/libexec/agy-as-user"
"""


def _load(tmp_path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return load_config(
        env={"XDG_CONFIG_HOME": _NO_CONFIG_FILES}, cwd=_NO_CONFIG_FILES, explicit_path=str(path)
    )


def test_a_misspelled_account_key_is_named_with_the_known_ones(tmp_path) -> None:
    config = _load(tmp_path, '[accounts.claude_b]\ncapacty = 0.088\n')
    warning, = [w for w in config.warnings if "capacty" in w]
    assert warning.startswith("unknown key [accounts.claude_b] capacty ignored (known: ")
    assert "capacity" in warning and "plan_ends" in warning
    assert config.capacities() == {}


def test_every_known_account_key_loads_without_a_warning(tmp_path) -> None:
    config = _load(tmp_path, EVERY_ACCOUNT_KEY)
    assert not [w for w in config.warnings if "unknown key" in w], config.warnings
    account = config.account("claude_b")
    assert account is not None and account.capacity == 0.5


def test_the_known_keys_are_exactly_the_keys_the_loader_reads() -> None:
    """A key added to the loader but not to the set would warn on every use; one in the
    set that the loader never reads would be accepted and ignored. Reads are found as
    ``body.get(key)``, ``body[key]`` or ``key in body``, with either quote. A key read
    through a variable, or passed with ``body`` to a helper such as ``_first_present``,
    is not seen."""
    source = inspect.getsource(config_mod._build_accounts)
    read = set(re.findall(r'body(?:\.get\(|\[)\s*["\']([A-Za-z0-9_]+)["\']', source))
    read |= set(re.findall(r'["\']([A-Za-z0-9_]+)["\']\s+(?:not\s+)?in\s+body\b', source))
    assert read == config_mod._ACCOUNT_KEYS


def test_the_fixture_covers_every_known_key() -> None:
    import tomllib

    fixture = tomllib.loads(EVERY_ACCOUNT_KEY)["accounts"]
    assert {key for body in fixture.values() for key in body} == config_mod._ACCOUNT_KEYS
