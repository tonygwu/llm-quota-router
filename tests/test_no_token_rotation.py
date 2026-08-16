"""The credential-safety guard: this package must never rotate an OAuth token.

WHY THIS FILE EXISTS
--------------------
On 2026-08-15 two of the operator's three Claude logins were destroyed by this
package. The mechanism, confirmed end to end:

1. Anthropic's OAuth uses **refresh-token rotation**. Redeeming a refresh token
   returns a new one and invalidates the old one. Exactly one holder can win.
2. ``cswap`` reads per-account usage by *unconditionally* redeeming the refresh
   token first (``claude_swap/oauth.py`` -> ``grant_type=refresh_token``), even
   when the current access token is still valid.
3. It stores the rotated token in its **own** keychain store and never writes it
   back to the entry Claude Code reads (``Claude Code-credentials-<sha256[:8] of
   config dir>``). Claude Code is left holding a revoked token, its next refresh
   fails with ``invalid_grant``, and it blanks the credential -> "Not logged in".

The old contract ("cswap is an oracle; the only permitted invocation is ``cswap
list --json``") did not prevent this, because the damage is done by ``list``
itself. The invocation was read-only on disk and mutating on the server, and no
argv-shaped assertion can see that difference.

THE RULE THAT REPLACES IT
-------------------------
**Single writer.** Exactly one process on this machine may redeem a refresh
token: Claude Code itself. This package is a pure reader. It reads whatever
access token is currently in the keychain and calls the usage endpoint with it.
If that token is expired we report ``unknown`` -- we never mint a new one.

That is enforced structurally here rather than by intent, because the failure is
silent, delayed (it surfaces on the *next* session launch, not at fetch time),
and expensive (an interactive re-login per account).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "src" / "quota_router"


def _source_files() -> list[Path]:
    return sorted(p for p in PACKAGE_ROOT.rglob("*.py"))


def _module_id(path: Path) -> str:
    return str(path.relative_to(PACKAGE_ROOT))


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """Source lines with full-line comments and docstring bodies excluded.

    Prose must stay free to *describe* the hazard -- this very module names
    ``grant_type=refresh_token`` a dozen times. Only executable code is scanned.
    """
    out: list[tuple[int, str]] = []
    in_doc: str | None = None
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw
        if in_doc:
            if in_doc in line:
                line = line.split(in_doc, 1)[1]
                in_doc = None
            else:
                continue
        while True:
            m = re.search(r'"""|\'\'\'', line)
            if not m:
                break
            delim = m.group(0)
            rest = line[m.end() :]
            if delim in rest:
                line = line[: m.start()] + rest.split(delim, 1)[1]
                continue
            line = line[: m.start()]
            in_doc = delim
            break
        line = re.sub(r"#.*$", "", line)
        if line.strip():
            out.append((lineno, line))
    return out


# --------------------------------------------------------------------------
# 1. No refresh-token grant, anywhere, ever.
# --------------------------------------------------------------------------

#: The wire-level markers of a token redemption. Any of these in executable code
#: means some path can rotate a credential out from under Claude Code.
ROTATION_MARKERS = (
    "grant_type",
    "oauth/token",
    "refresh_token",
    "refreshToken",
)


@pytest.mark.parametrize("path", _source_files(), ids=_module_id)
def test_no_module_can_redeem_a_refresh_token(path: Path) -> None:
    offenders = [
        (lineno, line.strip(), marker)
        for lineno, line in _code_lines(path)
        for marker in ROTATION_MARKERS
        if marker in line
    ]
    assert not offenders, (
        f"{_module_id(path)} contains refresh-token machinery in executable code: "
        f"{offenders!r}. This package is a pure reader: exactly one process may "
        f"redeem a refresh token and that process is Claude Code. Redeeming one "
        f"here revokes the copy Claude Code holds and logs the operator out of "
        f"that account on their next session launch."
    )


# --------------------------------------------------------------------------
# 2. No dependency that redeems on our behalf.
# --------------------------------------------------------------------------

#: ``cswap`` refreshes unconditionally before every usage read, so *any*
#: invocation of it -- including the formerly-blessed ``list --json`` -- rotates
#: the operator's tokens. There is no safe subcommand.
BANNED_BINARIES = ("cswap", "claude_swap", "claude-swap")


@pytest.mark.parametrize("path", _source_files(), ids=_module_id)
def test_no_module_invokes_a_rotating_dependency(path: Path) -> None:
    offenders = [
        (lineno, line.strip(), binary)
        for lineno, line in _code_lines(path)
        for binary in BANNED_BINARIES
        if binary in line
    ]
    assert not offenders, (
        f"{_module_id(path)} references {BANNED_BINARIES} in executable code: "
        f"{offenders!r}. cswap redeems the refresh token before every usage read "
        f"and keeps the rotated token in its own store, so no invocation of it is "
        f"read-only. Read the keychain and call the usage endpoint directly instead."
    )


