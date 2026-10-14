"""API key files must never be committable from a checkout of this public repository.

On 2026-10-14 a plaintext Console key sat untracked in a working checkout as
``claude-d-api-key.txt``. Only its editor backup (``*~``) was ignored, so one ``git add -A``
by any agent sharing the checkout would have published it. The router's credit pools keep
keys in the Keychain and name them by service (``anthropic-api-key-claude-<x>``), so a file
whose name says it holds a key is never legitimate here.

The check asks git itself (``check-ignore --no-index``) rather than re-implementing gitignore
matching, so it tests the rules git will actually apply.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _ignored(relpath: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "--no-index", "-q", relpath],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise AssertionError(f"git check-ignore failed ({result.returncode}): {result.stderr}")
    return result.returncode == 0


@pytest.mark.parametrize(
    "relpath",
    [
        "claude-d-api-key.txt",
        "claude-d-api-key.txt~",
        "anthropic-api-key-claude-a.txt",
        "notes/openai-api-key",
        "console.key",
        "keys/anthropic.key",
    ],
)
def test_key_files_are_ignored(relpath: str) -> None:
    assert _ignored(relpath), f"{relpath} would be committable"


@pytest.mark.parametrize(
    "relpath",
    [
        "src/quota_router/secrets.py",
        "src/quota_router/credits.py",
        "tests/test_run_secrets.py",
        "README.md",
    ],
)
def test_source_files_about_keys_stay_committable(relpath: str) -> None:
    assert not _ignored(relpath), f"{relpath} must not be swallowed by a key pattern"
