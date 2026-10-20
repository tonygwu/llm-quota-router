"""Suite-wide guards."""

from __future__ import annotations

from pathlib import Path

import pytest

from quota_router import config as config_mod

#: Where a config search may not reach from a test (BACKLOG #44). Every checkout of this
#: repository sits under a home directory, so the home covers a ``.quota-router.toml`` in
#: the checkout as well as ``~/.config/quota-router``; the working directory is listed
#: for a suite run from outside the home.
_REAL_ROOTS = (Path.home().resolve(), Path.cwd().resolve())


def real_paths_searched(paths) -> list[str]:
    """The ``(path, required)`` search layers that fall under a real directory."""
    return [
        str(path)
        for path, _required in paths
        if any(Path(path).resolve().is_relative_to(root) for root in _REAL_ROOTS)
    ]


@pytest.fixture(autouse=True)
def _no_config_search_reaches_the_real_machine(request, monkeypatch):
    """Fail a test whose ``load_config`` would search the operator's real directories.

    ``tests/test_no_live_config_in_tests.py`` checks the calls a test module writes. This
    sees every search, however it was reached: through ``select_account``, a launcher,
    a helper's default, or an ``env`` that names neither ``HOME`` nor
    ``XDG_CONFIG_HOME``. A test marked ``live`` may search them.
    """
    if request.node.get_closest_marker("live"):
        yield
        return
    reached: list[str] = []
    search = config_mod.config_search_paths

    def guarded(env=None, cwd=None):
        paths = search(env, cwd)
        reached.extend(real_paths_searched(paths))
        return paths

    monkeypatch.setattr(config_mod, "config_search_paths", guarded)
    yield
    assert not reached, (
        "load_config searched the real machine; pass an env rooted in a scratch folder "
        f"and cwd=_NO_CONFIG_FILES: {sorted(set(reached))}"
    )
