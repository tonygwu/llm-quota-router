"""Value types shared by every layer of the quota router.

This module is the *contract*. Sources (the vendor usage endpoint, ``~/.claude*/.claude.json``) produce
:class:`AccountSnapshot` objects; scoring turns them into :class:`ScoreBreakdown`
objects; selection turns those into a :class:`Decision`; the CLI renders it. Nothing
here knows about subprocesses, files, the environment or the wall clock.

Purity rules (enforced by tests elsewhere in the tree)
-----------------------------------------------------
* stdlib imports only,
* no filesystem / env / network access,
* no implicit clock: every time-dependent computation takes ``now_s`` (epoch seconds,
  UTC) as an explicit parameter.

The algorithm these types encode
--------------------------------
For each usage window of each account::

    remaining_fraction  = 1 - used_fraction
    burn_rate prior     = 1 / length_s                  (uniform pacing prior)
    expected_demand     = burn_rate * time_to_reset      (fraction still expected to burn)
    slack               = remaining_fraction - expected_demand

When the upstream oracle already publishes a pacing baseline (``expectedPct``) the same
number falls out of a simpler identity, which is what we use verbatim::

    slack = expected_used_fraction - used_fraction

(the two agree exactly: ``(1 - used) - (1 - expected) == expected - used``).

Per account::

    min_slack(account) = min(slack) over the windows that APPLY to the requested model
                         class -- a window whose ``applies_to`` excludes the class is
                         skipped entirely; the tightest applicable window throttles the
                         rest.
    capacity(account)  = tier capacity ratio (max_20x -> 1.0, max_5x -> 0.25, pro -> 1.0)

And then two mandatory regimes:

``REGIME_A`` (surplus -- *any* candidate has ``min_slack > 0``)
    ``score = provider_weight * capacity * min_slack`` -> argmax.
    Spend from the pool with the most quota at risk of expiring unused.

``REGIME_B`` (scarcity -- *every* candidate has ``min_slack <= 0``)
    ``score = provider_weight * capacity * min_remaining`` -> argmax.
    Spend from the pool that can actually serve the call.

The second regime is not an optimization, it is a correctness requirement: multiplying a
*negative* slack by a capacity ratio moves it toward zero, so under scarcity a scaled
slack would rank the *smaller* account first (``claude_c`` at ``-0.5`` scores ``-0.125``
and beats ``claude`` at ``-0.2``). Surplus and deficit are different objectives.

One more trap, recorded here because :class:`ScoreBreakdown` is where it bites: the
hysteresis / switch margin used by selection applies to the **unscaled** ``min_slack``,
never to ``score``. An additive epsilon on a capacity-scaled score is 4x stricter for a
``max_5x`` account than for a ``max_20x`` one.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

__all__ = [
    "QUOTA_ROUTER_CONTRACT_VERSION",
    # account identity / providers
    "ACCOUNT_CLAUDE",
    "ACCOUNT_CLAUDE_B",
    "ACCOUNT_CLAUDE_C",
    "ACCOUNT_CLAUDE_D",
    "ACCOUNT_CODEX",
    "ACCOUNT_ANTIGRAVITY_GEMINI",
    "ACCOUNT_ANTIGRAVITY_CLAUDE",
    "ACCOUNT_IDS",
    "ACCOUNT_PROVIDERS",
    "PROVIDER_CLAUDE",
    "PROVIDER_CODEX",
    "PROVIDER_ANTIGRAVITY",
    "PROVIDER_UNKNOWN",
    "PROVIDERS",
    "DEFAULT_PROVIDER_WEIGHTS",
    "is_canonical_account_id",
    "provider_for_account_id",
    # tiers / capacity
    "TIER_MAX_20X",
    "TIER_MAX_5X",
    "TIER_PRO",
    "TIER_UNKNOWN",
    "TIER_CAPACITY",
    "UNKNOWN_TIER_CAPACITY",
    "WINDOW_KEY_5H",
    "WINDOW_KEY_7D",
    "normalize_tier",
    "capacity_for_tier",
    # model classes
    "MODEL_CLASS_FABLE",
    "normalize_model_class",
    # provenance / confidence
    "SOURCE_LIVE",
    "SOURCE_CLAUDE_JSON",
    "SOURCE_MANUAL",
    "SOURCE_CACHE",
    "SOURCE_ASSUMED",
    "SOURCE_UNKNOWN",
    "CONFIDENCE_LABELS",
    "confidence_label",
    # regimes
    "REGIME_A",
    "REGIME_B",
    "normalize_regime",
    # dataclasses
    "Identity",
    "Window",
    "AccountSnapshot",
    "unreadable_reason",
    "WindowSlack",
    "ScoreBreakdown",
    "Decision",
]


# ======================================================================================
# Contract version
# ======================================================================================

#: Bump when the shape of the dataclasses below changes in a way a consumer must notice.
#: The TypeScript consumer reads this out of ``quotapick --json`` and refuses to parse a
#: payload from a version it does not understand.
QUOTA_ROUTER_CONTRACT_VERSION: Final[int] = 1


# ======================================================================================
# Small pure helpers
# ======================================================================================

#: Float slop tolerated when validating values that must live in ``[0, 1]``.
_TOL: Final[float] = 1e-9


def _clamp01(value: float) -> float:
    """Clamp ``value`` into ``[0.0, 1.0]``."""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _as_finite(value: Any, field_name: str) -> float:
    """Coerce ``value`` to a finite ``float`` or raise."""
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:  # pragma: no cover - message is the payload
        raise TypeError(f"{field_name} must be a real number, got {value!r}") from exc
    if not math.isfinite(out):
        raise ValueError(f"{field_name} must be finite, got {out!r}")
    return out


def _as_fraction(value: Any, field_name: str) -> float:
    """Coerce ``value`` to a finite float in ``[0.0, 1.0]`` or raise.

    Values within :data:`_TOL` of the bounds are clamped (float noise), anything further
    out is a bug in the caller -- most often a percentage that was never divided by 100 --
    and fails loud rather than silently poisoning a routing decision.
    """
    out = _as_finite(value, field_name)
    if out < -_TOL or out > 1.0 + _TOL:
        raise ValueError(
            f"{field_name} must be a fraction in [0, 1], got {out!r} "
            f"(percentages must be divided by 100)"
        )
    return _clamp01(out)


def _as_text(value: Any, field_name: str, *, allow_empty: bool = False) -> str:
    """Coerce ``value`` to a stripped ``str``; optionally reject the empty string."""
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise TypeError(f"{field_name} must be a string, got {value!r}")
    if not text and not allow_empty:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


# ======================================================================================
# Canonical account ids and providers
# ======================================================================================

ACCOUNT_CLAUDE: Final[str] = "claude"
ACCOUNT_CLAUDE_B: Final[str] = "claude_b"
ACCOUNT_CLAUDE_C: Final[str] = "claude_c"
ACCOUNT_CLAUDE_D: Final[str] = "claude_d"
ACCOUNT_CODEX: Final[str] = "codex"
ACCOUNT_ANTIGRAVITY_GEMINI: Final[str] = "antigravity_gemini"
ACCOUNT_ANTIGRAVITY_CLAUDE: Final[str] = "antigravity_claude"

#: The complete, ordered set of routable account ids. These strings are the wire format:
#: the TypeScript consumer maps them 1:1 with **no translation table**, so they must
#: never be renamed, aliased or prettified on the way out.
ACCOUNT_IDS: Final[tuple[str, ...]] = (
    ACCOUNT_CLAUDE,
    ACCOUNT_CLAUDE_B,
    ACCOUNT_CLAUDE_C,
    ACCOUNT_CLAUDE_D,
    ACCOUNT_CODEX,
    ACCOUNT_ANTIGRAVITY_GEMINI,
    ACCOUNT_ANTIGRAVITY_CLAUDE,
)

PROVIDER_CLAUDE: Final[str] = "claude"
PROVIDER_CODEX: Final[str] = "codex"
PROVIDER_ANTIGRAVITY: Final[str] = "antigravity"
PROVIDER_UNKNOWN: Final[str] = "unknown"

#: Providers a routable account can belong to. A provider is the *billing/CLI* pool
#: (which binary gets spawned), not the model family: ``antigravity_gemini`` and
#: ``antigravity_claude`` are two pools behind the one ``antigravity`` CLI.
PROVIDERS: Final[tuple[str, ...]] = (
    PROVIDER_CLAUDE,
    PROVIDER_CODEX,
    PROVIDER_ANTIGRAVITY,
)

#: Canonical account id -> provider.
ACCOUNT_PROVIDERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        ACCOUNT_CLAUDE: PROVIDER_CLAUDE,
        ACCOUNT_CLAUDE_B: PROVIDER_CLAUDE,
        ACCOUNT_CLAUDE_C: PROVIDER_CLAUDE,
        ACCOUNT_CLAUDE_D: PROVIDER_CLAUDE,
        ACCOUNT_CODEX: PROVIDER_CODEX,
        ACCOUNT_ANTIGRAVITY_GEMINI: PROVIDER_ANTIGRAVITY,
        ACCOUNT_ANTIGRAVITY_CLAUDE: PROVIDER_ANTIGRAVITY,
    }
)

#: Neutral provider weights. Weighting is *policy*, so the config layer owns the real
#: values; scoring falls back to these so it can run with no configuration at all.
DEFAULT_PROVIDER_WEIGHTS: Final[Mapping[str, float]] = MappingProxyType(
    {provider: 1.0 for provider in PROVIDERS}
)


def is_canonical_account_id(account_id: str) -> bool:
    """Return ``True`` when ``account_id`` is one of the six wire-format ids."""
    return account_id in ACCOUNT_PROVIDERS


def provider_for_account_id(account_id: str) -> str:
    """Best-effort provider for an account id.

    Canonical ids resolve exactly. Unknown ids fall back to a prefix heuristic so that a
    hypothetical future ``claude_d`` still lands in the ``claude`` pool instead of being
    silently weighted as an unknown provider; anything unrecognizable returns
    :data:`PROVIDER_UNKNOWN`.
    """
    known = ACCOUNT_PROVIDERS.get(account_id)
    if known is not None:
        return known
    text = (account_id or "").strip().casefold()
    for provider in (PROVIDER_ANTIGRAVITY, PROVIDER_CLAUDE, PROVIDER_CODEX):
        if text == provider or text.startswith(f"{provider}_"):
            return provider
    return PROVIDER_UNKNOWN


# ======================================================================================
# Subscription tiers and capacity ratios
# ======================================================================================

TIER_MAX_20X: Final[str] = "max_20x"
TIER_MAX_5X: Final[str] = "max_5x"
TIER_PRO: Final[str] = "pro"
TIER_UNKNOWN: Final[str] = "unknown"

#: Capacity used when the tier could not be determined. Deliberately *neutral* (1.0):
#: an unknown tier must neither flatter nor punish an account, and the resulting
#: uncertainty is expressed through :attr:`AccountSnapshot.confidence` instead.
UNKNOWN_TIER_CAPACITY: Final[float] = 1.0

#: Canonical window keys. These live here rather than in the providers layer because
#: the pure scoring core has to name them to normalize units, and the pure core may
#: not import the providers layer.
WINDOW_KEY_5H: Final[str] = "5h"
WINDOW_KEY_7D: Final[str] = "7d"

#: Tier -> capacity ratio. A window fraction is *relative to that account's own budget*,
#: so a 0.4 slack on a max_5x account is worth a quarter of a 0.4 slack on a max_20x one.
TIER_CAPACITY: Final[Mapping[str, float]] = MappingProxyType(
    {
        TIER_MAX_20X: 1.0,
        TIER_MAX_5X: 0.25,
        TIER_PRO: 1.0,
        TIER_UNKNOWN: UNKNOWN_TIER_CAPACITY,
    }
)

#: Accepted spellings for a tier, including the raw ``organizationRateLimitTier`` values
#: found in ``~/.claude*/.claude.json`` (the only place the tier is observable -- the
#: usage endpoint does not report it).
_TIER_ALIASES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "default_claude_max_20x": TIER_MAX_20X,
        "claude_max_20x": TIER_MAX_20X,
        "max_20x": TIER_MAX_20X,
        "max20x": TIER_MAX_20X,
        "20x": TIER_MAX_20X,
        "default_claude_max_5x": TIER_MAX_5X,
        "claude_max_5x": TIER_MAX_5X,
        "max_5x": TIER_MAX_5X,
        "max5x": TIER_MAX_5X,
        "5x": TIER_MAX_5X,
        "default_pro": TIER_PRO,
        "claude_pro": TIER_PRO,
        "pro": TIER_PRO,
        "": TIER_UNKNOWN,
        "unknown": TIER_UNKNOWN,
        "none": TIER_UNKNOWN,
        "null": TIER_UNKNOWN,
    }
)


def normalize_tier(raw: Any) -> str:
    """Map any known spelling of a tier onto a canonical ``TIER_*`` constant.

    An unrecognized tier is a legitimate degraded state (a new subscription level, a
    config file we could not read), so this returns :data:`TIER_UNKNOWN` rather than
    raising. Callers that care should lower the snapshot's confidence.
    """
    if raw is None:
        return TIER_UNKNOWN
    key = str(raw).strip().casefold().replace("-", "_").replace(" ", "_")
    return _TIER_ALIASES.get(key, TIER_UNKNOWN)


def capacity_for_tier(tier: Any) -> float:
    """Capacity ratio for a tier, accepting canonical or raw spellings."""
    return TIER_CAPACITY.get(normalize_tier(tier), UNKNOWN_TIER_CAPACITY)


# ======================================================================================
# Model classes
# ======================================================================================

#: The one model class the oracle currently scopes windows by. It arrives as the
#: ``scoped[].name`` field (``"Fable"``) and is normalized to lower case here.
MODEL_CLASS_FABLE: Final[str] = "fable"


def normalize_model_class(name: Any) -> str | None:
    """Normalize a model-class label (``"Fable"`` -> ``"fable"``).

    Returns ``None`` for ``None``/blank input, meaning "the caller did not say which
    model class this invocation targets".
    """
    if name is None:
        return None
    text = str(name).strip().casefold()
    return text or None


# ======================================================================================
# Provenance and confidence
# ======================================================================================

#: A live read of the vendor usage endpoint using the account's existing access
#: token. Highest trust: current server-side truth, and obtained without minting
#: a credential (see providers/claude_oauth.py).
SOURCE_LIVE: Final[str] = "live"
SOURCE_CLAUDE_JSON: Final[str] = "claude_json"
SOURCE_MANUAL: Final[str] = "manual"
SOURCE_CACHE: Final[str] = "cache"
SOURCE_ASSUMED: Final[str] = "assumed"
SOURCE_UNKNOWN: Final[str] = "unknown"

#: Word -> number, so a source layer may hand :class:`AccountSnapshot` either
#: ``confidence=0.6`` or ``confidence="medium"``. Storage is always the float.
CONFIDENCE_LABELS: Final[Mapping[str, float]] = MappingProxyType(
    {
        "high": 1.0,
        "medium": 0.6,
        "low": 0.3,
        "none": 0.0,
        "unknown": 0.0,
    }
)

#: Confidence assumed when a snapshot does not state one (a live, fresh oracle read).
DEFAULT_CONFIDENCE: Final[float] = 1.0


def confidence_label(confidence: float) -> str:
    """Bucket a numeric confidence into ``high`` / ``medium`` / ``low`` / ``none``."""
    if confidence >= 0.85:
        return "high"
    if confidence >= 0.5:
        return "medium"
    if confidence > 0.0:
        return "low"
    return "none"


def _as_confidence(value: Any) -> float:
    """Coerce a confidence given as a float **or** a label into a float in ``[0, 1]``."""
    if value is None:
        return DEFAULT_CONFIDENCE
    if isinstance(value, str):
        key = value.strip().casefold()
        if key in CONFIDENCE_LABELS:
            return CONFIDENCE_LABELS[key]
        raise ValueError(
            f"confidence label must be one of {sorted(CONFIDENCE_LABELS)}, got {value!r}"
        )
    return _as_fraction(value, "confidence")


# ======================================================================================
# Regimes
# ======================================================================================

#: Surplus regime: at least one candidate has ``min_slack > 0``.
REGIME_A: Final[str] = "A"
#: Scarcity regime: every candidate has ``min_slack <= 0``.
REGIME_B: Final[str] = "B"

_REGIME_ALIASES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "a": REGIME_A,
        "regime_a": REGIME_A,
        "surplus": REGIME_A,
        "b": REGIME_B,
        "regime_b": REGIME_B,
        "scarcity": REGIME_B,
        "deficit": REGIME_B,
    }
)


def normalize_regime(value: Any) -> str | None:
    """Normalize a regime label to :data:`REGIME_A` / :data:`REGIME_B` or ``None``.

    ``None`` and the empty string mean "no regime was reached" (for example when there
    were no eligible candidates at all). Anything else unrecognized raises, because a
    silently mislabelled regime hides the exact bug the two-regime split exists to fix.
    """
    if value is None:
        return None
    key = str(value).strip().casefold().replace("-", "_").replace(" ", "_")
    if not key:
        return None
    try:
        return _REGIME_ALIASES[key]
    except KeyError:
        raise ValueError(
            f"regime must be {REGIME_A!r} (surplus) or {REGIME_B!r} (scarcity), got {value!r}"
        ) from None


# ======================================================================================
# Identity
# ======================================================================================


@dataclass(frozen=True, slots=True)
class Identity:
    """Who an account actually *is*, independent of where its config happens to live.

    Accounts are keyed by identity -- ``(email, organizationUuid)`` -- and never by
    config directory. A directory is a mutable local detail: the operator can point
    ``CLAUDE_CONFIG_DIR`` anywhere, re-login into a different account in the same
    directory, or shuffle account numbers; none of that may change which quota pool a
    routing decision refers to.
    """

    email: str
    organization_uuid: str = ""
    organization_name: str | None = None
    #: The oracle's positional ``number`` for this account. Display/debug only -- it is
    #: NOT part of :attr:`key` because it is reassigned when accounts are added/removed.
    account_number: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "email", _as_text(self.email, "Identity.email"))
        object.__setattr__(
            self,
            "organization_uuid",
            _as_text(
                self.organization_uuid, "Identity.organization_uuid", allow_empty=True
            ),
        )
        if self.organization_name is not None:
            object.__setattr__(
                self,
                "organization_name",
                _as_text(
                    self.organization_name,
                    "Identity.organization_name",
                    allow_empty=True,
                )
                or None,
            )
        if self.account_number is not None:
            object.__setattr__(self, "account_number", int(self.account_number))

    @property
    def key(self) -> tuple[str, str]:
        """Case-insensitive identity key, safe to use as a dict key."""
        return (self.email.casefold(), self.organization_uuid.casefold())

    def matches(self, other: Identity | None) -> bool:
        """Return ``True`` when ``other`` denotes the same account."""
        return other is not None and self.key == other.key

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Identity:
        """Build an :class:`Identity` from an oracle account object.

        Accepts both the oracle's camelCase keys (``email``, ``organizationUuid``,
        ``organizationName``, ``number``) and snake_case equivalents.
        """
        email = data.get("email")
        org_uuid = data.get("organizationUuid", data.get("organization_uuid", ""))
        org_name = data.get("organizationName", data.get("organization_name"))
        number = data.get("number", data.get("account_number"))
        return cls(
            email=email if isinstance(email, str) else "",
            organization_uuid=org_uuid if isinstance(org_uuid, str) else "",
            organization_name=org_name if isinstance(org_name, str) else None,
            account_number=number if isinstance(number, int) else None,
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready mapping (plain types only)."""
        return {
            "email": self.email,
            "organization_uuid": self.organization_uuid,
            "organization_name": self.organization_name,
            "account_number": self.account_number,
        }


