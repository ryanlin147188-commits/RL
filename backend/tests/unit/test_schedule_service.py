"""Unit tests for schedule_service — pure date-calculation logic, no DB."""
from __future__ import annotations

from datetime import datetime

import pytest

from app.services.schedule_service import (
    _parse_month_day,
    _parse_weekday_list,
    _py_weekday_to_sun0,
    compute_next_run,
)


# ── _parse_weekday_list ─────────────────────────────────────────────────

class TestParseWeekdayList:
    def test_empty_string_returns_empty(self):
        assert _parse_weekday_list("") == []

    def test_none_returns_empty(self):
        assert _parse_weekday_list(None) == []

    def test_single_value(self):
        assert _parse_weekday_list("3") == [3]

    def test_multiple_values_sorted(self):
        assert _parse_weekday_list("5,1,3") == [1, 3, 5]

    def test_deduplicates(self):
        assert _parse_weekday_list("1,1,2") == [1, 2]

    def test_ignores_out_of_range(self):
        assert _parse_weekday_list("0,6,7,8,-1") == [0, 6]

    def test_ignores_non_numeric(self):
        assert _parse_weekday_list("1,abc,3") == [1, 3]

    def test_all_weekdays(self):
        assert _parse_weekday_list("0,1,2,3,4,5,6") == [0, 1, 2, 3, 4, 5, 6]


# ── _parse_month_day ────────────────────────────────────────────────────

class TestParseMonthDay:
    def test_normal_day(self):
        assert _parse_month_day("15") == 15

    def test_clamps_below_1(self):
        assert _parse_month_day("0") == 1

    def test_clamps_above_31(self):
        assert _parse_month_day("32") == 31

    def test_boundary_1(self):
        assert _parse_month_day("1") == 1

    def test_boundary_31(self):
        assert _parse_month_day("31") == 31

    def test_none_defaults_to_1(self):
        assert _parse_month_day(None) == 1

    def test_empty_defaults_to_1(self):
        assert _parse_month_day("") == 1

    def test_non_numeric_defaults_to_1(self):
        assert _parse_month_day("abc") == 1


# ── _py_weekday_to_sun0 ─────────────────────────────────────────────────

class TestPyWeekdayToSun0:
    def test_monday_is_1(self):
        # 2026-05-11 is Monday; Python weekday() == 0 → Sun0 == 1
        d = datetime(2026, 5, 11)
        assert _py_weekday_to_sun0(d) == 1

    def test_sunday_is_0(self):
        # 2026-05-10 is Sunday; Python weekday() == 6 → Sun0 == 0
        d = datetime(2026, 5, 10)
        assert _py_weekday_to_sun0(d) == 0

    def test_saturday_is_6(self):
        # 2026-05-16 is Saturday; Python weekday() == 5 → Sun0 == 6
        d = datetime(2026, 5, 16)
        assert _py_weekday_to_sun0(d) == 6

    def test_friday_is_5(self):
        d = datetime(2026, 5, 15)  # Friday
        assert _py_weekday_to_sun0(d) == 5


# ── compute_next_run ────────────────────────────────────────────────────

class TestComputeNextRun:
    """Base datetime used across tests: 2026-05-15 14:00 (Friday)."""

    _base = datetime(2026, 5, 15, 14, 0, 0)

    def test_once_returns_none(self):
        result = compute_next_run(
            repeat_type="ONCE",
            repeat_config=None,
            from_time=self._base,
            start_time=self._base,
        )
        assert result is None

    def test_daily_returns_next_day_same_time(self):
        result = compute_next_run(
            repeat_type="DAILY",
            repeat_config=None,
            from_time=self._base,
            start_time=self._base,
        )
        assert result is not None
        assert result.day == 16
        assert result.month == 5
        assert result.hour == 14
        assert result.minute == 0

    def test_daily_preserves_start_time_hm(self):
        start = datetime(2026, 5, 1, 9, 30, 0)
        result = compute_next_run(
            repeat_type="DAILY",
            repeat_config=None,
            from_time=self._base,
            start_time=start,
        )
        assert result is not None
        assert result.hour == 9
        assert result.minute == 30

    def test_weekly_next_occurrence_found_within_7_days(self):
        # from_time is Friday (sun0=5); target Sunday (sun0=0)
        result = compute_next_run(
            repeat_type="WEEKLY",
            repeat_config="0",  # Sunday
            from_time=self._base,
            start_time=self._base,
        )
        assert result is not None
        assert _py_weekday_to_sun0(result) == 0
        delta = (result.date() - self._base.date()).days
        assert 1 <= delta <= 7

    def test_weekly_multiple_days(self):
        # Target Mon(1) and Wed(3); from Friday, next Mon should be picked
        result = compute_next_run(
            repeat_type="WEEKLY",
            repeat_config="1,3",
            from_time=self._base,
            start_time=self._base,
        )
        assert result is not None
        assert _py_weekday_to_sun0(result) in (1, 3)

    def test_weekly_no_config_falls_back_to_daily(self):
        result = compute_next_run(
            repeat_type="WEEKLY",
            repeat_config="",
            from_time=self._base,
            start_time=self._base,
        )
        assert result is not None
        assert result.day == 16  # next day

    def test_monthly_same_month_future_day(self):
        from_time = datetime(2026, 5, 1, 10, 0, 0)
        result = compute_next_run(
            repeat_type="MONTHLY",
            repeat_config="20",
            from_time=from_time,
            start_time=from_time,
        )
        assert result is not None
        assert result.month == 5
        assert result.day == 20

    def test_monthly_rolls_to_next_month(self):
        # from_time is the 20th; day=15 → should roll to next month
        from_time = datetime(2026, 5, 20, 10, 0, 0)
        result = compute_next_run(
            repeat_type="MONTHLY",
            repeat_config="15",
            from_time=from_time,
            start_time=from_time,
        )
        assert result is not None
        assert result.month == 6
        assert result.day == 15

    def test_monthly_clamps_feb_28(self):
        # day=31 in February → should clamp to 28 (non-leap year)
        from_time = datetime(2026, 1, 31, 10, 0, 0)
        result = compute_next_run(
            repeat_type="MONTHLY",
            repeat_config="31",
            from_time=from_time,
            start_time=from_time,
        )
        assert result is not None
        assert result.month == 2
        assert result.day == 28

    def test_unknown_repeat_type_returns_none(self):
        result = compute_next_run(
            repeat_type="INVALID",
            repeat_config=None,
            from_time=self._base,
            start_time=self._base,
        )
        assert result is None

    def test_case_insensitive_repeat_type(self):
        result = compute_next_run(
            repeat_type="daily",
            repeat_config=None,
            from_time=self._base,
            start_time=self._base,
        )
        assert result is not None

    def test_result_is_always_after_from_time(self):
        for rt in ("DAILY", "WEEKLY", "MONTHLY"):
            result = compute_next_run(
                repeat_type=rt,
                repeat_config="1" if rt == "WEEKLY" else "1",
                from_time=self._base,
                start_time=self._base,
            )
            if result is not None:
                assert result > self._base, f"{rt} result {result} should be after {self._base}"
