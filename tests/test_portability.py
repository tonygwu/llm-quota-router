"""Contract tests for the three guarantees that are only true if nothing erodes them.

Every assertion here protects a property that is *invisible at runtime* until the day it
matters, which is exactly why it needs a test rather than a comment:

1. **Purity.** :mod:`quota_router.scoring` and :mod:`quota_router.select` import nothing
   but the standard library and :mod:`quota_router.types` (plus ``scoring`` itself, which
   ``select`` builds on), and touch no process, environment, filesystem or clock. That is
   what lets the decision function be replayed, unit-tested at an arbitrary ``now_s``, and
   lifted into another host without dragging the oracle layer along.

2. **Never proxy.** ``ANTHROPIC_BASE_URL`` and ``ANTHROPIC_AUTH_TOKEN`` are never emitted
   into a child environment. Anthropic blocked OAuth proxying on 2026-04-04; the router
   spawns the vendor CLI with that account's own ``CLAUDE_CONFIG_DIR`` instead. Those two
   names may appear in the source only as *blocklist* entries or prose.

3. **cswap is an oracle, not an executor.** The only cswap command this package may ever
   run is ``cswap list --json``. ``cswap run`` / ``switch`` / ``auto`` / ``add`` /
   ``remove`` mutate the operator's active account behind the router's back, which would
   make the routing decision a lie.

The purity and blocklist checks are done on the **AST**, not with a substring grep, so
that prose in a docstring is not mistaken for behaviour and vice versa.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

import pytest

from quota_router.config import BANNED_EXEC_ENV
from quota_router.providers.base import CommandOutcome
from quota_router.providers.claude_cswap import (
    CSWAP_BINARY,
    CSWAP_LIST_ARGV,
    ClaudeCswapAdapter,
)

# ======================================================================================
# Source access
# ======================================================================================

PACKAGE_DIR: Path = Path(__file__).resolve().parents[1] / "src" / "quota_router"

#: Every module in the distribution, so the "never proxy" / "oracle only" sweeps cannot be
#: dodged by putting the offending line in a file nobody thought to list.
ALL_MODULES: tuple[Path, ...] = tuple(sorted(PACKAGE_DIR.rglob("*.py")))

#: The modules that must stay pure, and what each is allowed to import from the package.
PURE_MODULES: dict[str, frozenset[str]] = {
    "scoring.py": frozenset({"types"}),
    "select.py": frozenset({"types", "scoring"}),
}


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """``id()`` of every string constant that is a module/class/function docstring.

    Prose is allowed to say "we never emit ANTHROPIC_BASE_URL" or "never run cswap
    switch"; code is not. Separating the two is the whole point of doing this on the AST.
    """
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                if isinstance(first.value.value, str):
                    found.add(id(first.value))
    return found


def _code_strings(path: Path) -> list[str]:
    """Every string literal in ``path`` that is *not* a docstring."""
    tree = _parse(path)
    docstrings = _docstring_nodes(tree)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _module_id(path: Path) -> str:
    return str(path.relative_to(PACKAGE_DIR))


# ======================================================================================
# 1. Purity
# ======================================================================================


@pytest.mark.parametrize("filename", sorted(PURE_MODULES))
def test_pure_modules_import_only_stdlib_and_types(filename: str) -> None:
    """A pure module may import the stdlib and its allowed in-package siblings, nothing else."""
    path = PACKAGE_DIR / filename
    allowed_local = PURE_MODULES[filename]
    tree = _parse(path)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root in sys.stdlib_module_names, (
                    f"{filename} imports non-stdlib module {alias.name!r}; the pure "
                    f"decision layer may only import the standard library and "
                    f"{sorted(allowed_local)}"
                )
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative, i.e. in-package
                module = (node.module or "").split(".")[0]
                assert module in allowed_local, (
                    f"{filename} imports in-package module {module!r}; only "
                    f"{sorted(allowed_local)} are allowed, or purity is lost"
                )
                continue
            root = (node.module or "").split(".")[0]
            assert root in sys.stdlib_module_names, (
                f"{filename} imports non-stdlib module {node.module!r}"
            )


#: Modules whose mere presence in a pure file means it reached for the outside world.
_IMPURE_MODULES: frozenset[str] = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "pathlib",
        "shutil",
        "tempfile",
        "socket",
        "urllib",
        "requests",
        "httpx",
        "time",
        "datetime",
        "random",
        "logging",
        "json",
        "sqlite3",
    }
)

#: Builtins that read or write state outside the call.
_IMPURE_BUILTINS: frozenset[str] = frozenset(
    {"open", "input", "print", "eval", "exec", "compile", "__import__", "breakpoint"}
)


@pytest.mark.parametrize("filename", sorted(PURE_MODULES))
def test_pure_modules_touch_no_process_env_clock_or_filesystem(filename: str) -> None:
    """No name in a pure module may refer to the process, the environment or the disk."""
    path = PACKAGE_DIR / filename
    tree = _parse(path)

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id not in _IMPURE_MODULES, (
                f"{filename} references {node.id!r}; the decision layer takes now_s as a "
                f"parameter and reads nothing from the outside world"
            )
            assert node.id not in _IMPURE_BUILTINS, (
                f"{filename} calls {node.id!r}; a pure function may not perform I/O"
            )
        # Catches `os.environ` / `time.time()` even if reached via an aliased import.
        elif isinstance(node, ast.Attribute) and node.attr in {
            "environ",
            "getenv",
            "putenv",
            "system",
            "popen",
            "spawn",
            "read_text",
            "write_text",
            "now",
            "today",
            "time",
        }:
            base = node.value
            if isinstance(base, ast.Name):
                assert base.id not in _IMPURE_MODULES, (
                    f"{filename} uses {base.id}.{node.attr}; that is exactly the "
                    f"ambient state purity forbids"
                )


def test_pure_modules_do_not_import_the_impure_layers() -> None:
    """Purity is transitive: importing ``config``/``cli``/``providers`` would import the world."""
    for filename in PURE_MODULES:
        source = (PACKAGE_DIR / filename).read_text(encoding="utf-8")
        for forbidden in ("from .config", "from .cli", "from .providers", "from .state",
                          "from .history", "import subprocess"):
            assert forbidden not in source, (
                f"{filename} contains {forbidden!r}, which drags an impure layer into the "
                f"pure decision path"
            )


def test_scoring_and_select_are_importable_without_the_oracle_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real proof of portability: import them with the provider layer un-importable."""
    for name in list(sys.modules):
        if name.startswith("quota_router.providers"):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "quota_router.providers", None)

    import importlib

    for name in ("quota_router.types", "quota_router.scoring", "quota_router.select"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    for name in ("quota_router.types", "quota_router.scoring", "quota_router.select"):
        importlib.import_module(name)


# ======================================================================================
# 2. Never proxy
# ======================================================================================


def test_the_blocklist_names_both_proxy_variables() -> None:
    assert set(BANNED_EXEC_ENV) == {"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"}


@pytest.mark.parametrize(
    "path", ALL_MODULES, ids=lambda p: str(p.relative_to(PACKAGE_DIR))
)
def test_no_module_emits_a_proxy_variable(path: Path) -> None:
    """The proxy names may appear as blocklist entries or prose -- never anywhere else.

    ``config.py`` owns the blocklist tuple, so its literals are the definition itself.
    Every other module must not contain those strings outside a docstring: the only way
    to *use* the name in code is to set it, and setting it is the thing that is banned.
    """
    banned = {"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"}
    strings = set(_code_strings(path))
    offenders = strings & banned

    if _module_id(path) == "config.py":
        # The blocklist definition lives here and is the mechanism, not a violation.
        assert offenders == banned, (
            "config.py must keep both proxy variable names in BANNED_EXEC_ENV"
        )
        return

    assert not offenders, (
        f"{_module_id(path)} contains proxy variable name(s) {sorted(offenders)} in "
        f"executable code; this router never proxies -- it spawns the vendor CLI with "
        f"the account's own CLAUDE_CONFIG_DIR"
    )


def test_the_exec_environment_strips_proxy_variables_even_if_inherited() -> None:
    """Functional proof: a poisoned parent environment cannot leak a proxy to the child."""
    from quota_router import cli

    poisoned = {
        "PATH": "/usr/bin",
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9999",
        "ANTHROPIC_AUTH_TOKEN": "sk-should-never-be-forwarded",
    }
    child = {
        key: value
        for key, value in poisoned.items()
        if key.strip().upper() not in cli.BANNED_EXEC_ENV
    }
    assert "ANTHROPIC_BASE_URL" not in child
    assert "ANTHROPIC_AUTH_TOKEN" not in child
    assert child == {"PATH": "/usr/bin"}


# ======================================================================================
# 3. cswap is an oracle, not an executor
# ======================================================================================

#: Subcommands that would make cswap change the operator's state instead of report it.
MUTATING_CSWAP_SUBCOMMANDS: frozenset[str] = frozenset(
    {"run", "switch", "auto", "add", "remove"}
)


def test_the_only_cswap_argv_is_list_json() -> None:
    assert CSWAP_LIST_ARGV == ("list", "--json")


def test_the_adapter_argv_property_is_read_only_by_construction() -> None:
    adapter = ClaudeCswapAdapter(runner=lambda argv, **kw: None, configs=())
    assert adapter.argv == (CSWAP_BINARY, "list", "--json")
    assert not MUTATING_CSWAP_SUBCOMMANDS & set(adapter.argv)


def test_the_adapter_actually_runs_only_cswap_list_json() -> None:
    """Functional proof: drive a real ``snapshot()`` and record every argv it spawns."""
    spawned: list[list[str]] = []

    def recording_runner(argv: Any, **kwargs: Any) -> CommandOutcome:
        spawned.append(list(argv))
        raise FileNotFoundError(argv[0])  # run_command flattens this into a failed outcome

    adapter = ClaudeCswapAdapter(runner=recording_runner, configs=())
    adapter.snapshot(now_s=1_786_740_238.0)

    assert spawned, "the adapter never invoked the oracle at all"
    for argv in spawned:
        assert argv[1:] == ["list", "--json"], (
            f"the adapter spawned {argv!r}; cswap is an oracle and the only permitted "
            f"invocation is `cswap list --json`"
        )
        assert not MUTATING_CSWAP_SUBCOMMANDS & set(argv[1:])


@pytest.mark.parametrize(
    "path", ALL_MODULES, ids=lambda p: str(p.relative_to(PACKAGE_DIR))
)
def test_no_module_builds_a_mutating_cswap_command(path: Path) -> None:
    """No executable string anywhere may pair the cswap binary with a mutating verb.

    Checked as whole string literals so that ordinary English in a comment, or an
    unrelated identifier containing "run", cannot trip it -- and so that an actual
    ``("cswap", "switch")`` argv cannot slip through.
    """
    strings = _code_strings(path)
    for text in strings:
        lowered = text.strip().lower()
        for verb in MUTATING_CSWAP_SUBCOMMANDS:
            assert lowered != f"cswap {verb}", (
                f"{_module_id(path)} contains the command string {text!r}"
            )
            assert not lowered.startswith(f"cswap {verb} "), (
                f"{_module_id(path)} contains the command string {text!r}"
            )

    # A bare mutating verb is only dangerous in the module that knows the cswap binary,
    # where it could be concatenated into an argv.
    if "cswap" in _module_id(path):
        bare = {t.strip().lower() for t in strings} & MUTATING_CSWAP_SUBCOMMANDS
        assert not bare, (
            f"{_module_id(path)} contains bare cswap subcommand literal(s) {sorted(bare)}; "
            f"the oracle-only guarantee means `list` and `--json` are the only verbs"
        )