# --------------------------------------------------------------------------
# 3. The positive contract: usage is fetched with a bearer token we did not mint.
# --------------------------------------------------------------------------


def test_usage_endpoint_is_reached_with_a_plain_bearer_token() -> None:
    """The replacement path must GET the usage endpoint, never POST for a token.

    Verified live on 2026-08-16 against a real account: a plain access token
    returns HTTP 200 with the full window set (including the model-scoped Fable
    row at ``limits[].scope.model``), and the refresh token fingerprint is
    byte-identical before and after. No rotation is required to read usage.
    """
    from quota_router.providers import claude_oauth

    assert claude_oauth.USAGE_URL == "https://api.anthropic.com/api/oauth/usage"
    assert claude_oauth.HTTP_METHOD == "GET"


# --------------------------------------------------------------------------
# 4. A broken toolchain must not masquerade as a logged-out account.
# --------------------------------------------------------------------------


def test_a_security_binary_that_never_ran_is_reported_as_such() -> None:
    """``security`` missing or hung != "this account has no Keychain entry".

    ``run_command`` flattens every failure into a ``CommandOutcome``, and the two
    cases are distinguished only by ``returncode``: ``None`` means the process
    never ran, an int means it ran and exited non-zero. Collapsing them files an
    environment fault under a routine miss, and the operator goes looking at the
    wrong thing -- they re-login an account that was never logged out.
    """
    from quota_router.providers.base import CommandOutcome
    from quota_router.providers.claude_oauth import read_access_token

    def missing_binary(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    read = read_access_token("/nonexistent/.claude-x", runner=missing_binary)
    assert read.token is None
    assert read.problem is not None
    assert "security" in read.problem, (
        f"expected the problem to name the tool that failed, got {read.problem!r}"
    )

    # And the ordinary miss must still read as an ordinary miss.
    def no_entry(argv, **kwargs):
        return CommandOutcome(ok=False, returncode=44, stderr="could not be found")

    absent = read_access_token("/nonexistent/.claude-x", runner=no_entry)
    assert absent.problem == "no Keychain entry found for this config dir"


# --------------------------------------------------------------------------
# 5. The default account is selected by ABSENCE of CLAUDE_CONFIG_DIR.
# --------------------------------------------------------------------------


def test_the_default_account_exec_plan_must_not_set_claude_config_dir() -> None:
    """Setting CLAUDE_CONFIG_DIR=~/.claude is not a no-op -- it breaks the account.

    Account A's config lives at ``~/.claude.json``, OUTSIDE ``~/.claude``. Setting
    the variable makes the CLI look *inside* the directory, find nothing, and
    scaffold a brand-new empty account -- plus a stray Keychain entry keyed by
    ``sha256(~/.claude)``. Observed doing exactly that on this machine.

    The default account is therefore selected by the variable's ABSENCE. An exec
    plan that names it is worse than one that omits it.
    """
    from quota_router.config import load_config

    cfg = load_config(env={})
    account = cfg.accounts.get("claude")
    assert account is not None
    env = dict(account.exec_env())
    assert "CLAUDE_CONFIG_DIR" not in env, (
        "the default Claude account must be selected by unsetting CLAUDE_CONFIG_DIR, "
        f"not by pointing it at the config dir; got {env!r}"
    )


# --------------------------------------------------------------------------
# 6. A slot account must never borrow the default account's credentials.
# --------------------------------------------------------------------------


def test_a_slot_dir_with_no_keychain_entry_does_not_fall_back_to_the_default() -> None:
    """Observed the moment a fourth subscription was added, before it was logged in.

    ``keychain_service_for`` returned (suffixed, unsuffixed) for every directory.
    For the DEFAULT directory that is a sensible fallback. For a slot directory it
    is misattribution: ~/.claude-d had no entry yet, so the lookup fell through to
    ``Claude Code-credentials`` and read ACCOUNT A's token -- reporting A's usage as
    D's, and routing work to "D" that actually spends A.

    A slot with no entry is not logged in. That is the only correct answer.
    """
    from quota_router.providers.claude_oauth import keychain_service_for

    default = keychain_service_for("/Users/x/.claude", home="/Users/x")
    assert default[0] == "Claude Code-credentials"

    slot = keychain_service_for("/Users/x/.claude-d", home="/Users/x")
    assert "Claude Code-credentials" not in slot, (
        f"a slot dir must never resolve to the default account's entry; got {slot}"
    )
    assert len(slot) == 1 and slot[0].startswith("Claude Code-credentials-")