# ======================================================================================
# Window
# ======================================================================================


@dataclass(frozen=True, slots=True)
class Window:
    """One rate-limit window of one account (5-hour, 7-day, or a model-scoped window).

    All fractions are of that window's own budget, in ``[0, 1]``. Note that the oracle
    reports ``pct`` as **used** percent, so ``used_fraction = pct / 100`` and
    ``remaining_fraction = 1 - pct / 100``.

    Args:
        key: Stable identifier for the window, e.g. ``"five_hour"``, ``"seven_day"``,
            ``"scoped:fable"``. Used as :attr:`ScoreBreakdown.binding_window`.
        used_fraction: Fraction of the window's budget already consumed.
        length_s: Nominal window length in seconds (5h -> 18000, 7d -> 604800). Drives
            the uniform pacing prior ``burn_rate = 1 / length_s``.
        resets_at_s: Epoch seconds at which the window rolls over.
        observed_at_s: Epoch seconds at which these numbers were read, for staleness.
        applies_to: Model classes this window governs, e.g. ``frozenset({"fable"})`` for
            the oracle's ``scoped[]`` entries. ``None`` means the window applies to every
            model class (the account-wide 5-hour and 7-day windows). An empty collection
            is treated as ``None``: "applies to nothing" would silently *remove* a
            constraint and make an account look better than it is.
        expected_used_fraction: The upstream pacing baseline (``expectedPct / 100``) when
            the oracle publishes one. Used verbatim; when absent the baseline is derived
            from ``resets_at_s`` and ``length_s`` (the 5-hour window has no
            ``expectedPct``).
    """

    key: str
    used_fraction: float
    length_s: float
    resets_at_s: float
    observed_at_s: float
    applies_to: frozenset[str] | None = None
    expected_used_fraction: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _as_text(self.key, "Window.key"))
        object.__setattr__(
            self, "used_fraction", _as_fraction(self.used_fraction, "Window.used_fraction")
        )
        length_s = _as_finite(self.length_s, "Window.length_s")
        if length_s <= 0.0:
            raise ValueError(f"Window.length_s must be > 0, got {length_s!r}")
        object.__setattr__(self, "length_s", length_s)
        object.__setattr__(
            self, "resets_at_s", _as_finite(self.resets_at_s, "Window.resets_at_s")
        )
        object.__setattr__(
            self, "observed_at_s", _as_finite(self.observed_at_s, "Window.observed_at_s")
        )

        if self.applies_to is not None:
            if isinstance(self.applies_to, str):
                raw: Iterable[Any] = (self.applies_to,)
            else:
                raw = self.applies_to
            classes = {
                normalized
                for normalized in (normalize_model_class(item) for item in raw)
                if normalized is not None
            }
            # Empty == unconstrained (see the class docstring).
            object.__setattr__(self, "applies_to", frozenset(classes) or None)

        if self.expected_used_fraction is not None:
            object.__setattr__(
                self,
                "expected_used_fraction",
                _as_fraction(self.expected_used_fraction, "Window.expected_used_fraction"),
            )

    # -- geometry ----------------------------------------------------------------------

    @property
    def remaining_fraction(self) -> float:
        """Fraction of this window's budget still available."""
        return _clamp01(1.0 - self.used_fraction)

    @property
    def is_exhausted(self) -> bool:
        """``True`` when nothing is left in this window."""
        return self.remaining_fraction <= 0.0

    @property
    def burn_rate_prior(self) -> float:
        """Uniform pacing prior, as a fraction of budget per second."""
        return 1.0 / self.length_s

    def time_to_reset_s(self, now_s: float) -> float:
        """Seconds until the window rolls over; never negative."""
        return max(0.0, self.resets_at_s - now_s)

    def elapsed_fraction(self, now_s: float) -> float:
        """Fraction of the window that has already elapsed, clamped to ``[0, 1]``.

        Derived from the reset time rather than a start time because that is what the
        oracle publishes. Clamping keeps stale or inconsistent data (a reset further out
        than the window is long) from producing nonsense outside ``[0, 1]``.
        """
        return _clamp01(1.0 - self.time_to_reset_s(now_s) / self.length_s)

    # -- the pacing model --------------------------------------------------------------

    def expected_used_at(self, now_s: float) -> float:
        """The pacing baseline: fraction a perfectly-even spender would have used by now.

        Uses the upstream :attr:`expected_used_fraction` when present, otherwise the
        uniform prior derived from :meth:`elapsed_fraction`.
        """
        if self.expected_used_fraction is not None:
            return self.expected_used_fraction
        return self.elapsed_fraction(now_s)

    def expected_demand(self, now_s: float) -> float:
        """Fraction of budget still expected to burn before reset.

        ``burn_rate * time_to_reset`` under the uniform prior, i.e. ``1 - expected_used``.
        """
        return _clamp01(1.0 - self.expected_used_at(now_s))

    def slack(self, now_s: float) -> float:
        """``remaining_fraction - expected_demand``: surplus (>0) or deficit (<=0).

        Positive slack is quota that will expire unused unless it gets spent; negative
        slack means this window is already running ahead of pace. Equivalent to
        ``expected_used_fraction - used_fraction`` whenever the oracle supplies a
        baseline -- do **not** trust the oracle's own ``aheadOfPace`` boolean, which is
        self-inconsistent upstream; derive the sign from these numbers instead.
        """
        return self.remaining_fraction - self.expected_demand(now_s)

    def ahead_of_pace(self, now_s: float) -> bool:
        """``True`` when this window has burned more than the pacing baseline."""
        return self.slack(now_s) < 0.0

    # -- model-class gating ------------------------------------------------------------

    def applies(self, model_class: str | None) -> bool:
        """Does this window constrain a call for ``model_class``?

        ``applies_to is None`` -> the window is account-wide and always applies. A caller
        that does not name a model class gets ``True`` as well: an unspecified request
        could land on any class, so no constraint may be dropped.
        """
        if self.applies_to is None:
            return True
        normalized = normalize_model_class(model_class)
        if normalized is None:
            return True
        return normalized in self.applies_to

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready mapping (frozensets become sorted lists)."""
        return {
            "key": self.key,
            "used_fraction": self.used_fraction,
            "length_s": self.length_s,
            "resets_at_s": self.resets_at_s,
            "observed_at_s": self.observed_at_s,
            "applies_to": None if self.applies_to is None else sorted(self.applies_to),
            "expected_used_fraction": self.expected_used_fraction,
        }


# ======================================================================================
# AccountSnapshot
# ======================================================================================


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Everything the router knows about one spendable account at one instant.

    Args:
        id: One of :data:`ACCOUNT_IDS`. This is the wire format -- the TypeScript
            consumer maps these 1:1 with no translation table. Non-canonical ids are
            accepted (tests, future accounts) but :func:`is_canonical_account_id` is the
            check that matters.
        provider: One of :data:`PROVIDERS`. Derived from :attr:`id` when left blank.
        windows: The account's rate-limit windows. Order is preserved and is the
            tie-break order when two windows are equally binding.
        tier: Subscription tier; raw ``organizationRateLimitTier`` strings are accepted
            and normalized (see :func:`normalize_tier`).
        source: Where this snapshot came from (``SOURCE_*``), for debugging degraded runs.
        confidence: ``0..1`` trust in these numbers, or a label from
            :data:`CONFIDENCE_LABELS`. Stale cache reads and guessed tiers should lower it.
        available: ``False`` marks an account that must not be selected at all (logged
            out, config missing, operator-disabled). Distinct from being out of quota,
            which is expressed by the windows themselves.
        note: Free-text explanation, surfaced in ``--json`` output.
        identity: The account's real identity. Accounts key by identity, never by config
            directory; ``None`` means the source could not determine it.
    """

    id: str
    provider: str = ""
    windows: tuple[Window, ...] = ()
    tier: str = TIER_UNKNOWN
    source: str = SOURCE_UNKNOWN
    confidence: float = DEFAULT_CONFIDENCE
    available: bool = True
    note: str | None = None
    identity: Identity | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _as_text(self.id, "AccountSnapshot.id"))

        provider = _as_text(self.provider, "AccountSnapshot.provider", allow_empty=True)
        object.__setattr__(self, "provider", provider or provider_for_account_id(self.id))

        windows = self.windows
        if isinstance(windows, Window):
            windows = (windows,)
        object.__setattr__(self, "windows", tuple(windows))

        object.__setattr__(self, "tier", normalize_tier(self.tier))
        object.__setattr__(
            self,
            "source",
            _as_text(self.source, "AccountSnapshot.source", allow_empty=True)
            or SOURCE_UNKNOWN,
        )
        object.__setattr__(self, "confidence", _as_confidence(self.confidence))
        object.__setattr__(self, "available", bool(self.available))
        if self.note is not None:
            object.__setattr__(
                self,
                "note",
                _as_text(self.note, "AccountSnapshot.note", allow_empty=True) or None,
            )
        if self.identity is not None and not isinstance(self.identity, Identity):
            raise TypeError(
                f"AccountSnapshot.identity must be an Identity or None, got {self.identity!r}"
            )

    # -- derived ------------------------------------------------------------------------

    @property
    def capacity(self) -> float:
        """Tier capacity ratio for this account (see :data:`TIER_CAPACITY`)."""
        return capacity_for_tier(self.tier)

    @property
    def observed_at_s(self) -> float | None:
        """Most recent observation time across windows, or ``None`` if there are none."""
        if not self.windows:
            return None
        return max(window.observed_at_s for window in self.windows)

    def staleness_s(self, now_s: float) -> float | None:
        """Age of the freshest window reading in seconds, or ``None`` without windows."""
        observed = self.observed_at_s
        if observed is None:
            return None
        return max(0.0, now_s - observed)

    def window(self, key: str) -> Window | None:
        """Look a window up by :attr:`Window.key`."""
        for candidate in self.windows:
            if candidate.key == key:
                return candidate
        return None

    def applicable_windows(self, model_class: str | None = None) -> tuple[Window, ...]:
        """Windows that constrain a call for ``model_class`` (order preserved)."""
        return tuple(window for window in self.windows if window.applies(model_class))

    def slacks(
        self, now_s: float, model_class: str | None = None
    ) -> tuple[WindowSlack, ...]:
        """Per-window breakdown, with the binding (minimum-slack) window flagged.

        Non-applicable windows are included with ``applicable=False`` so the CLI can show
        *why* a window was skipped, but they never affect :meth:`min_slack`.
        """
        rows = tuple(
            WindowSlack.from_window(window, now_s, model_class) for window in self.windows
        )
        binding_index = _argmin_slack(rows)
        if binding_index is None:
            return rows
        return tuple(
            row.with_binding(index == binding_index) for index, row in enumerate(rows)
        )

    def min_slack(self, now_s: float, model_class: str | None = None) -> float | None:
        """Minimum slack across applicable windows; ``None`` when none apply.

        The tightest window throttles the others, so the *minimum* -- not the mean, not
        the account-wide window alone -- is what a decision may be based on.
        """
        applicable = self.applicable_windows(model_class)
        if not applicable:
            return None
        return min(window.slack(now_s) for window in applicable)

    def binding_window_key(
        self, now_s: float, model_class: str | None = None
    ) -> str | None:
        """Key of the window that produced :meth:`min_slack` (first wins on ties)."""
        best_key: str | None = None
        best_slack = math.inf
        for window in self.windows:
            if not window.applies(model_class):
                continue
            slack = window.slack(now_s)
            if slack < best_slack:
                best_slack = slack
                best_key = window.key
        return best_key

    def min_remaining_fraction(self, model_class: str | None = None) -> float | None:
        """Minimum remaining budget across applicable windows; ``None`` when none apply.

        This is the regime-B objective: under scarcity the question is no longer "whose
        quota is about to expire" but "who can actually serve the call".
        """
        applicable = self.applicable_windows(model_class)
        if not applicable:
            return None
        return min(window.remaining_fraction for window in applicable)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready mapping (plain types only)."""
        return {
            "id": self.id,
            "provider": self.provider,
            "windows": [window.to_dict() for window in self.windows],
            "tier": self.tier,
            "capacity": self.capacity,
            "source": self.source,
            "confidence": self.confidence,
            "available": self.available,
            "note": self.note,
            "identity": None if self.identity is None else self.identity.to_dict(),
        }


def unreadable_reason(snapshot: AccountSnapshot) -> str | None:
    """Why nothing could be read for this account -- ``None`` when it *was* read.

    A failed usage read and an empty measurement are the same shape and opposite
    facts. The OAuth provider reports a failure as ``windows=()``, ``available=False``,
    ``confidence=0.0`` and a ``note`` naming the cause ("access token expired"), but it
    still labels ``source`` as ``live`` -- that field describes the attempt, not the
    outcome -- so ``note`` plus ``available`` is the only honest signal downstream.

    Observed live: an account with 53% of its Fable quota left went dark on an expired
    token, was dropped from the candidate set before eligibility ever ran, and appeared
    in neither ``ranked`` nor ``excluded`` nor ``degraded``. The router then picked an
    account that was 97% spent, and the only trace was a warning about a missing Fable
    window -- which is a data-shape complaint, not the truth. An unread account's
    remaining quota is UNKNOWN: not zero, not full, and never silently absent.

    ``confidence`` alone cannot be the test: the Antigravity pools publish no windows at
    ``confidence=0.0`` by design and are perfectly routable, so ``available=False`` with
    nothing measured is what separates "we could not look" from "there is nothing to
    look at".

    It lives HERE, in the value layer, rather than beside either caller. The CLI's policy
    pass and the pure layer's eligibility pass are separate filters that share exactly
    this one judgment, and when it lived in ``cli.py`` the fix landed in the CLI and was
    left live in ``select.select`` -- which is public, takes no ``Config``, and is what a
    direct library caller reaches. A judgment two passes both make belongs to neither.
    """
    if snapshot.windows or snapshot.available:
        return None
    detail = (snapshot.note or "").strip() or "the source gave no cause"
    return (
        "unreadable: no usage reading was obtained for this account, so its remaining "
        f"quota is unknown rather than free -- {detail}"
    )


# ======================================================================================
# Scoring records
# ======================================================================================


@dataclass(frozen=True, slots=True)
class WindowSlack:
    """One row of :attr:`ScoreBreakdown.per_window`: how one window scored.

    Pure explanation -- nothing reads these back to make a decision, they exist so a
    human (or the ``--json`` consumer) can see exactly which window bound the score and
    what its numbers were.
    """

    key: str
    applicable: bool = True
    remaining_fraction: float = 0.0
    expected_used_fraction: float = 0.0
    slack: float = 0.0
    time_to_reset_s: float = 0.0
    length_s: float = 0.0
    #: ``True`` on the window whose slack became the account's ``min_slack``.
    binding: bool = False
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _as_text(self.key, "WindowSlack.key"))
        object.__setattr__(self, "applicable", bool(self.applicable))
        object.__setattr__(
            self,
            "remaining_fraction",
            _as_finite(self.remaining_fraction, "WindowSlack.remaining_fraction"),
        )
        object.__setattr__(
            self,
            "expected_used_fraction",
            _as_finite(self.expected_used_fraction, "WindowSlack.expected_used_fraction"),
        )
        object.__setattr__(self, "slack", _as_finite(self.slack, "WindowSlack.slack"))
        object.__setattr__(
            self,
            "time_to_reset_s",
            _as_finite(self.time_to_reset_s, "WindowSlack.time_to_reset_s"),
        )
        object.__setattr__(
            self, "length_s", _as_finite(self.length_s, "WindowSlack.length_s")
        )
        object.__setattr__(self, "binding", bool(self.binding))
        object.__setattr__(
            self, "reason", _as_text(self.reason, "WindowSlack.reason", allow_empty=True)
        )

    @classmethod
    def from_window(
        cls,
        window: Window,
        now_s: float,
        model_class: str | None = None,
        *,
        binding: bool = False,
    ) -> WindowSlack:
        """Evaluate ``window`` at ``now_s`` for ``model_class``."""
        applicable = window.applies(model_class)
        reason = (
            ""
            if applicable
            else f"skipped: does not apply to model class {normalize_model_class(model_class)!r}"
        )
        return cls(
            key=window.key,
            applicable=applicable,
            remaining_fraction=window.remaining_fraction,
            expected_used_fraction=window.expected_used_at(now_s),
            slack=window.slack(now_s),
            time_to_reset_s=window.time_to_reset_s(now_s),
            length_s=window.length_s,
            binding=binding and applicable,
            reason=reason,
        )

    def with_binding(self, binding: bool) -> WindowSlack:
        """Return a copy with :attr:`binding` set (frozen dataclasses don't mutate)."""
        if binding == self.binding:
            return self
        return WindowSlack(
            key=self.key,
            applicable=self.applicable,
            remaining_fraction=self.remaining_fraction,
            expected_used_fraction=self.expected_used_fraction,
            slack=self.slack,
            time_to_reset_s=self.time_to_reset_s,
            length_s=self.length_s,
            binding=binding,
            reason=self.reason,
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready mapping (plain types only)."""
        return {
            "key": self.key,
            "applicable": self.applicable,
            "remaining_fraction": self.remaining_fraction,
            "expected_used_fraction": self.expected_used_fraction,
            "slack": self.slack,
            "time_to_reset_s": self.time_to_reset_s,
            "length_s": self.length_s,
            "binding": self.binding,
            "reason": self.reason,
        }


def _argmin_slack(rows: tuple[WindowSlack, ...]) -> int | None:
    """Index of the applicable row with the smallest slack; ``None`` if there is none."""
    best_index: int | None = None
    best_slack = math.inf
    for index, row in enumerate(rows):
        if not row.applicable:
            continue
        if row.slack < best_slack:
            best_slack = row.slack
            best_index = index
    return best_index


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """Why one account scored the way it did. One per candidate, eligible or not.

    Args:
        account_id: The candidate's :attr:`AccountSnapshot.id`.
        score: The regime's objective, already scaled:
            ``provider_weight * capacity * min_slack`` in regime A,
            ``provider_weight * capacity * min_remaining`` in regime B.
        min_slack: Minimum slack across applicable windows, **unscaled**. This is the
            quantity a hysteresis / switch margin must be compared against -- never
            :attr:`score`, because an additive epsilon on a capacity-scaled score is 4x
            stricter for a ``max_5x`` account than for a ``max_20x`` one.
        binding_window: :attr:`Window.key` of the window that produced ``min_slack``.
        regime: :data:`REGIME_A` or :data:`REGIME_B` (``None`` if scoring never ran).
        capacity: Tier capacity ratio applied.
        provider_weight: Policy weight applied for the account's provider.
        fits: Whether the account can actually serve the call right now (has remaining
            budget in every applicable window). Independent of slack: an account can be
            far behind pace and still fit.
        per_window: Tuple of :class:`WindowSlack`, one per window, in snapshot order.
        eligible: Whether this candidate was allowed to win. Ineligible candidates carry
            a populated :attr:`reason` and live in :attr:`Decision.excluded`.
        reason: Human-readable explanation of the score or the exclusion.
        min_remaining: The regime-B objective before scaling, recorded for auditability.
            Optional so positional construction stays compatible with the field list above.
    """

    account_id: str
    score: float = 0.0
    min_slack: float = 0.0
    binding_window: str | None = None
    regime: str | None = None
    capacity: float = 1.0
    provider_weight: float = 1.0
    fits: bool = True
    per_window: tuple[WindowSlack, ...] = ()
    eligible: bool = True
    reason: str = ""
    min_remaining: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "account_id", _as_text(self.account_id, "ScoreBreakdown.account_id")
        )
        object.__setattr__(self, "score", _as_finite(self.score, "ScoreBreakdown.score"))
        object.__setattr__(
            self, "min_slack", _as_finite(self.min_slack, "ScoreBreakdown.min_slack")
        )
        if self.binding_window is not None:
            object.__setattr__(
                self,
                "binding_window",
                _as_text(
                    self.binding_window, "ScoreBreakdown.binding_window", allow_empty=True
                )
                or None,
            )
        object.__setattr__(self, "regime", normalize_regime(self.regime))
        object.__setattr__(
            self, "capacity", _as_finite(self.capacity, "ScoreBreakdown.capacity")
        )
        object.__setattr__(
            self,
            "provider_weight",
            _as_finite(self.provider_weight, "ScoreBreakdown.provider_weight"),
        )
        object.__setattr__(self, "fits", bool(self.fits))
        object.__setattr__(self, "per_window", tuple(self.per_window))
        object.__setattr__(self, "eligible", bool(self.eligible))
        object.__setattr__(
            self, "reason", _as_text(self.reason, "ScoreBreakdown.reason", allow_empty=True)
        )
        if self.min_remaining is not None:
            object.__setattr__(
                self,
                "min_remaining",
                _as_finite(self.min_remaining, "ScoreBreakdown.min_remaining"),
            )

    @property
    def binding(self) -> WindowSlack | None:
        """The :class:`WindowSlack` row flagged as binding, if any."""
        for row in self.per_window:
            if row.binding:
                return row
        return None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready mapping (plain types only)."""
        return {
            "account_id": self.account_id,
            "score": self.score,
            "min_slack": self.min_slack,
            "min_remaining": self.min_remaining,
            "binding_window": self.binding_window,
            "regime": self.regime,
            "capacity": self.capacity,
            "provider_weight": self.provider_weight,
            "fits": self.fits,
            "per_window": [row.to_dict() for row in self.per_window],
            "eligible": self.eligible,
            "reason": self.reason,
        }


# ======================================================================================
# Decision
# ======================================================================================


@dataclass(frozen=True, slots=True)
class Decision:
    """The router's answer: which account to spend from, and the full audit trail.

    Args:
        chosen: The winning :attr:`AccountSnapshot.id`, or ``None`` when nothing was
            eligible. For convenience a :class:`ScoreBreakdown` may be passed instead and
            is reduced to its ``account_id``.
        ranked: Eligible candidates, best first. ``ranked[0].account_id == chosen``
            whenever a choice was made.
        excluded: Candidates that could not win, each carrying its
            :attr:`ScoreBreakdown.reason`.
        reason: One-line explanation of the decision, suitable for a log line.
        regime: Which regime decided it -- :data:`REGIME_A` (surplus) or :data:`REGIME_B`
            (scarcity); ``None`` when no candidate was scorable.
        sticky_applied: ``True`` when hysteresis kept the previously-used account even
            though another account scored higher (by less than the switch margin).
        degraded: ``True`` when the decision was made on incomplete or stale data (oracle
            unavailable, cached snapshot, unknown tier). The decision is still valid --
            the router always answers -- but the caller may want to say so out loud.
        warnings: Human-readable notes about anything degraded or surprising.
    """

    chosen: str | None = None
    ranked: tuple[ScoreBreakdown, ...] = ()
    excluded: tuple[ScoreBreakdown, ...] = ()
    reason: str = ""
    regime: str | None = None
    sticky_applied: bool = False
    degraded: bool = False
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        chosen = self.chosen
        if chosen is not None and not isinstance(chosen, str):
            # Convenience: accept the winning ScoreBreakdown itself.
            account_id = getattr(chosen, "account_id", None)
            if account_id is None:
                raise TypeError(
                    f"Decision.chosen must be an account id string, a ScoreBreakdown, "
                    f"or None; got {chosen!r}"
                )
            chosen = account_id
        if chosen is not None:
            chosen = _as_text(chosen, "Decision.chosen", allow_empty=True) or None
        object.__setattr__(self, "chosen", chosen)

        object.__setattr__(self, "ranked", tuple(self.ranked))
        object.__setattr__(self, "excluded", tuple(self.excluded))
        object.__setattr__(
            self, "reason", _as_text(self.reason, "Decision.reason", allow_empty=True)
        )
        object.__setattr__(self, "regime", normalize_regime(self.regime))
        object.__setattr__(self, "sticky_applied", bool(self.sticky_applied))
        object.__setattr__(self, "degraded", bool(self.degraded))
        object.__setattr__(
            self,
            "warnings",
            tuple(
                _as_text(warning, "Decision.warnings[]", allow_empty=True)
                for warning in self.warnings
            ),
        )

    @property
    def has_choice(self) -> bool:
        """``True`` when an account was selected."""
        return self.chosen is not None

    @property
    def chosen_breakdown(self) -> ScoreBreakdown | None:
        """The winner's :class:`ScoreBreakdown`, looked up in :attr:`ranked`."""
        if self.chosen is None:
            return None
        for row in self.ranked:
            if row.account_id == self.chosen:
                return row
        return None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready mapping, stamped with the contract version.

        This is the payload the TypeScript consumer reads; it must refuse anything whose
        ``contract_version`` it does not recognize.
        """
        return {
            "contract_version": QUOTA_ROUTER_CONTRACT_VERSION,
            "chosen": self.chosen,
            "regime": self.regime,
            "reason": self.reason,
            "sticky_applied": self.sticky_applied,
            "degraded": self.degraded,
            "warnings": list(self.warnings),
            "ranked": [row.to_dict() for row in self.ranked],
            "excluded": [row.to_dict() for row in self.excluded],
        }
