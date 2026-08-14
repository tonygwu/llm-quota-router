"""Failure-text classification tests.

These are the highest-stakes assertions in the package. Each of the three buckets has a
production incident behind it:

* reading ``"Not logged in · Please run /login"`` as exhaustion benched a healthy account
  and produced ~44 spurious failures,
* falling back to "now" on an unparseable reset would un-bench a spent pool immediately
  and burn the next call on a guaranteed failure,
* widening a model-class limit to the whole account would stop serving classes that are
  still perfectly routable.

Every case pins ``now_s`` explicitly, and expected deadlines are computed here from
:mod:`zoneinfo` directly rather than by re-running the code under test.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from quota_router.failure_text import (
    EXHAUSTED_WITH_DEADLINE,
    EXHAUSTED_WITHOUT_DEADLINE,
    SCOPE_ACCOUNT,
    SCOPE_MODEL_CLASS,
    TRANSIENT,
    UNKNOWN,
    classify_cli_json,
    classify_failure_text,
    classify_output,
    extract_failure_text,
    next_occurrence_epoch,
    parse_relative_duration_s,
)

LA = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc

#: 2026-08-14 10:00 Pacific -- a plain, non-DST-boundary afternoon.
NOW = datetime(2026, 8, 14, 10, 0, tzinfo=LA).timestamp()

SESSION_LIMIT = "You've hit your session limit · resets 1pm (America/Los_Angeles)"
FABLE_LIMIT = "You've reached your Fable 5 limit"
LOGIN_RACE = "Not logged in · Please run /login"
AGY_QUOTA = "Individual quota reached. Your quota will refresh. Resets in 25m54s"


# ======================================================================================
# Bucket 1: exhausted, deadline known
# ======================================================================================


def test_session_limit_resolves_to_the_next_future_wall_clock_time() -> None:
    result = classify_failure_text(SESSION_LIMIT, NOW)

    assert result.kind == EXHAUSTED_WITH_DEADLINE
    assert result.scope == SCOPE_ACCOUNT
    assert result.blocks_account is True
    assert result.is_transient is False
    assert result.exhausted_until_s == datetime(2026, 8, 14, 13, 0, tzinfo=LA).timestamp()


def test_a_time_already_past_today_rolls_over_to_tomorrow() -> None:
    """No date is given, so 1pm seen at 2pm means tomorrow -- the common case, not an edge."""
    afternoon = datetime(2026, 8, 14, 14, 0, tzinfo=LA).timestamp()
    result = classify_failure_text(SESSION_LIMIT, afternoon)

    assert result.exhausted_until_s == datetime(2026, 8, 15, 13, 0, tzinfo=LA).timestamp()
    assert result.exhausted_until_s > afternoon


def test_dst_fall_back_picks_the_first_of_the_two_ambiguous_instants() -> None:
    """1:30am happens twice on 2026-11-01; the earlier one is the next future occurrence."""
    now = datetime(2026, 11, 1, 0, 30, tzinfo=LA, fold=0).timestamp()
    text = "You've hit your session limit · resets 1:30am (America/Los_Angeles)"

    result = classify_failure_text(text, now)

    first = datetime(2026, 11, 1, 1, 30, tzinfo=LA, fold=0).timestamp()
    second = datetime(2026, 11, 1, 1, 30, tzinfo=LA, fold=1).timestamp()
    assert second - first == 3600  # the hour really is ambiguous
    assert result.exhausted_until_s == first
    assert result.exhausted_until_s - now == pytest.approx(3600)


def test_dst_spring_forward_still_produces_a_sane_future_instant() -> None:
    """2:30am does not exist on 2027-03-14; the earliest consistent instant is used."""
    now = datetime(2027, 3, 14, 1, 0, tzinfo=LA, fold=0).timestamp()
    text = "You've hit your session limit · resets 2:30am (America/Los_Angeles)"

    result = classify_failure_text(text, now)

    assert result.exhausted_until_s is not None
    assert result.exhausted_until_s == datetime(2027, 3, 14, 2, 30, tzinfo=LA, fold=1).timestamp()
    assert 0 < result.exhausted_until_s - now <= 2 * 3600


def test_relative_reset_from_the_antigravity_cli() -> None:
    result = classify_failure_text(AGY_QUOTA, NOW)

    assert result.kind == EXHAUSTED_WITH_DEADLINE
    # Exact, not approx: at epoch magnitudes pytest's default *relative* tolerance is
    # ~1800 seconds, which would happily accept a deadline half an hour off.
    assert result.exhausted_until_s == NOW + 25 * 60 + 54


def test_twenty_four_hour_clock_and_abbreviated_zones() -> None:
    utc_result = classify_failure_text("You've hit your usage limit · resets at 9:00 (UTC)", NOW)
    assert utc_result.exhausted_until_s == datetime(2026, 8, 15, 9, 0, tzinfo=UTC).timestamp()

    pt_result = classify_failure_text("You've hit your usage limit, resets at 11:30 PT", NOW)
    assert pt_result.exhausted_until_s == datetime(2026, 8, 14, 11, 30, tzinfo=LA).timestamp()


def test_midnight_and_noon_meridiems() -> None:
    midnight = classify_failure_text(
        "You've hit your session limit · resets 12am (America/Los_Angeles)", NOW
    )
    noon = classify_failure_text(
        "You've hit your session limit · resets 12pm (America/Los_Angeles)", NOW
    )
    assert midnight.exhausted_until_s == datetime(2026, 8, 15, 0, 0, tzinfo=LA).timestamp()
    assert noon.exhausted_until_s == datetime(2026, 8, 14, 12, 0, tzinfo=LA).timestamp()


@pytest.mark.parametrize(
    "text",
    [
        "You've hit your session limit · resets whenever it feels like it",
        "You've hit your session limit · resets 1pm (Mars/Olympus_Mons)",
        "You've hit your session limit · resets 1pm",  # no zone, and no default given
        "You've hit your session limit · resets in a little while",
        "You've hit your session limit · resets 99:99 (America/Los_Angeles)",
    ],
)
def test_an_unparseable_reset_yields_an_unknown_deadline_never_now(text: str) -> None:
    """The cardinal rule: a parse failure must never resolve to ``now``."""
    result = classify_failure_text(text, NOW)

    assert result.kind == EXHAUSTED_WITH_DEADLINE
    assert result.exhausted_until_s is None
    assert "UNKNOWN" in result.reason


def test_a_default_timezone_is_used_only_when_the_message_omits_one() -> None:
    without_zone = "You've hit your session limit · resets 1pm"
    assert classify_failure_text(without_zone, NOW).exhausted_until_s is None

    with_default = classify_failure_text(without_zone, NOW, default_tz="America/Los_Angeles")
    assert with_default.exhausted_until_s == datetime(2026, 8, 14, 13, 0, tzinfo=LA).timestamp()

    # An explicit zone in the text still wins over the default. NOW is 17:00 UTC, so
    # 13:00 UTC has already passed today and the next occurrence is tomorrow -- which is
    # also a second check that the zone really was read as UTC and not as Pacific.
    explicit = classify_failure_text(
        "You've hit your session limit · resets 1pm (UTC)", NOW, default_tz="America/Los_Angeles"
    )
    assert explicit.exhausted_until_s == datetime(2026, 8, 15, 13, 0, tzinfo=UTC).timestamp()


# ======================================================================================
# Bucket 2: exhausted, no deadline (model-class scoped)
# ======================================================================================


def test_model_class_limit_carries_no_deadline_and_does_not_bench_the_account() -> None:
    result = classify_failure_text(FABLE_LIMIT, NOW)

    assert result.kind == EXHAUSTED_WITHOUT_DEADLINE
    assert result.exhausted_until_s is None
    assert result.scope == SCOPE_MODEL_CLASS
    assert result.model_class == "fable"  # matches the oracle's scoped window key
    assert result.model_label == "Fable 5"  # echoed exactly as the provider wrote it
    assert result.is_exhaustion is True
    assert result.blocks_account is False  # other classes stay routable


def test_a_scoped_limit_that_does_carry_a_reset_keeps_both_facts() -> None:
    result = classify_failure_text(f"{FABLE_LIMIT} · resets 1pm (America/Los_Angeles)", NOW)

    assert result.kind == EXHAUSTED_WITH_DEADLINE
    assert result.scope == SCOPE_MODEL_CLASS
    assert result.model_class == "fable"
    assert result.blocks_account is False
    assert result.exhausted_until_s == datetime(2026, 8, 14, 13, 0, tzinfo=LA).timestamp()


def test_account_wide_wording_is_not_mistaken_for_a_model_class() -> None:
    for text in (
        "You've reached your usage limit",
        "You've hit your weekly limit",
        "You have exceeded your account limit",
    ):
        result = classify_failure_text(text, NOW)
        assert result.kind == EXHAUSTED_WITHOUT_DEADLINE, text
        assert result.scope == SCOPE_ACCOUNT, text
        assert result.model_class is None, text
        assert result.blocks_account is True, text


# ======================================================================================
# Bucket 3: transient (never exhaustion)
# ======================================================================================


def test_the_login_race_is_transient() -> None:
    """~44 spurious production failures came from calling this one exhaustion."""
    result = classify_failure_text(LOGIN_RACE, NOW)

    assert result.kind == TRANSIENT
    assert result.is_exhaustion is False
    assert result.blocks_account is False
    assert result.exhausted_until_s is None


def test_the_login_race_wins_even_when_the_text_also_carries_a_429() -> None:
    """It has arrived alongside a 429 in production; the login check must run first."""
    noisy = "API Error (429 rate_limit_error): Not logged in · Please run /login"
    assert classify_failure_text(noisy, NOW).kind == TRANSIENT


@pytest.mark.parametrize(
    "text",
    [
        'API Error: 429 {"type":"rate_limit_error","message":"Number of requests"}',
        "Error: rate limit exceeded, please retry",
        "overloaded_error",
        "upstream connect error: 503 service unavailable",
        "fetch failed (ECONNRESET)",
        "You have reached your rate limit",
    ],
)
def test_throttles_and_transport_errors_are_never_exhaustion(text: str) -> None:
    result = classify_failure_text(text, NOW)
    assert result.kind == TRANSIENT, result
    assert result.exhausted_until_s is None


# ======================================================================================
# Unknown
# ======================================================================================


@pytest.mark.parametrize("text", ["", "   ", None, "segmentation fault", "unexpected token <"])
def test_unrecognized_text_is_unknown_rather_than_guessed(text: str | None) -> None:
    result = classify_failure_text(text, NOW)

    assert result.kind == UNKNOWN
    assert result.is_exhaustion is False
    assert result.is_transient is False
    assert result.exhausted_until_s is None


# ======================================================================================
# JSON envelopes (claude -p --output-format json)
# ======================================================================================


def test_limit_text_is_read_from_result_not_stderr() -> None:
    """Under ``--output-format json`` the limit message lives in ``.result``."""
    payload = {
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "duration_ms": 812,
        "result": SESSION_LIMIT,
        "api_error_status": 429,
        "session_id": "abc-123",
    }

    assert extract_failure_text(payload) == SESSION_LIMIT
    result = classify_cli_json(payload, NOW)
    assert result.kind == EXHAUSTED_WITH_DEADLINE
    assert result.exhausted_until_s == datetime(2026, 8, 14, 13, 0, tzinfo=LA).timestamp()


def test_a_successful_result_is_not_mined_for_failure_text() -> None:
    payload = {"type": "result", "is_error": False, "result": "Sure, here is your answer."}
    assert extract_failure_text(payload) is None
    assert classify_cli_json(payload, NOW).kind == UNKNOWN


def test_a_bare_429_envelope_is_transient() -> None:
    payload = {"is_error": True, "result": "something went sideways", "api_error_status": 429}
    result = classify_cli_json(payload, NOW)

    assert result.kind == TRANSIENT
    assert result.matched == "transient:status"


def test_a_429_envelope_whose_text_is_the_login_race_is_still_transient() -> None:
    payload = {"is_error": True, "result": LOGIN_RACE, "api_error_status": 429}
    assert classify_cli_json(payload, NOW).matched.startswith("login_race")


def test_a_429_envelope_that_names_a_real_limit_is_exhaustion() -> None:
    payload = {"is_error": True, "result": FABLE_LIMIT, "api_error_status": 429}
    result = classify_cli_json(payload, NOW)

    assert result.kind == EXHAUSTED_WITHOUT_DEADLINE
    assert result.model_class == "fable"


def test_nested_error_objects_contribute_their_type_and_message() -> None:
    payload = {"error": {"type": "rate_limit_error", "message": "Too many requests"}}
    text = extract_failure_text(payload)

    assert text is not None and "rate_limit_error" in text
    assert classify_cli_json(payload, NOW).kind == TRANSIENT


def test_classify_output_accepts_mappings_json_strings_and_raw_text() -> None:
    payload = {"is_error": True, "result": SESSION_LIMIT, "api_error_status": 429}

    from_mapping = classify_output(payload, NOW)
    from_json_text = classify_output(json.dumps(payload), NOW)
    from_raw_text = classify_output(SESSION_LIMIT, NOW)
    from_bytes = classify_output(SESSION_LIMIT.encode("utf-8"), NOW)

    assert from_mapping.exhausted_until_s == from_json_text.exhausted_until_s
    assert from_raw_text.exhausted_until_s == from_mapping.exhausted_until_s
    assert from_bytes.exhausted_until_s == from_mapping.exhausted_until_s
    assert classify_output(object(), NOW).kind == UNKNOWN


# ======================================================================================
# Normalization and small helpers
# ======================================================================================


def test_case_smart_quotes_and_separators_do_not_change_the_verdict() -> None:
    variants = [
        SESSION_LIMIT,
        SESSION_LIMIT.upper(),
        "You’ve hit your session limit · resets 1pm (America/Los_Angeles)",
        "You've hit your  session   limit\n- resets 1pm (America/Los_Angeles)",
    ]
    deadlines = {classify_failure_text(text, NOW).exhausted_until_s for text in variants}
    assert deadlines == {datetime(2026, 8, 14, 13, 0, tzinfo=LA).timestamp()}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("25m54s", 25 * 60 + 54),
        ("1h 5m", 3900),
        ("90s", 90),
        ("2 hours", 7200),
        ("45 minutes", 2700),
        ("1d 3h", 97200),
        ("soon", None),
        ("", None),
    ],
)
def test_relative_duration_parsing(text: str, expected: float | None) -> None:
    assert parse_relative_duration_s(text) == expected


def test_next_occurrence_rejects_an_impossible_clock_time() -> None:
    assert next_occurrence_epoch(25, 0, LA, NOW) is None
    assert next_occurrence_epoch(13, 61, LA, NOW) is None


def test_no_classification_ever_reports_a_deadline_of_now() -> None:
    """A blanket guard against the "fall back to now" bug in every code path."""
    corpus = [
        SESSION_LIMIT,
        FABLE_LIMIT,
        LOGIN_RACE,
        AGY_QUOTA,
        "You've hit your session limit",
        "You've hit your session limit · resets soon",
        "You've hit your session limit · resets at ??",
        "quota exceeded",
        "429",
        "",
    ]
    for text in corpus:
        result = classify_failure_text(text, NOW)
        assert result.exhausted_until_s is None or result.exhausted_until_s > NOW, text


def test_to_dict_is_json_serializable() -> None:
    payload = classify_failure_text(SESSION_LIMIT, NOW).to_dict()
    assert json.loads(json.dumps(payload))["kind"] == EXHAUSTED_WITH_DEADLINE
