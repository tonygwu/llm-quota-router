"""No test may call ``load_config`` or ``cli.main`` in a way that reads the live config.

This is BACKLOG #18. The check reads the source of each ``test_*.py`` and sees only
those two calls by name. Tests that reach ``load_config`` another way, such as through
``select_account`` or the launcher, are BACKLOG #44.

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
from pathlib import Path

from tests import test_cli

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
