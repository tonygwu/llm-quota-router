"""Suite-wide guards."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from quota_router import config as config_mod

pytest_plugins = ["pytester"]

#: Where a config search may not reach from a test (BACKLOG #44). Every checkout of this
#: repository sits under a home directory, so the home covers a ``.quota-router.toml`` in
#: the checkout as well as ``~/.config/quota-router``; the working directory is listed
#: for a suite run from outside the home, and the process ``XDG_CONFIG_HOME`` and
#: ``QUOTA_ROUTER_CONFIG`` for a config kept outside it (BACKLOG #47).
_REAL_ROOTS = tuple(
    Path(root).resolve()
    for root in (
        Path.home(),
        Path.cwd(),
        os.environ.get("XDG_CONFIG_HOME"),
        os.environ.get("QUOTA_ROUTER_CONFIG", "").strip(),
    )
    if root
)


def real_paths_searched(paths, *, allowed=()) -> list[str]:
    """The ``(path, required)`` search layers that fall under a real directory.

    A layer under one of ``allowed`` (pytest's own temporary folder, which a developer
    may keep under the home) is never real.
    """
    reached = []
    for path, _required in paths:
        resolved = Path(path).resolve()
        if any(resolved.is_relative_to(root) for root in allowed):
            continue
        if any(resolved.is_relative_to(root) for root in _REAL_ROOTS):
            reached.append(str(path))
    return reached


@pytest.fixture(autouse=True)
def _no_config_search_reaches_the_real_machine(request, tmp_path_factory):
    """Fail a test whose ``load_config`` would search the operator's real directories.

    ``tests/test_no_live_config_in_tests.py`` checks the calls a test module writes. This
    sees every search, however it was reached: through ``select_account``, a launcher,
    a helper's default, or an ``env`` that names neither ``HOME`` nor
    ``XDG_CONFIG_HOME``. A test marked ``live`` may search them. The patch has its own
    ``MonkeyPatch``, so a test that calls ``monkeypatch.undo()`` cannot remove it.
    """
    if request.node.get_closest_marker("live"):
        yield
        return
    reached: list[str] = []
    search = config_mod.config_search_paths
    allowed = (tmp_path_factory.getbasetemp().resolve(),)

    def guarded(env=None, cwd=None):
        paths = search(env, cwd)
        reached.extend(real_paths_searched(paths, allowed=allowed))
        return paths

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(config_mod, "config_search_paths", guarded)
        yield
    assert not reached, (
        "load_config searched the real machine; pass an env rooted in a scratch folder "
        f"and cwd=_NO_CONFIG_FILES: {sorted(set(reached))}"
    )
