"""Reading an API key from the Keychain without ever printing it.

The router names a credit pool's key by Keychain item (``keychain_service``) and reads
it only when it is about to start a child that needs it. The key travels in that child's
environment and nowhere else: never argv (``ps`` shows argv), never stdout, history,
state, the ledger, or an exception message.

:func:`read_api_key` calls ``security`` itself rather than going through the providers'
``run_command``, whose errors quote the child's output.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Any, Final

REDACTED: Final[str] = "<redacted>"


class SecretError(RuntimeError):
    """A key could not be read. The message names the item, never its content."""


class Secret:
    """A key that prints as ``<redacted>``. :meth:`reveal` is the one way out."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise SecretError("an empty key is not a key")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def scrub(self, text: Any) -> Any:
        """``text`` with every occurrence of the key replaced; non-strings pass through."""
        if isinstance(text, str):
            return text.replace(self._value, REDACTED)
        return text

    def __repr__(self) -> str:
        return REDACTED

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return REDACTED


def read_api_key(
    service: str,
    *,
    runner: Callable[..., Any] = subprocess.run,
    timeout_s: float = 10.0,
) -> Secret:
    """The key in Keychain item ``service`` (``security find-generic-password -w``)."""
    try:
        completed = runner(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SecretError(f"cannot read Keychain item {service!r}: {type(exc).__name__}") from None
    if completed.returncode != 0:
        raise SecretError(
            f"Keychain item {service!r} not found or not readable "
            f"(security exited {completed.returncode})"
        )
    value = (completed.stdout or "").strip()
    if not value:
        raise SecretError(f"Keychain item {service!r} is empty")
    return Secret(value)
