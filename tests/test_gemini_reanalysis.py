"""Guard the offline measurement's contamination and pooling rules."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "gemini_reanalysis", Path(__file__).resolve().parents[1] / "ops/reanalyze-gemini-oct12.py"
)
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


def test_any_pro_overlap_excludes_the_entire_interval():
    # The Pro call finishes after the interval: filtering only on end time
    # (or merely dropping its dollars) must not call this Flash-only movement.
    chain = [({"t": 10, "rep": "flash"}, (1, 1)),
             ({"t": 20, "rep": "flash"}, (0.99, 0.85))]
    calls = [{"model": analysis.PRO, "start": 18, "end": 25, "usd": 0, "succeeded": True},
             {"model": analysis.FLASH, "start": 11, "end": 15, "usd": 1, "succeeded": True}]
    interval = analysis.intervals(chain, calls, 0)[0]
    assert interval["excluded"] == "pro_overlap"
    assert interval["pro_attempts_overlapping"] == 1
    assert interval["usd"] == 1  # retained for audit, excluded as a whole from fine sum


def test_pooled_size_is_total_dollars_over_total_movement():
    result = analysis.summarize([
        {"dw": 0.01, "dh": 0.15, "usd": 10, "flash_calls": 1},
        {"dw": 0.03, "dh": 0.45, "usd": 90, "flash_calls": 9},
    ])
    assert result["weekly_pse"] == pytest.approx(100 / 0.04 / 283.20)
    assert result["k"] == pytest.approx(15)


def test_missing_token_field_is_an_error():
    with pytest.raises(KeyError, match="cache_read_tokens"):
        analysis.price({"input_tokens": 100, "output_tokens": 100, "thinking_tokens": 0})


def test_third_pair_at_first_point_is_reported():
    _, omitted = analysis.track([
        {"t": 10, "rep": "first", "pairs": [(0.9, 0.7), (0.92, 0.72), (0.95, 0.75)]},
        {"t": 20, "rep": "second", "pairs": [(0.89, 0.69), (0.94, 0.74)]},
    ])
    assert omitted == [{"at": analysis.iso(10), "rep": "first", "pairs_omitted": 1}]
