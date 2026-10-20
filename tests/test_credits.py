"""API credit pools: config, ledger, status. ``pick`` must not change.

A credit pool is prepaid API credit in one vendor org (an Anthropic Console org linked
to one Max plan). No vendor publishes the remaining promotional balance, so the router
keeps a ledger: a grant or an operator checkpoint, minus spend it booked, minus holds
for calls in flight. What must hold:

* Config is fail-loud: a key that looks like a secret, an unknown linked account, a pool
  id that could be read as a Claude account, any card on file, or a
  missing ``usd_per_pse`` is a ConfigError, never a default.
* The balance arithmetic: checkpoint, spends, holds that age out, an exhaustion signal
  that zeroes the pool until the next grant, and monthly renewal that stops when the
  linked plan ends. Credits already granted outlive the plan (Anthropic: "Credits you
  already have stay usable until they expire").
* ``status`` shows the pools; ``pick`` output never mentions them.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from quota_router import cli, credits
from quota_router.config import ConfigError, load_config
from tests.test_cli import NOW, _NO_CONFIG_FILES, real_capture, run

HOUR = 3600.0
DAY = 86400.0

POOL = """
[engine]
usd_per_pse = 305

[credit_pools.anthropic_api_d]
provider = "anthropic_api"
keychain_service = "anthropic-api-key-claude-d"
linked_account = "claude_d"
grant_usd = 200
expires = "2026-10-19T00:00:00Z"
renewal = "monthly"
card_on_file = false
"""


def _load(tmp_path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return load_config(
        env={"XDG_CONFIG_HOME": _NO_CONFIG_FILES}, cwd=_NO_CONFIG_FILES, explicit_path=path
    )


def test_a_pool_parses(tmp_path) -> None:
    pool = _load(tmp_path, POOL).credit_pools["anthropic_api_d"]
    assert pool.provider == "anthropic_api"
    assert pool.keychain_service == "anthropic-api-key-claude-d"
    assert pool.linked_account == "claude_d"
    assert pool.grant_usd == 200.0
    assert pool.renewal == "monthly"
    assert pool.card_on_file is False
    assert pool.admission_margin == 1.5
    assert pool.max_usd_per_call == 10.0


@pytest.mark.parametrize(
    "old, new, fragment",
    [
        ('keychain_service = "anthropic-api-key-claude-d"',
         'keychain_service = "sk-ant-api03-abcdef"', "looks like a key"),
        ('linked_account = "claude_d"', 'linked_account = "claude_z"', "claude_z"),
        ('provider = "anthropic_api"', 'provider = "openai_api"', "provider"),
        ('renewal = "monthly"', 'renewal = "weekly"', "renewal"),
        ("card_on_file = false", "card_on_file = true", "card_on_file = true is not supported"),
        ("grant_usd = 200", "grant_usd = 0", "grant_usd"),
        ('expires = "2026-10-19T00:00:00Z"', 'expires = "2026-10-19T00:00:00"', "zone"),
        ("[credit_pools.anthropic_api_d]", "[credit_pools.claude_api]", "claude"),
        ("usd_per_pse = 305", "", "usd_per_pse"),
        ("card_on_file = false\n", "", "card_on_file"),
    ],
)
def test_bad_pool_config_is_refused(tmp_path, old, new, fragment) -> None:
    assert old in POOL
    with pytest.raises(ConfigError, match=fragment):
        _load(tmp_path, POOL.replace(old, new))


def test_a_pool_id_may_not_collide_with_an_account(tmp_path) -> None:
    body = POOL.replace("[credit_pools.anthropic_api_d]", "[credit_pools.cursor]")
    with pytest.raises(ConfigError, match="cursor"):
        _load(tmp_path, body)


def test_a_card_on_file_is_refused_even_with_a_hard_cap(tmp_path) -> None:
    """No code enforces a spending cap, so a card-on-file pool could spend past its
    credits onto the card. The router supports promotional credit only (operator
    decision 2026-10-18), and a ``hard_cap_usd`` no longer makes a card acceptable.
    """
    body = POOL.replace("card_on_file = false", "card_on_file = true\nhard_cap_usd = 50")
    with pytest.raises(ConfigError, match="card_on_file = true is not supported"):
        _load(tmp_path, body)


def test_a_card_on_file_is_refused_on_a_disabled_pool_too(tmp_path) -> None:
    """Enabling the pool later must not be the step that admits a card."""
    body = POOL.replace("card_on_file = false", "card_on_file = true\nenabled = false")
    with pytest.raises(ConfigError, match="card_on_file = true is not supported"):
        _load(tmp_path, body)


def test_a_hard_cap_without_a_card_is_an_unknown_key(tmp_path) -> None:
    """``hard_cap_usd`` is gone; a config that still sets it hears so and loads."""
    body = POOL.replace("card_on_file = false", "card_on_file = false\nhard_cap_usd = 50")
    config = _load(tmp_path, body)
    assert "anthropic_api_d" in config.credit_pools
    assert any(
        "unknown key [credit_pools.anthropic_api_d] hard_cap_usd ignored" in w
        for w in config.warnings
    ), config.warnings
    assert not hasattr(config.credit_pools["anthropic_api_d"], "hard_cap_usd")


# ======================================================================================
# The ledger
# ======================================================================================

EXPIRES = 1_792_368_000.0  # 2026-10-19T00:00:00Z


def _balance(tmp_path, lines, now, *, plan_end=None):
    pool = _load(tmp_path, POOL).credit_pools["anthropic_api_d"]
    return credits.balance(pool, lines, now_s=now, plan_end_s=plan_end)


def _line(t, kind, **fields):
    return {"v": 1, "t": t, "pool": "anthropic_api_d", "type": kind, **fields}


def test_with_no_ledger_the_config_grant_applies_and_says_so(tmp_path) -> None:
    b = _balance(tmp_path, [], EXPIRES - 3 * DAY)
    assert b.remaining_usd == 200.0
    assert b.source == "config"
    assert b.confidence == 0.5
    assert "credits set" in b.note


def test_a_checkpoint_then_spends_and_holds(tmp_path) -> None:
    t0 = EXPIRES - 3 * DAY
    lines = [
        _line(t0, "checkpoint", remaining_usd=180.0),
        _line(t0 + 10, "spend", run_id="r1", usd=2.5),
        _line(t0 + 20, "hold", run_id="r2", usd=4.0),
        _line(t0 + 30, "hold", run_id="r3", usd=1.0),
        _line(t0 + 40, "spend", run_id="r3", usd=0.75),
        _line(t0 - 50, "spend", run_id="old", usd=99.0),
    ]
    b = _balance(tmp_path, lines, t0 + 60)
    # 180 - 2.5 spent - 4.0 held (r2 open) - 0.75 spent (r3 settled, its hold gone).
    assert b.remaining_usd == pytest.approx(180 - 2.5 - 4.0 - 0.75)
    assert b.held_usd == pytest.approx(4.0)
    assert b.source == "operator"
    later = _balance(tmp_path, lines, t0 + 2 * HOUR)
    assert later.held_usd == 0.0
    assert later.remaining_usd == pytest.approx(180 - 2.5 - 0.75)


def test_an_exhaustion_signal_zeroes_the_pool_until_the_next_grant(tmp_path) -> None:
    t0 = EXPIRES - 3 * DAY
    lines = [_line(t0, "checkpoint", remaining_usd=50.0), _line(t0 + 10, "exhausted",
                                                                signal="credit balance is too low")]
    b = _balance(tmp_path, lines, t0 + 20)
    assert b.remaining_usd == 0.0 and b.exhausted and not b.available
    lines.append(_line(t0 + 30, "grant", usd=200.0, expires_s=EXPIRES + 31 * DAY))
    assert _balance(tmp_path, lines, t0 + 40).remaining_usd == 200.0


def test_after_expiry_a_monthly_pool_assumes_a_new_grant(tmp_path) -> None:
    lines = [_line(EXPIRES - 3 * DAY, "checkpoint", remaining_usd=12.0)]
    b = _balance(tmp_path, lines, EXPIRES + HOUR)
    assert b.remaining_usd == 200.0
    assert b.source == "assumed"
    assert b.confidence == 0.6
    assert b.expires_s == 1_795_046_400.0  # 2026-11-19T00:00:00Z: one calendar month on


def test_no_renewal_is_assumed_after_the_linked_plan_ends(tmp_path) -> None:
    plan_end = EXPIRES - 3 * DAY
    lines = [_line(EXPIRES - 4 * DAY, "checkpoint", remaining_usd=150.0)]
    # Credits already granted stay usable until their own expiry after the plan ends...
    still = _balance(tmp_path, lines, EXPIRES - DAY, plan_end=plan_end)
    assert still.remaining_usd == 150.0 and still.available
    # ...and nothing new arrives after it.
    gone = _balance(tmp_path, lines, EXPIRES + HOUR, plan_end=plan_end)
    assert gone.remaining_usd == 0.0 and gone.expired and not gone.available


def test_the_ledger_appends_and_reads_back(tmp_path) -> None:
    env = {"HOME": str(tmp_path / "home"), "XDG_STATE_HOME": str(tmp_path / "state")}
    credits.append(env, _line(NOW, "checkpoint", remaining_usd=199.99, expires_s=EXPIRES))
    credits.append(env, _line(NOW + 1, "spend", run_id="r1", usd=0.01))
    lines, problems = credits.read(env)
    assert [line["type"] for line in lines] == ["checkpoint", "spend"]
    assert problems == ()
    assert credits.ledger_path(env).parent == tmp_path / "state" / "quota-router"


def test_an_unreadable_ledger_line_is_reported(tmp_path) -> None:
    env = {"HOME": str(tmp_path / "home"), "XDG_STATE_HOME": str(tmp_path / "state")}
    path = credits.ledger_path(env)
    path.parent.mkdir(parents=True)
    path.write_text('{"v":1,"t":1,"pool":"x","type":"spend","usd":1}\nnot json\n')
    lines, problems = credits.read(env)
    assert len(lines) == 1
    assert problems and "line 2" in problems[0]


# ======================================================================================
# The CLI
# ======================================================================================


@pytest.fixture()
def env(tmp_path):
    xdg = tmp_path / "config" / "quota-router"
    xdg.mkdir(parents=True)
    (xdg / "config.toml").write_text(POOL, encoding="utf-8")
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PATH": "/usr/bin:/bin",
    }


def test_credits_set_records_a_checkpoint_and_credits_shows_it(env) -> None:
    code, out, err = run(
        ["credits", "set", "anthropic_api_d", "--remaining", "199.99",
         "--expires", "2026-10-19T00:00:00Z"],
        env, cwd=_NO_CONFIG_FILES,
    )
    assert code == cli.EXIT_OK, err
    code, out, err = run(["credits", "--json"], env, cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_OK, err
    pool = json.loads(out)["credit_pools"][0]
    assert pool["id"] == "anthropic_api_d"
    assert pool["remaining_usd"] == pytest.approx(199.99)
    assert pool["remaining_pse"] == pytest.approx(199.99 / 305)
    assert pool["source"] == "operator"
    assert pool["expires"] == "2026-10-19T00:00:00Z"


def test_credits_refuses_an_unknown_pool(env) -> None:
    code, _out, err = run(["credits", "set", "nope", "--remaining", "1"], env,
                          cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_USAGE
    assert "nope" in err


def test_status_shows_the_pools(env) -> None:
    code, out, err = run(["status", "--json"], env, snapshots=real_capture(),
                         cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_OK, err
    assert json.loads(out)["credit_pools"][0]["id"] == "anthropic_api_d"
    code, out, err = run(["status"], env, snapshots=real_capture(), cwd=_NO_CONFIG_FILES)
    assert "anthropic_api_d" in out
    assert "quotapick run" in out


@pytest.mark.parametrize(
    "zone, stamp",
    [
        ("America/Los_Angeles", "Sun 2026-10-18 17:00 PDT (65.2d)"),
        ("UTC", "Mon 2026-10-19 00:00 UTC (65.2d)"),
        (":Asia/Tokyo", "Mon 2026-10-19 09:00 JST (65.2d)"),
    ],
)
def test_the_tables_print_expires_on_the_readers_clock(env, zone, stamp) -> None:
    # The Console date 2026-10-19 was recorded as 00:00Z: 5pm the day before in
    # California. Read as "the 19th" it overstates the time left by up to a day.
    env = {**env, "TZ": zone}
    code, out, err = run(["credits"], env, cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_OK, err
    assert stamp in out
    assert "2026-10-19T00:00:00Z" not in out
    code, out, err = run(["status"], env, snapshots=real_capture(), cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_OK, err
    assert stamp in out


def test_json_keeps_expires_in_utc_whatever_the_zone(env) -> None:
    env = {**env, "TZ": "America/Los_Angeles"}
    code, out, err = run(["credits", "--json"], env, cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_OK, err
    assert json.loads(out)["credit_pools"][0]["expires"] == "2026-10-19T00:00:00Z"
    code, out, err = run(["status", "--json"], env, snapshots=real_capture(),
                         cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_OK, err
    assert json.loads(out)["credit_pools"][0]["expires"] == "2026-10-19T00:00:00Z"


def test_an_expiry_past_a_daylight_saving_change_gets_its_own_offset(env) -> None:
    # California leaves PDT on 2026-11-01. An offset taken from "now" (PDT in August)
    # would print 17:00 PDT here, an hour off.
    code, out, err = run(
        ["credits", "set", "anthropic_api_d", "--remaining", "200",
         "--expires", "2026-11-16T00:00:00Z"],
        {**env, "TZ": "America/Los_Angeles"}, cwd=_NO_CONFIG_FILES,
    )
    assert code == cli.EXIT_OK, err
    assert "Sun 2026-11-15 16:00 PST" in out


@pytest.mark.parametrize(
    "machine_zone, expires, stamp",
    [
        ("Asia/Tokyo", "2026-10-19T00:00:00Z", "Mon 2026-10-19 09:00 JST (65.2d)"),
        # PDT now, PST at the expiry: the machine's zone is also read per instant.
        ("America/Los_Angeles", "2026-11-16T00:00:00Z", "Sun 2026-11-15 16:00 PST (93.2d)"),
    ],
)
def test_with_no_tz_the_table_uses_this_machines_zone(
    env, monkeypatch, machine_zone, expires, stamp
) -> None:
    # macOS sets no TZ; the zone comes from /etc/localtime through the C library. The
    # process zone stands in for it here, and is restored before the next test.
    assert "TZ" not in env
    monkeypatch.setenv("TZ", machine_zone)
    time.tzset()
    try:
        code, out, err = run(
            ["credits", "set", "anthropic_api_d", "--remaining", "200", "--expires", expires],
            env, cwd=_NO_CONFIG_FILES,
        )
    finally:
        monkeypatch.undo()
        time.tzset()
    assert code == cli.EXIT_OK, err
    assert stamp in out


def test_a_tz_that_names_no_zone_prints_utc_and_says_so(env) -> None:
    code, out, err = run(["credits"], {**env, "TZ": "Mars/Olympus_Mons"},
                         cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_OK, err
    assert "Mon 2026-10-19 00:00 UTC (65.2d)" in out
    assert "TZ='Mars/Olympus_Mons' is not an IANA zone name, so times print in UTC" in err


def test_pick_never_mentions_a_pool(env) -> None:
    code, out, err = run(["pick", "--dry-run", "--json"], env, snapshots=real_capture(),
                         cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_OK, err
    assert "anthropic_api_d" not in out
    assert "credit" not in json.loads(out)


@pytest.mark.parametrize(
    "start, end",
    [
        ("2026-10-19T00:00:00+00:00", "2026-11-19T00:00:00+00:00"),
        ("2026-01-31T12:00:00+00:00", "2026-02-28T12:00:00+00:00"),
        ("2028-01-31T00:00:00+00:00", "2028-02-29T00:00:00+00:00"),
        ("2026-12-15T00:00:00+00:00", "2027-01-15T00:00:00+00:00"),
    ],
)
def test_add_month_keeps_the_day_and_clamps_at_month_end(start, end) -> None:
    from datetime import datetime

    got = credits.add_month(datetime.fromisoformat(start).timestamp())
    assert got == datetime.fromisoformat(end).timestamp()


# ======================================================================================
# Expiry and doctor (U9)
# ======================================================================================


def test_status_records_what_a_pool_had_left_when_it_expired(env) -> None:
    expires = 1_792_368_000.0  # the pool's configured expiry, 2026-10-19T00:00:00Z
    credits.append(env, {"t": expires - 3 * DAY, "pool": "anthropic_api_d",
                         "type": "checkpoint", "remaining_usd": 150.0})
    credits.append(env, {"t": expires - 2 * DAY, "pool": "anthropic_api_d",
                         "type": "spend", "run_id": "r1", "usd": 10.0})
    for _ in range(2):  # twice: the record is written once
        code, _out, err = run(["status", "--json"], env, snapshots=real_capture(),
                              now_s=expires + HOUR, cwd=_NO_CONFIG_FILES)
        assert code == cli.EXIT_OK, err
    expired = [line for line in credits.read(env)[0] if line["type"] == "expired"]
    assert len(expired) == 1
    assert expired[0]["expires_s"] == expires
    assert expired[0]["remaining_usd"] == pytest.approx(140.0)
    assert expired[0]["remaining_pse"] == pytest.approx(140.0 / 305)
    code, out, _err = run(["credits", "--json"], env, now_s=expires + HOUR, cwd=_NO_CONFIG_FILES)
    pool = json.loads(out)["credit_pools"][0]
    assert pool["expired_unused_usd"] == pytest.approx(140.0)


def _state_bytes(env) -> dict[str, bytes]:
    """Every data file under the state folder. Lock files hold no data and are skipped."""
    root = Path(env["XDG_STATE_HOME"])
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and not p.name.endswith(".lock")}


def test_status_dry_run_records_nothing(env) -> None:
    """``--dry-run`` promises no writes, and agents add it to keep a checkout off live
    state. ``status`` used to ignore it and append history, reset rows, waiting
    records, ledger encodings, credit expiries and a forecast (BACKLOG #35)."""
    from quota_router import calibrations
    from tests.test_calibrations import T0, measurement

    expires = 1_792_368_000.0  # the pool's configured expiry, 2026-10-19T00:00:00Z
    credits.append(env, {"t": expires - 3 * DAY, "pool": "anthropic_api_d",
                         "type": "checkpoint", "remaining_usd": 150.0})
    # A reconstructed ledger with no encodings: a recording status appends every one.
    calibrations.append_records([measurement()], env=env)
    calibrations.mark_reconstruction_complete(
        env=env, now_s=calibrations.parse_time(T0), reason="Fixture reconstruction")
    before = _state_bytes(env)
    for argv in (["status", "--dry-run"], ["status", "--dry-run", "--json"]):
        code, out, err = run(argv, env, snapshots=real_capture(), now_s=expires + HOUR,
                             cwd=_NO_CONFIG_FILES)
        assert code == cli.EXIT_OK, err
        assert "claude_c" in out
    assert _state_bytes(env) == before
    # Without the flag the same call writes, so the assertion above can fail.
    code, _out, err = run(["status", "--json"], env, snapshots=real_capture(),
                          now_s=expires + HOUR, cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_OK, err
    after = _state_bytes(env)
    assert {"history.jsonl", "forecast.json"} <= {Path(k).name for k in after}
    assert after["quota-router/credits.jsonl"] != before["quota-router/credits.jsonl"]
    assert after["quota-router/calibrations.jsonl"] != before["quota-router/calibrations.jsonl"]


def test_waste_backfill_refuses_dry_run(env) -> None:
    """``waste --backfill`` exists to write; with ``--dry-run`` the request contradicts
    itself, so it fails loudly instead of writing (BACKLOG #35)."""
    before = _state_bytes(env)
    code, _out, err = run(["waste", "--backfill", "--dry-run"], env, cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_USAGE
    assert "--dry-run" in err and "--backfill" in err
    assert _state_bytes(env) == before


def test_dry_run_help_no_longer_promises_no_state(capsys) -> None:
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["status", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "write no state" not in text
    assert "history" in text


def test_doctor_flags_a_pool_with_no_checkpoint_and_a_missing_key(env) -> None:
    from quota_router import doctor

    rows, _ = cli._credit_pool_rows(cli.load_config(env=env, cwd=_NO_CONFIG_FILES), env, NOW)

    def runner(argv, **kwargs):
        assert "-w" not in argv  # existence only; the key is never read here
        return subprocess.CompletedProcess(argv, 44, "", "")

    checks = doctor.check_credit_pools(rows, now_s=NOW, keychain_runner=runner,
                                       services={"anthropic_api_d": "anthropic-api-key-claude-d"})
    by_name = {c.name: c for c in checks}
    assert by_name["credits[anthropic_api_d]"].status == "warn"
    assert "credits set" in by_name["credits[anthropic_api_d]"].remedy
    assert by_name["keychain[anthropic_api_d]"].status == "fail"


def test_doctor_flags_a_stale_forecast_only_when_something_uses_it() -> None:
    from quota_router import doctor
    from quota_router.config import EngineConfig

    stale = doctor.check_forecast(
        computed_at_s=NOW - 7 * HOUR, problem=None, engine=EngineConfig(objective="joint"),
        credit_pools=0, now_s=NOW,
    )
    assert stale[0].status == "warn" and "stale" in stale[0].detail
    missing = doctor.check_forecast(
        computed_at_s=None, problem="no forecast yet", engine=EngineConfig(),
        credit_pools=1, now_s=NOW,
    )
    assert missing[0].status == "warn"
    unused = doctor.check_forecast(
        computed_at_s=None, problem="no forecast yet", engine=EngineConfig(),
        credit_pools=0, now_s=NOW,
    )
    assert unused == []
