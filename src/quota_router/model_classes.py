"""Model strings in, *model classes* out.

Two jobs, both pure:

1. **Classification.** Turn a real model string the caller actually passes to a vendor
   CLI (``claude-opus-4-8``, ``claude-fable-5[1m]``, ``gpt-5.4``) into a model *class*
   (``opus``, ``fable``, ``gpt``). The class is what gates routing: the oracle publishes
   per-model-class windows in ``usage.scoped[]`` (``name: "Fable"``), which become a
   :class:`~quota_router.types.Window` with ``applies_to={"fable"}``. A window whose
   ``applies_to`` excludes the requested class is skipped when computing ``min_slack``.

2. **Demand weighting.** Turn a class into a relative demand multiplier, so that a haiku
   call does not reserve as much of a pool as an opus call does (see
   :mod:`quota_router.state` -- the multiplier scales the pileup reservation cost).

Why an unrecognized model resolves to ``None``
----------------------------------------------
``None`` means "the caller did not say which class this is", and
:meth:`quota_router.types.Window.applies` treats that as *every window applies*. That is
the conservative direction: a wrong guess that returns a concrete class would **drop** a
real constraint (skip the Fable window for a Fable call) and make an exhausted account
look spendable. Guessing wrong must never remove a limit, so we decline to guess.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from .types import MODEL_CLASS_FABLE, normalize_model_class

__all__ = [
    "CLASS_OPUS",
    "CLASS_SONNET",
    "CLASS_HAIKU",
    "CLASS_FABLE",
    "CLASS_GPT",
    "CLASS_GEMINI",
    "KNOWN_MODEL_CLASSES",
    "DEFAULT_PATTERNS",
    "DEFAULT_MULTIPLIERS",
    "DEFAULT_MULTIPLIER",
    "ModelResolution",
    "classify",
    "demand_multiplier",
    "is_class_name",
    "resolve",
    "sorted_patterns",
]


# ======================================================================================
# The classes
# ======================================================================================

CLASS_OPUS: Final[str] = "opus"
CLASS_SONNET: Final[str] = "sonnet"
CLASS_HAIKU: Final[str] = "haiku"
#: Re-exported from :mod:`quota_router.types` so callers have one place to import from.
#: This is the class the oracle actually scopes windows by today.
CLASS_FABLE: Final[str] = MODEL_CLASS_FABLE
CLASS_GPT: Final[str] = "gpt"
CLASS_GEMINI: Final[str] = "gemini"

#: Classes the router understands out of the box. Config may add more (any key in
#: ``[model_classes.multipliers]`` or a value in ``[model_classes.patterns]`` counts).
KNOWN_MODEL_CLASSES: Final[tuple[str, ...]] = (
    CLASS_OPUS,
    CLASS_SONNET,
    CLASS_HAIKU,
    CLASS_FABLE,
    CLASS_GPT,
    CLASS_GEMINI,
)

#: Glob -> class. Substring globs on purpose: real model ids arrive with vendor prefixes
#: and suffixes (``us.anthropic.claude-opus-4-8-v1:0``, ``claude-fable-5[1m]``,
#: ``claude-3-5-sonnet-20241022``) and the family name is the only stable part.
DEFAULT_PATTERNS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "*fable*": CLASS_FABLE,
        "*opus*": CLASS_OPUS,
        "*sonnet*": CLASS_SONNET,
        "*haiku*": CLASS_HAIKU,
        "gpt*": CLASS_GPT,
        "*gpt-*": CLASS_GPT,
        "o3*": CLASS_GPT,
        "o4*": CLASS_GPT,
        "codex*": CLASS_GPT,
        "*gemini*": CLASS_GEMINI,
    }
)

#: Class -> relative demand multiplier. Opus and Fable are the expensive classes and are
#: the pacing baseline (1.0); sonnet and haiku consume a fraction of the same budget.
DEFAULT_MULTIPLIERS: Final[Mapping[str, float]] = MappingProxyType(
    {
        CLASS_OPUS: 1.00,
        CLASS_SONNET: 0.46,
        CLASS_HAIKU: 0.01,
        CLASS_FABLE: 1.00,
    }
)

#: Multiplier for a class nobody configured. Deliberately the *expensive* end: an unknown
#: model that turns out to be cheap merely over-reserves for ``pileup.window_s`` seconds,
#: whereas an unknown model assumed cheap lets concurrent callers pile onto one pool.
DEFAULT_MULTIPLIER: Final[float] = 1.0

#: Trailing bracket qualifiers on a model id -- ``claude-fable-5[1m]`` names the 1M-token
#: context variant of the same model, which is the same quota class.
_BRACKET_SUFFIX: Final[re.Pattern[str]] = re.compile(r"\[[^\]]*\]")


# ======================================================================================
# Resolution
# ======================================================================================


@dataclass(frozen=True, slots=True)
class ModelResolution:
    """What ``--model`` turned into, and how.

    Args:
        raw: Exactly what the caller passed (for messages).
        model_class: The resolved class, or ``None`` when nothing matched. ``None`` means
            "unspecified", which keeps *every* window applicable.
        source: ``"class"`` (the caller named a class), ``"pattern"`` (a glob matched a
            real model string), ``"none"`` (no ``--model`` at all) or ``"unknown"`` (a
            model string that matched nothing).
        pattern: The glob that matched, when ``source == "pattern"``.
        multiplier: Demand multiplier for :attr:`model_class`.
    """

    raw: str | None = None
    model_class: str | None = None
    source: str = "none"
    pattern: str | None = None
    multiplier: float = DEFAULT_MULTIPLIER

    @property
    def matched(self) -> bool:
        """``True`` when the input resolved to a concrete class."""
        return self.model_class is not None

    @property
    def warning(self) -> str | None:
        """A caller-facing warning when a model string could not be classified."""
        if self.source != "unknown":
            return None
        return (
            f"model {self.raw!r} matched no model-class pattern; treating the request as "
            f"class-agnostic (every window applies, no constraint dropped)"
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready mapping (plain types only)."""
        return {
            "raw": self.raw,
            "model_class": self.model_class,
            "source": self.source,
            "pattern": self.pattern,
            "multiplier": self.multiplier,
        }


