"""Test package for llm-quota-router.

Exists so the suite is an importable package (stable module names, no ``sys.path``
games) and so every test module has one obvious way to reach the shared fixtures::

    from tests import load_cswap_real

    payload = load_cswap_real()
    assert payload["accounts"][0]["usage"]["sevenDay"]["pct"] == 60.0

Fixture policy
--------------
``fixtures/cswap_real.json`` is a **real** capture of ``cswap list --json`` from the
operator's machine, redacted for commit: emails were replaced with
``acct1@example.com`` / ``acct2@example.com`` / ``acct3@example.com`` and the
``organizationUuid`` values with synthetic (but structurally valid) v4 UUIDs. Every
numeric and timestamp byte is untouched, including two properties worth preserving
deliberately:

* the ``scoped[]`` entries (``name: "Fable"``) that carry per-model-class windows, which
  is how a model class gates routing at all, and
* the upstream ``aheadOfPace`` inconsistency -- account 1 reports ``false`` for
  ``pct 60.0 > expectedPct 54.6`` while its scoped window reports ``true`` for
  ``80.0 > 54.6``. The router must compute that sign itself; the fixture is the
  regression evidence, so do not "fix" those booleans.

Do not add un-redacted captures. Live-machine tests belong behind the ``live`` marker
(deselected by default; run with ``pytest -m live``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["TESTS_DIR", "FIXTURES_DIR", "CSWAP_REAL_FIXTURE", "load_cswap_real"]

#: Directory holding this package.
TESTS_DIR: Path = Path(__file__).resolve().parent

#: Directory holding committed test fixtures.
FIXTURES_DIR: Path = TESTS_DIR / "fixtures"

#: Redacted capture of ``cswap list --json`` -- the ground-truth oracle payload shape.
CSWAP_REAL_FIXTURE: Path = FIXTURES_DIR / "cswap_real.json"


def load_cswap_real() -> dict[str, Any]:
    """Parse :data:`CSWAP_REAL_FIXTURE` and return it as a plain dict.

    A fresh object every call, so a test that mutates the payload (to simulate a missing
    field, a stale read, an exhausted account) cannot leak that mutation into another
    test.
    """
    return json.loads(CSWAP_REAL_FIXTURE.read_text(encoding="utf-8"))
