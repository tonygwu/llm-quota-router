"""``history.iter_records`` over rotated files: order, and skipping files a read cannot need.

Rotation names a day's FIRST rotated file ``history-<day>.jsonl`` and the later ones
``history-<day>.1.jsonl``, ``.2`` and so on (``_rotation_target``). Sorting those names as
strings puts ``.1`` and ``.2`` before the bare name, because ``1`` sorts before ``j``, so a
day's records came back out of order although the docstring promises oldest-first.

A ``since_s`` read used to parse every line of every retained file (about 200 MB on the
operator's machine) only to discard most of them. Rotated files are named by the UTC day
of their first record and hold one day each, so files that end well before ``since_s`` are
skipped unopened. One day of slack is kept so a file holding a straggler past its own day
is still read.
"""

from __future__ import annotations

import json
from pathlib import Path

from quota_router import history

DAY = 86_400.0
#: 2026-10-01T00:00:00Z
D1 = 1_790_812_800.0


def _write(path: Path, *stamps: float) -> None:
    path.write_text("".join(json.dumps({"t": t, "event": "status"}) + "\n" for t in stamps))


def _layout(tmp_path: Path) -> Path:
    live = tmp_path / "history.jsonl"
    # Day 1 rotated three times: the bare name is the OLDEST of the day.
    _write(tmp_path / "history-2026-10-01.jsonl", D1 + 10)
    _write(tmp_path / "history-2026-10-01.1.jsonl", D1 + 20)
    _write(tmp_path / "history-2026-10-01.2.jsonl", D1 + 30)
    _write(tmp_path / "history-2026-10-02.jsonl", D1 + DAY + 10)
    _write(tmp_path / "history-2026-10-05.jsonl", D1 + 4 * DAY + 10)
    _write(live, D1 + 5 * DAY + 10)
    return live


def test_rotated_files_of_one_day_come_back_oldest_first(tmp_path) -> None:
    live = _layout(tmp_path)
    stamps = [r["t"] for r in history.iter_records(live, include_rotated=True)]
    assert stamps == sorted(stamps)
    assert stamps[:3] == [D1 + 10, D1 + 20, D1 + 30]


def test_a_since_read_skips_files_that_end_before_it(tmp_path, monkeypatch) -> None:
    live = _layout(tmp_path)
    opened: list[str] = []
    real_open = open

    def spy(file, *args, **kwargs):
        opened.append(Path(file).name)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(history, "open", spy, raising=False)
    since = D1 + 4 * DAY  # 2026-10-05
    records = list(history.iter_records(live, include_rotated=True, since_s=since))

    assert [r["t"] for r in records] == [D1 + 4 * DAY + 10, D1 + 5 * DAY + 10]
    # A file named for day D holds records of day D, and with one day of slack it may
    # hold D+1. Against a cutoff on 10-05 that skips 10-01 and 10-02; a file named 10-04
    # would still be read.
    assert sorted(opened) == ["history-2026-10-05.jsonl", "history.jsonl"]


def test_skipping_never_changes_what_a_since_read_returns(tmp_path) -> None:
    live = _layout(tmp_path)
    everything = list(history.iter_records(live, include_rotated=True))
    for since in (D1, D1 + 15, D1 + DAY, D1 + 3 * DAY, D1 + 4 * DAY + 11, D1 + 9 * DAY):
        expected = [r for r in everything if r["t"] >= since]
        got = list(history.iter_records(live, include_rotated=True, since_s=since))
        assert got == expected, since


def test_a_rotated_name_without_a_date_is_still_read(tmp_path) -> None:
    live = _layout(tmp_path)
    _write(tmp_path / "history-manual.jsonl", D1 + 9 * DAY)
    stamps = [
        r["t"] for r in history.iter_records(live, include_rotated=True, since_s=D1 + 8 * DAY)
    ]
    assert D1 + 9 * DAY in stamps