def _variants(model: str) -> tuple[str, ...]:
    """Candidate spellings of ``model`` to match patterns against, most literal first."""
    text = model.strip().casefold()
    out = [text]
    stripped = _BRACKET_SUFFIX.sub("", text).strip()
    if stripped and stripped != text:
        out.append(stripped)
    # ``us.anthropic.claude-opus-4-8-v1:0`` -> ``claude-opus-4-8-v1:0``: a leading vendor
    # namespace should not stop an anchored pattern like ``gpt*`` from matching.
    tail = text.rsplit(".", 1)[-1] if "." in text else ""
    if tail and tail not in out:
        out.append(tail)
    tail_stripped = _BRACKET_SUFFIX.sub("", tail).strip() if tail else ""
    if tail_stripped and tail_stripped not in out:
        out.append(tail_stripped)
    return tuple(out)


def _specificity(pattern: str) -> tuple[int, int, str]:
    """Sort key making the most *literal* pattern win, deterministically.

    ``"claude-opus-4-8"`` beats ``"*opus*"`` beats ``"*"``. Ties fall back to length and
    then alphabetical order so two equally specific patterns always resolve the same way
    -- a router that changes its mind between runs is worse than one that is wrong.
    """
    literal = sum(1 for char in pattern if char not in "*?[]")
    return (-literal, -len(pattern), pattern)


def sorted_patterns(patterns: Mapping[str, str] | None = None) -> tuple[tuple[str, str], ...]:
    """``(glob, class)`` pairs in match order (most specific first)."""
    table = DEFAULT_PATTERNS if patterns is None else patterns
    return tuple(sorted(table.items(), key=lambda item: _specificity(item[0])))


