"""Waking a dark account -- the starvation loop, and the four ways to make it worse.

THE LOOP
--------
An OAuth *access* token lasts about eight hours and is renewed only when the account
is USED and the token has already lapsed. This package will never redeem a refresh
token itself (see ``tests/test_no_token_rotation.py`` for what happened the last time
something did), so an account nobody touches simply goes dark: the usage read fails,
the snapshot carries no windows, and the router cannot route to it.

That inverts the whole objective. The account with the most quota left is by
definition the one being used least, so it is the FIRST to go dark -- and once dark
the router will not spend from it, which keeps it dark. Observed live: an account at
47% Fable was invisible while the router picked one at 97%, and the cycle only broke
because the operator happened to launch that account by hand.

The fix is to ask Claude Code to do its own job: spawn the vendor CLI against that
account's config directory, let IT refresh (it is the only authorised redeemer), and
read again.

MEASURED, NOT ASSUMED
---------------------
Two measurements shaped this and both contradicted the obvious design:

* ``claude auth status`` does **not** refresh. It runs in 0.19s, makes no network
  call, and leaves the stored credential byte-identical. It reads; it does not renew.
* ``claude -p`` on a *healthy* token does not refresh either -- 3.5s, a real API
  call, and the credential is still byte-identical afterwards. Renewal happens only
  when the token has actually lapsed.

So a *scheduled* keepalive does not work: running a prompt every six hours against an
eight-hour token is a no-op that costs an API call each time, and the token expires on
its original schedule regardless. The spend is only worth anything at the moment the
read fails, which is why this is reactive and not a cron.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from quota_router import refresh as refresh_mod
from quota_router.config import BANNED_EXEC_ENV, AccountConfig


class Runner:
    """Stand-in for :func:`subprocess.run`, matching ``providers.base.Runner``.

    Records the spawn instead of making it, and returns the same duck type the rest
    of the package injects, so there is only one spawn convention to learn.
    """

    def __init__(self, *, rc: int = 0, out: str = "2", raises: Exception | None = None):
        self.calls: list[tuple[list[str], dict, float]] = []
        self.rc, self.out, self.raises = rc, out, raises

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs.get("env") or {}), kwargs.get("timeout")))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(list(argv), self.rc, self.out, "")


@pytest.fixture()
def stamp(tmp_path):
    return tmp_path / "claude_d.refresh.json"


def account(account_id="claude_d", config_dir="/home/op/.claude-d") -> AccountConfig:
    return AccountConfig(id=account_id, config_dir=config_dir)


# ======================================================================================
# It does the one thing it is for
# ======================================================================================


def test_it_spawns_the_vendor_cli_against_that_accounts_config_dir(stamp) -> None:
    runner = Runner()
    out = refresh_mod.refresh_auth(
        account(), stamp_path=stamp, now_s=1000.0, runner=runner
    )

    assert out.attempted and out.ok, out.reason
    argv, env, timeout = runner.calls[0]
    assert argv[0].endswith("claude")
    assert "-p" in argv, argv
    assert env["CLAUDE_CONFIG_DIR"] == "/home/op/.claude-d"
    assert timeout > 0


def test_the_default_account_is_selected_by_absence_not_by_naming_its_directory(
    stamp,
) -> None:
    """Design note 3, and it bites hardest here.

    Pointing ``CLAUDE_CONFIG_DIR`` at ``~/.claude`` makes Claude Code look *inside*
    the directory, find nothing, and scaffold a brand-new empty account. A refresh
    that did this would not merely fail to renew the token -- it would manufacture a
    phantom account while trying to heal the real one.
    """
    runner = Runner()
    # An inherited value, injected explicitly: this runs from shells that are already
    # pointed at another account, and relying on the ambient environment would make
    # the test pass or fail depending on who ran it.
    refresh_mod.refresh_auth(
        account("claude", "/home/op/.claude"),
        stamp_path=stamp,
        now_s=1000.0,
        runner=runner,
        env={"CLAUDE_CONFIG_DIR": "/home/op/.claude-b", "HOME": "/home/op"},
    )

    _argv, env, _t = runner.calls[0]
    assert "CLAUDE_CONFIG_DIR" not in env, (
        "omitting the variable is not enough when it is already set; it must be deleted, "
        "or the refresh renews a DIFFERENT account's token"
    )


def test_it_never_passes_a_proxy_variable(stamp) -> None:
    """The 2026-04-04 rule, enforced where a spawn actually happens."""
    runner = Runner()
    refresh_mod.refresh_auth(account(), stamp_path=stamp, now_s=1000.0, runner=runner)

    _argv, env, _t = runner.calls[0]
    for banned in BANNED_EXEC_ENV:
        assert banned not in env, banned


def test_it_asks_for_the_cheapest_model(stamp) -> None:
    """The point is to make an authenticated call, not to get an answer."""
    runner = Runner()
    refresh_mod.refresh_auth(account(), stamp_path=stamp, now_s=1000.0, runner=runner)

    argv, _env, _t = runner.calls[0]
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == refresh_mod.DEFAULT_REFRESH_MODEL


# ======================================================================================
# The cooldown -- the guard that stops this from being a fork bomb
# ======================================================================================


def test_a_second_attempt_inside_the_cooldown_does_not_spawn(stamp) -> None:
    """Without this, every routing decision spawns a CLI per dark account.

    The failure mode is not subtle: an account that stays dark (revoked credentials,
    a logged-out slot) would spawn a process on EVERY invocation, and the launcher
    runs on every new terminal. A refresh that cannot fix the problem must stop
    trying, or the remedy costs more than the disease.
    """
    runner = Runner()
    first = refresh_mod.refresh_auth(account(), stamp_path=stamp, now_s=1000.0, runner=runner)
    second = refresh_mod.refresh_auth(account(), stamp_path=stamp, now_s=1060.0, runner=runner)

    assert first.attempted
    assert not second.attempted, second.reason
    assert len(runner.calls) == 1, "the cooldown must prevent the spawn, not just the retry"
    assert "cooldown" in second.reason.lower()


def test_the_cooldown_lapses(stamp) -> None:
    runner = Runner()
    refresh_mod.refresh_auth(account(), stamp_path=stamp, now_s=1000.0, runner=runner)
    later = refresh_mod.refresh_auth(
        account(),
        stamp_path=stamp,
        now_s=1000.0 + refresh_mod.DEFAULT_REFRESH_COOLDOWN_S + 1,
        runner=runner,
    )

    assert later.attempted
    assert len(runner.calls) == 2


def test_the_cooldown_is_recorded_even_when_the_spawn_fails(stamp) -> None:
    """A failing refresh is exactly the one that must not be retried in a tight loop."""
    runner = Runner(rc=1, out="")
    first = refresh_mod.refresh_auth(account(), stamp_path=stamp, now_s=1000.0, runner=runner)
    second = refresh_mod.refresh_auth(account(), stamp_path=stamp, now_s=1030.0, runner=runner)

    assert first.attempted and not first.ok
    assert not second.attempted
    assert len(runner.calls) == 1


# ======================================================================================
# It can never be the reason a routing decision fails
# ======================================================================================


def test_a_crashing_spawn_is_reported_not_raised(stamp) -> None:
    runner = Runner(raises=OSError("no such binary"))
    out = refresh_mod.refresh_auth(account(), stamp_path=stamp, now_s=1000.0, runner=runner)

    assert out.attempted and not out.ok
    assert "OSError" in out.reason or "no such binary" in out.reason


def test_an_unwritable_stamp_does_not_stop_the_refresh(tmp_path) -> None:
    """Best-effort bookkeeping must not veto the repair it is bookkeeping for."""
    runner = Runner()
    unwritable = tmp_path / "missing-dir" / "deep" / "x.json"
    out = refresh_mod.refresh_auth(
        account(), stamp_path=unwritable, now_s=1000.0, runner=runner, make_parents=False
    )

    assert out.attempted, out.reason
    assert len(runner.calls) == 1


def test_a_corrupt_stamp_is_treated_as_no_stamp(stamp) -> None:
    stamp.write_text("{not json", encoding="utf-8")
    runner = Runner()
    out = refresh_mod.refresh_auth(account(), stamp_path=stamp, now_s=1000.0, runner=runner)

    assert out.attempted
    assert json.loads(stamp.read_text(encoding="utf-8"))["last_attempt_s"] == 1000.0


def test_an_account_with_no_config_dir_is_not_spawned_for(stamp) -> None:
    """Nothing to point the CLI at, so there is nothing to refresh."""
    runner = Runner()
    out = refresh_mod.refresh_auth(
        AccountConfig(id="claude_d"), stamp_path=stamp, now_s=1000.0, runner=runner
    )

    assert not out.attempted
    assert not runner.calls


def test_a_spawn_that_never_returns_cleanly_still_counts_as_an_attempt(stamp) -> None:
    """The stamp is written BEFORE the spawn, and this is why.

    A crash, a timeout, or a hard kill between the spawn and its result must still
    burn the cooldown. Recording the attempt afterwards inverts the guard exactly
    where it matters most: the spawn that dies is the one that would otherwise be
    retried on every single invocation, forever.

    Found by sabotage -- moving the write after the spawn passed the whole suite.
    """
    crashing = Runner(raises=TimeoutError("claude hung"))
    first = refresh_mod.refresh_auth(account(), stamp_path=stamp, now_s=1000.0, runner=crashing)
    assert first.attempted and not first.ok

    healthy = Runner()
    second = refresh_mod.refresh_auth(account(), stamp_path=stamp, now_s=1030.0, runner=healthy)
    assert not second.attempted, second.reason
    assert not healthy.calls, "a spawn that died must still hold the cooldown"


# ======================================================================================
# Wiring: the provider must only try this where there is nobody waiting
# ======================================================================================


def test_refresh_is_off_unless_the_environment_asks_for_it() -> None:
    """Latency, not caution.

    The interactive launcher caps its ENTIRE routing decision at three seconds, and a
    refresh spawn takes longer than that on its own. Enabling this everywhere would
    trade a dark account for a stall on every new terminal, so it is opt-in and the
    poller -- which has nobody waiting on it -- is what opts in.
    """
    from quota_router.providers.claude_oauth import ClaudeOAuthAdapter

    assert ClaudeOAuthAdapter(env={}).refresh_enabled is False
    assert ClaudeOAuthAdapter(env={"QUOTA_ROUTER_REFRESH_AUTH": "1"}).refresh_enabled is True
    assert ClaudeOAuthAdapter(env={"QUOTA_ROUTER_REFRESH_AUTH": "0"}).refresh_enabled is False


def test_the_stamp_lives_beside_the_usage_cache_but_survives_it(tmp_path) -> None:
    """A cooldown erased by a successful read is no cooldown on a flapping account."""
    from quota_router.providers.claude_oauth import refresh_stamp_path, usage_cache_path

    cache = usage_cache_path("claude_d", {"HOME": str(tmp_path)})
    stampp = refresh_stamp_path("claude_d", {"HOME": str(tmp_path)})
    assert stampp != cache
    assert stampp.parent == cache.parent


def test_a_dark_account_is_woken_and_re_read(tmp_path) -> None:
    """End to end: the loop actually breaks.

    The pieces passing in isolation proves nothing about the wiring, which is where
    this class of bug lives -- the provider already carried `available=False` and a
    note naming the true cause, and the router still dropped the account silently.

    Here the Keychain hands back an expired token until the CLI is spawned, exactly as
    the real one does: Claude Code renews on use, in its own process, and the next read
    simply succeeds.
    """
    from tests.test_adapters import (
        KEYCHAIN_SERVICE_BASE,
        NOW,
        FakeKeychain,
        FakeOpener,
        keychain_blob,
        make_config,
        usage_payload,
    )
    from quota_router.providers.claude_oauth import ClaudeOAuthAdapter

    config = make_config(tmp_path)
    expired = FakeKeychain({KEYCHAIN_SERVICE_BASE: keychain_blob(expires_at_s=NOW - 60)})
    healthy = FakeKeychain({KEYCHAIN_SERVICE_BASE: keychain_blob(expires_at_s=NOW + 8 * 3600)})

    state = {"woken": False}

    class Keychain:
        """Expired until the CLI runs, then valid -- the real renewal, in miniature."""

        def __call__(self, argv, **kwargs):
            return (healthy if state["woken"] else expired)(argv, **kwargs)

    spawns: list[list[str]] = []

    def refresher(argv, **kwargs):
        spawns.append(list(argv))
        state["woken"] = True
        return subprocess.CompletedProcess(list(argv), 0, "2", "")

    adapter = ClaudeOAuthAdapter(
        runner=Keychain(),
        opener=FakeOpener(usage_payload()),
        home=tmp_path,
        configs=[config],
        refresher=refresher,
        env={"QUOTA_ROUTER_REFRESH_AUTH": "1", "HOME": str(tmp_path)},
    )

    snapshot = adapter.snapshot(NOW)[0]

    assert spawns, "a dark account must be woken, not merely reported"
    assert "-p" in spawns[0]
    assert snapshot.available is True, snapshot.note
    assert snapshot.windows, "after a successful refresh the usage read must be retried"


def test_a_refresh_that_does_not_help_still_reports_the_account_as_dark(tmp_path) -> None:
    """A failed repair must not become a second failure mode.

    The account is still unavailable, still named, and the note now carries BOTH why
    the read failed and why the repair did not take -- so the next reader is not left
    guessing whether a refresh was even tried.
    """
    from tests.test_adapters import (
        KEYCHAIN_SERVICE_BASE,
        NOW,
        FakeKeychain,
        FakeOpener,
        keychain_blob,
        make_config,
        usage_payload,
    )
    from quota_router.providers.claude_oauth import ClaudeOAuthAdapter

    config = make_config(tmp_path)

    def refresher(argv, **kwargs):
        return subprocess.CompletedProcess(list(argv), 1, "", "credentials revoked")

    adapter = ClaudeOAuthAdapter(
        runner=FakeKeychain({KEYCHAIN_SERVICE_BASE: keychain_blob(expires_at_s=NOW - 60)}),
        opener=FakeOpener(usage_payload()),
        home=tmp_path,
        configs=[config],
        refresher=refresher,
        env={"QUOTA_ROUTER_REFRESH_AUTH": "1", "HOME": str(tmp_path)},
    )

    snapshot = adapter.snapshot(NOW)[0]

    assert snapshot.available is False
    assert snapshot.windows == ()
    assert "refresh:" in (snapshot.note or ""), snapshot.note
