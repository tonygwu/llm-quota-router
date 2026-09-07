"""Cursor source: identity yes, quota no.

Verified on the operator's machine against `cursor-agent` 2026.07.23-e383d2b. The CLI
has `status|whoami` and `about`, and neither reports quota. These are their complete
`--format json` responses, quoted exactly:

    $ cursor-agent status --format json
    {
      "status": "authenticated",
      "isAuthenticated": true,
      "hasAccessToken": true,
      "hasRefreshToken": true,
      "userInfo": {"email": "...", "userId": 157610015, "createdAt": "..."}
    }

    $ cursor-agent about --format json
    {
      "cliVersion": "2026.07.23-e383d2b", "model": "Auto",
      "subscriptionTier": "Pro", "osPlatform": "darwin", "osArch": "arm64",
      "userEmail": "...", "terminalProgram": "iterm2", "shell": "zsh",
      "lastRequestId": null
    }

No window, no percentage, no reset time, anywhere. So this adapter reports an
**unobservable** account, exactly as :mod:`quota_router.providers.antigravity` does:
no windows and ``confidence=0.0``. Inventing a number here would be how a router
silently sends every call to an exhausted account.

Identity is read from ``~/.cursor/cli-config.json`` (``authInfo.email``) rather than by
running the CLI. That file is local and free; ``cursor-agent about`` costs a subprocess
and a network round trip, measured at ~0.9s wall clock, and this adapter runs on the
critical path of opening a terminal. `subscriptionTier` is therefore NOT read here. An
operator who wants the tier scored declares it as ``tier`` in their own config, which is
the same manual override every other provider offers for an unreadable value.

**Multiple Cursor accounts.** Cursor has no per-account config directory, so there is no
equivalent of ``CLAUDE_CONFIG_DIR``. The seam is ``CURSOR_API_KEY``, which the CLI
documents and which the config layer already supports per account::

    [accounts.cursor_work]
    provider = "cursor"
    tier = "pro"
    env = { CURSOR_API_KEY = "..." }

An account carrying its own key is reported as observable-for-identity only, like the
default one. This adapter never reads the key and never sends it anywhere.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Final

from ..types import (
    ACCOUNT_CURSOR,
    PROVIDER_CURSOR,
    SOURCE_ASSUMED,
    TIER_UNKNOWN,
    AccountSnapshot,
    Identity,
)
from .base import ProviderAdapter, read_json_file

__all__ = [
    "CURSOR_BINARY",
    "CURSOR_CONFIG_FILE",
    "CURSOR_HOME_ENV",
    "CursorAdapter",
]

#: The CLI the executor spawns. Never invoked here: its presence is checked with
#: ``shutil.which``, which is a PATH lookup rather than a process.
CURSOR_BINARY: Final[str] = "cursor-agent"

#: Where the CLI keeps the signed-in identity, relative to the Cursor home.
CURSOR_CONFIG_FILE: Final[str] = "cli-config.json"

#: Override for the Cursor home directory, for tests and unusual layouts. Cursor itself
#: does not read this; it is this router's injection seam, which is why it carries the
#: router's own prefix rather than pretending to be a Cursor variable.
CURSOR_HOME_ENV: Final[str] = "QUOTA_ROUTER_CURSOR_HOME"

#: Confidence for an account with no observable quota at all. Not "low" -- *none*.
UNOBSERVABLE_CONFIDENCE: Final[float] = 0.0


class CursorAdapter(ProviderAdapter):
    """Report Cursor accounts as present, identified, and unobservable.

    Args:
        account_ids: Accounts to report on. Defaults to the single default account.
        home: Cursor home directory, overriding :data:`CURSOR_HOME_ENV` and ``~/.cursor``.
        binary: CLI whose presence marks the provider as usable.
        which: Injected ``shutil.which`` for the presence check.
        env: Environment mapping, injected for tests.
    """

    name = "cursor"

    def __init__(
        self,
        *,
        account_ids: Iterable[str] = (ACCOUNT_CURSOR,),
        home: Path | str | None = None,
        binary: str = CURSOR_BINARY,
        which: Callable[[str], str | None] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._account_ids = tuple(account_ids)
        self._home = home
        self._binary = binary
        self._which = which if which is not None else shutil.which
        self._env = env
        self.warnings: tuple[str, ...] = ()

    def _home_dir(self) -> Path:
        if self._home is not None:
            return Path(os.path.expanduser(str(self._home)))
        environ = os.environ if self._env is None else self._env
        override = (environ.get(CURSOR_HOME_ENV) or "").strip()
        if override:
            return Path(os.path.expanduser(override))
        home = environ.get("HOME") or os.path.expanduser("~")
        return Path(home) / ".cursor"

    def _identity(self) -> tuple[Identity | None, str]:
        """``(identity, note fragment)`` read from the local CLI config.

        A missing or unreadable file is not an error. It means the CLI has never been
        signed in on this machine, which is a real answer and is reported as one.
        """
        path = self._home_dir() / CURSOR_CONFIG_FILE
        payload = read_json_file(path)
        if not isinstance(payload, Mapping):
            return None, f"no readable {CURSOR_CONFIG_FILE} at {path}"
        auth: Any = payload.get("authInfo")
        if not isinstance(auth, Mapping):
            return None, f"{path} has no authInfo; the CLI is not signed in"
        email = auth.get("email")
        if not isinstance(email, str) or not email.strip():
            return None, f"{path} authInfo carries no email"
        user_id = auth.get("userId")
        return (
            Identity(
                email=email.strip(),
                organization_uuid=None,
                organization_name=None,
                account_number=str(user_id) if isinstance(user_id, (str, int)) else None,
            ),
            f"identity from {path}",
        )

    def snapshot(self, now_s: float) -> list[AccountSnapshot]:
        """One unobservable snapshot per configured Cursor account.

        ``now_s`` is unused: there is nothing time-varying to report. It stays in the
        signature because it is the adapter contract, not because this reads a clock.
        """
        installed = self._which(self._binary) is not None
        identity, identity_note = self._identity()

        out: list[AccountSnapshot] = []
        for account_id in self._account_ids:
            if installed:
                reason = "no usage API; unobservable"
            else:
                reason = f"{self._binary} is not on PATH"
            out.append(
                AccountSnapshot(
                    id=account_id,
                    provider=PROVIDER_CURSOR,
                    windows=(),
                    tier=TIER_UNKNOWN,
                    source=SOURCE_ASSUMED,
                    confidence=UNOBSERVABLE_CONFIDENCE,
                    available=installed and identity is not None,
                    note=f"{reason}; {identity_note}",
                    identity=identity,
                )
            )
        return out