def classify(
    model: Any,
    patterns: Mapping[str, str] | None = None,
    *,
    default: str | None = None,
) -> str | None:
    """Map a real model string onto a model class.

    Args:
        model: A model id such as ``"claude-fable-5[1m]"``. ``None``/blank -> ``default``.
        patterns: Glob -> class table; :data:`DEFAULT_PATTERNS` when omitted.
        default: Returned when nothing matches (``None`` = "unspecified", the safe value).

    Returns:
        The normalized class name, or ``default``.
    """
    if model is None:
        return default
    text = str(model).strip()
    if not text:
        return default
    ordered = sorted_patterns(patterns)
    for variant in _variants(text):
        for pattern, class_name in ordered:
            if fnmatch.fnmatchcase(variant, pattern.strip().casefold()):
                return normalize_model_class(class_name)
    return default


def is_class_name(
    value: Any,
    multipliers: Mapping[str, float] | None = None,
    patterns: Mapping[str, str] | None = None,
) -> bool:
    """Is ``value`` already a model *class* (rather than a model id)?

    A class is anything the router knows by that name: a builtin class, a key of the
    configured multiplier table, or a value in the configured pattern table. This is what
    lets ``--model`` accept both ``claude-opus-4-8`` and ``opus``.
    """
    normalized = normalize_model_class(value)
    if normalized is None:
        return False
    if normalized in KNOWN_MODEL_CLASSES:
        return True
    if multipliers and normalized in {normalize_model_class(k) for k in multipliers}:
        return True
    table = DEFAULT_PATTERNS if patterns is None else patterns
    return normalized in {normalize_model_class(v) for v in table.values()}


def demand_multiplier(
    model_class: Any,
    multipliers: Mapping[str, float] | None = None,
    *,
    default: float = DEFAULT_MULTIPLIER,
) -> float:
    """Relative demand of one call of ``model_class`` (see :data:`DEFAULT_MULTIPLIERS`)."""
    table = DEFAULT_MULTIPLIERS if multipliers is None else multipliers
    normalized = normalize_model_class(model_class)
    if normalized is None:
        return default
    for key, value in table.items():
        if normalize_model_class(key) == normalized:
            try:
                return float(value)
            except (TypeError, ValueError):
                return default
    return default


def resolve(
    value: Any,
    *,
    patterns: Mapping[str, str] | None = None,
    multipliers: Mapping[str, float] | None = None,
) -> ModelResolution:
    """Resolve a ``--model`` argument that may be a model id **or** a class name.

    Order matters: an exact class name wins before glob matching, so ``--model fable``
    means the Fable class even though ``"*fable*"`` would also match it, and a bare
    ``--model opus`` never has to round-trip through a fake model id.
    """
    if value is None:
        return ModelResolution(
            raw=None,
            model_class=None,
            source="none",
            multiplier=demand_multiplier(None, multipliers),
        )

    raw = str(value).strip()
    if not raw:
        return ModelResolution(
            raw=raw or None,
            model_class=None,
            source="none",
            multiplier=demand_multiplier(None, multipliers),
        )

    if is_class_name(raw, multipliers, patterns):
        model_class = normalize_model_class(raw)
        return ModelResolution(
            raw=raw,
            model_class=model_class,
            source="class",
            multiplier=demand_multiplier(model_class, multipliers),
        )

    ordered = sorted_patterns(patterns)
    for variant in _variants(raw):
        for pattern, class_name in ordered:
            if fnmatch.fnmatchcase(variant, pattern.strip().casefold()):
                model_class = normalize_model_class(class_name)
                return ModelResolution(
                    raw=raw,
                    model_class=model_class,
                    source="pattern",
                    pattern=pattern,
                    multiplier=demand_multiplier(model_class, multipliers),
                )

    return ModelResolution(
        raw=raw,
        model_class=None,
        source="unknown",
        multiplier=demand_multiplier(None, multipliers),
    )


def known_classes(
    multipliers: Mapping[str, float] | None = None,
    patterns: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Every class name the router currently recognizes, sorted, for help text."""
    names: set[str] = set(KNOWN_MODEL_CLASSES)
    for source in (multipliers or {}, {v: 0 for v in (patterns or DEFAULT_PATTERNS).values()}):
        for key in source:
            normalized = normalize_model_class(key)
            if normalized:
                names.add(normalized)
    return tuple(sorted(names))

