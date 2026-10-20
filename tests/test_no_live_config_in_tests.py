"""No test may call ``load_config`` or ``cli.main`` in a way that reads the live config.

This is BACKLOG #18. The check reads the source of each ``test_*.py`` and sees only
those two calls by name. ``tests/conftest.py`` backs it at run time (BACKLOG #44): it
fails any test whose config search reaches the real home, the working directory, or the
process ``XDG_CONFIG_HOME`` or ``QUOTA_ROUTER_CONFIG``, however the search was reached,
and allows pytest's own temp folder. The last tests below check that run-time guard.

``load_config`` searches ``$XDG_CONFIG_HOME`` (else ``$HOME/.config``, and with no
``HOME`` in ``env``, the real home) and the working directory. A call with an empty
``env``, or with no ``cwd``, therefore reads ``~/.config/quota-router/config.toml`` and
any ``.quota-router.toml`` where the suite runs. ``cli.main`` passes its own ``env``
and ``cwd`` straight through, and with no ``env`` it uses the process environment. It
happened on 2026-08-24, 2026-09-16 and 2026-10-18: tests passed in CI, which has no
live config, and failed on the operator's machine as soon as the live config touched
what they asserted.
"""

from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path

import pytest

from quota_router.config import config_search_paths
from tests import test_cli
from tests.conftest import real_paths_searched

TESTS = Path(__file__).resolve().parent


def _is_config_reader(func: ast.expr) -> bool:
    """``load_config(...)``, ``config.load_config(...)``, ``cli.main(...)`` or ``main(...)``."""
    if isinstance(func, ast.Name):
        return func.id in ("load_config", "main")
    if isinstance(func, ast.Attribute):
        if func.attr == "load_config":
            return True
        return func.attr == "main" and isinstance(func.value, ast.Name) and func.value.id == "cli"
    return False


def _config_reading_calls():
    for path in sorted(TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_config_reader(node.func):
                yield path.name, node


def test_every_config_reading_call_names_an_env_and_a_cwd() -> None:
    offenders = []
    for filename, call in _config_reading_calls():
        if any(kw.arg is None for kw in call.keywords):
            continue  # **kwargs: the caller's own call is what this test sees
        keywords = {kw.arg: kw.value for kw in call.keywords}
        env = keywords.get("env")
        if env is None:
            offenders.append(f"{filename}:{call.lineno} has no env")
        elif isinstance(env, ast.Dict) and not env.keys:
            offenders.append(f"{filename}:{call.lineno} passes env={{}}")
        if "cwd" not in keywords:
            offenders.append(f"{filename}:{call.lineno} has no cwd")
    assert offenders == [], "\n".join(offenders)


def test_the_shared_run_helper_defaults_to_no_project_config() -> None:
    """``run`` forwards ``cwd=cwd``, so the call passes the check above whatever the default."""
    default = inspect.signature(test_cli.run).parameters["cwd"].default
    assert default == test_cli._NO_CONFIG_FILES


def test_the_guard_sees_calls() -> None:
    calls = [call for _, call in _config_reading_calls()]
    assert sum(1 for call in calls if getattr(call.func, "attr", None) == "main") >= 9
    assert len(calls) > 50


@pytest.mark.parametrize(
    ("env", "cwd"),
    [
        ({}, test_cli._NO_CONFIG_FILES),
        ({"XDG_STATE_HOME": "/nonexistent/state"}, test_cli._NO_CONFIG_FILES),
        ({"XDG_CONFIG_HOME": test_cli._NO_CONFIG_FILES}, None),
        (None, test_cli._NO_CONFIG_FILES),
    ],
    ids=["empty-env", "env-without-home-or-config-home", "no-cwd", "process-env"],
)
def test_the_run_time_guard_sees_a_search_of_the_real_machine(env, cwd) -> None:
    """The shapes the source check above cannot see. ``config_search_paths`` reads no
    file, so asking it is safe; with the process environment it still names the real
    home whenever ``XDG_CONFIG_HOME`` is unset or under the home."""
    if env is None and "XDG_CONFIG_HOME" in os.environ:
        env = {k: v for k, v in os.environ.items() if k != "XDG_CONFIG_HOME"}
    assert real_paths_searched(config_search_paths(env, cwd))


def test_the_run_time_guard_is_installed_for_this_test(monkeypatch) -> None:
    """Without this, dropping ``autouse`` or the patch would leave the suite green."""
    from quota_router import config as config_mod

    assert config_mod.config_search_paths.__name__ == "guarded"
    monkeypatch.undo()
    assert config_mod.config_search_paths.__name__ == "guarded"


def test_the_run_time_guard_allows_pytest_temp_folders() -> None:
    """A temp folder under a real root, as with ``--basetemp=./tmp``, is not real."""
    layers = [(Path.cwd() / "tmp" / "config.toml", False)]
    assert real_paths_searched(layers)
    assert real_paths_searched(layers, allowed=((Path.cwd() / "tmp").resolve(),)) == []


def test_the_run_time_guard_passes_a_search_kept_off_the_machine(tmp_path) -> None:
    rooted = {"HOME": str(tmp_path), "XDG_CONFIG_HOME": test_cli._NO_CONFIG_FILES}
    assert real_paths_searched(config_search_paths(rooted, test_cli._NO_CONFIG_FILES)) == []


def test_a_search_of_the_real_machine_fails_the_test(pytester, monkeypatch, tmp_path) -> None:
    """The guard must fire, not only be installed. A refactor that bypasses the patched
    name, or a guard that stops recording or asserting, leaves these inner tests green.

    The inner run is a subprocess, so this test's own guard never sees its searches, and
    it gets a scratch HOME, so the real home is never searched.
    """
    home = tmp_path / "home"
    home.mkdir()
    operator_config = tmp_path / "operator" / "operator.toml"
    operator_config.parent.mkdir()
    operator_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("QUOTA_ROUTER_CONFIG", str(operator_config))
    monkeypatch.setenv("PYTHONPATH", str(TESTS.parent / "src"))
    pytester.makeconftest((TESTS / "conftest.py").read_text(encoding="utf-8"))
    pytester.makepyfile(
        test_inner=f"""
        import os
        from quota_router.config import load_config

        NOWHERE = {test_cli._NO_CONFIG_FILES!r}

        def test_the_home():
            load_config(env={{}}, cwd=NOWHERE)

        def test_the_process_xdg_config_home():
            load_config(env={{"HOME": NOWHERE, "XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"]}},
                        cwd=NOWHERE)

        def test_the_process_quota_router_config():
            env = {{"HOME": NOWHERE, "XDG_CONFIG_HOME": NOWHERE,
                    "QUOTA_ROUTER_CONFIG": os.environ["QUOTA_ROUTER_CONFIG"]}}
            load_config(env=env, cwd=NOWHERE)

        def test_a_search_kept_off_the_machine():
            load_config(env={{"HOME": NOWHERE, "XDG_CONFIG_HOME": NOWHERE}}, cwd=NOWHERE)

        def test_a_search_of_pytest_temp_folder(tmp_path):
            # The inner temp folder sits under the inner working directory, a real root,
            # so this passes only while the fixture allows pytest's base temp.
            load_config(env={{"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)}},
                        cwd=tmp_path)
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=5, errors=3)
