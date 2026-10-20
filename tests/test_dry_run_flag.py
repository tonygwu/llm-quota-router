"""``--dry-run`` is accepted only by the commands that honour it (BACKLOG #36).

Every subcommand built with the shared parser used to accept it, and most ignored it:
``calibrations --append --dry-run`` appended to the ledger, ``credits set --dry-run``
recorded a balance, and ``capabilities --refresh --dry-run`` rewrote the table that
``pick --capability`` reads. ``run`` dropped the flag after it spent a real call on
2026-10-16. Now argparse refuses it everywhere else, before anything runs.
"""

from __future__ import annotations

import io

import pytest

from quota_router import cli
from tests.test_cli import _NO_CONFIG_FILES

#: Each command that does not honour --dry-run, as a command line argparse accepts. For a
#: writer it names the write, but some of these would fail their own validation before
#: writing, so the usage error that names --dry-run is the proof, not the state folder.
#: The flag goes straight after the command word, where the shared parser took it; for
#: ``credits`` that is before ``set`` or ``grant``.
REFUSERS = {
    "calibrations-append": ["calibrations", "--append", "[]"],
    "calibrations-mark-reconstructed": ["calibrations", "--mark-reconstructed", "test"],
    "capabilities-refresh": ["capabilities", "--refresh"],
    "credits-set": ["credits", "set", "p", "--remaining", "1"],
    "credits-grant": ["credits", "grant", "p", "--amount", "1", "--expires", "2026-12-01T00:00:00Z"],
    "forecast-refresh": ["forecast", "--refresh"],
    "report-failure": ["report-failure", "claude", "--text", "x"],
    "forensics": ["forensics"],
    "doctor": ["doctor"],
    "calibrate": ["calibrate"],
    "launch-plan": ["launch-plan", "claude"],
    "replay": ["replay"],
}

#: The commands that honour it.
HONOURERS = ("pick", "exec", "explain", "status", "waste")


def _refuse(*args, **kwargs):
    raise AssertionError("a refused --dry-run must stop before anything runs")


def _env(tmp_path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "PATH": "/nonexistent",
    }


@pytest.mark.parametrize("name", sorted(REFUSERS))
def test_a_command_that_ignores_dry_run_refuses_it(tmp_path, name) -> None:
    deps = cli.Deps(
        load_snapshots=_refuse, run=_refuse, capability_refresh=_refuse, read_secret=_refuse
    )
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(
        [REFUSERS[name][0], "--dry-run", *REFUSERS[name][1:]],
        env=_env(tmp_path),
        stdout=out,
        stderr=err,
        deps=deps,
        cwd=_NO_CONFIG_FILES,
    )
    assert code == cli.EXIT_USAGE, err.getvalue()
    assert "--dry-run" in err.getvalue()
    assert not (tmp_path / "state").exists()


def test_every_refuser_is_a_real_command_line_without_the_flag() -> None:
    """A typo in a fixture would make argparse refuse it for a reason unrelated to --dry-run."""
    parser = cli._build_parser()
    for argv in REFUSERS.values():
        parser.parse_args(argv)


@pytest.mark.parametrize("command", HONOURERS)
def test_a_command_that_honours_dry_run_still_takes_it(command) -> None:
    args = cli._build_parser().parse_args([command, "--dry-run"])
    assert args.dry_run is True


def test_every_command_on_the_shared_parser_is_listed() -> None:
    """A new subcommand must decide whether it honours --dry-run."""
    parser = cli._build_parser()
    subparsers = next(a for a in parser._actions if a.dest == "command")
    shared = {
        name
        for name, sub in subparsers.choices.items()
        if any(a.dest == "no_sticky" for a in sub._actions)
    }
    listed = {argv[0] for argv in REFUSERS.values()} | set(HONOURERS)
    assert shared == listed
