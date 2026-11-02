from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.core.constants import (
    ERR_SCHEDULED_TASK_CRON_INVALID,
    ERR_SCHEDULED_TASK_SCHEDULE_INVALID,
    ERR_SCHEDULED_TASK_TIMEZONE_INVALID,
)
from app.core.exceptions import ParameterException
from app.core.utils.scheduled_time import get_next_run_times, normalize_cron_expression, validate_schedule
from app.schemas.scheduled_task import ScheduledTaskCreateRequest


def test_normalize_cron_expression_trims_whitespace_and_lowercases_fields():
    assert normalize_cron_expression("  0 9 * * MON  ") == "0 9 * * mon"


def test_create_request_normalizes_cron_expression():
    request = ScheduledTaskCreateRequest(
        name="morning task",
        session_id="session-1",
        message="run",
        cron_expression="  0 9 * * *  ",
        timezone="UTC",
    )

    assert request.cron_expression == "0 9 * * *"


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        (
            datetime(2026, 1, 2, 8, 59, tzinfo=UTC),
            datetime(2026, 1, 2, 9, tzinfo=UTC),
        ),
        (
            datetime(2026, 1, 2, 9, 1, tzinfo=UTC),
            datetime(2026, 1, 3, 9, tzinfo=UTC),
        ),
    ],
)
def test_daily_schedule_chooses_the_next_run_before_or_after_same_day_time(after, expected):
    result = get_next_run_times(
        cron_expression="0 9 * * *",
        timezone="UTC",
        after=after,
        count=1,
    )

    assert result == [expected]


def test_daily_schedule_returns_three_increasing_runs():
    result = get_next_run_times(
        cron_expression="0 9 * * *",
        timezone="UTC",
        after=datetime(2026, 1, 2, 8, 59, tzinfo=UTC),
        count=3,
    )

    assert result == [
        datetime(2026, 1, 2, 9, tzinfo=UTC),
        datetime(2026, 1, 3, 9, tzinfo=UTC),
        datetime(2026, 1, 4, 9, tzinfo=UTC),
    ]
    assert result[0] < result[1] < result[2]


def test_weekly_schedule_returns_the_next_three_weekdays():
    result = get_next_run_times(
        cron_expression="30 14 * * 1",
        timezone="UTC",
        after=datetime(2026, 1, 5, 14, 29, tzinfo=UTC),
        count=3,
    )

    assert result == [
        datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
        datetime(2026, 1, 12, 14, 30, tzinfo=UTC),
        datetime(2026, 1, 19, 14, 30, tzinfo=UTC),
    ]


@pytest.mark.parametrize(
    ("cron_expression", "after", "expected"),
    [
        (
            "0 0 28 * *",
            datetime(2026, 1, 1, tzinfo=UTC),
            [
                datetime(2026, 1, 28, tzinfo=UTC),
                datetime(2026, 2, 28, tzinfo=UTC),
                datetime(2026, 3, 28, tzinfo=UTC),
            ],
        ),
        (
            "0 0 29 * *",
            datetime(2028, 1, 1, tzinfo=UTC),
            [
                datetime(2028, 1, 29, tzinfo=UTC),
                datetime(2028, 2, 29, tzinfo=UTC),
                datetime(2028, 3, 29, tzinfo=UTC),
            ],
        ),
        (
            "0 0 30 * *",
            datetime(2026, 1, 1, tzinfo=UTC),
            [
                datetime(2026, 1, 30, tzinfo=UTC),
                datetime(2026, 3, 30, tzinfo=UTC),
                datetime(2026, 4, 30, tzinfo=UTC),
            ],
        ),
        (
            "0 0 31 * *",
            datetime(2026, 1, 1, tzinfo=UTC),
            [
                datetime(2026, 1, 31, tzinfo=UTC),
                datetime(2026, 3, 31, tzinfo=UTC),
                datetime(2026, 5, 31, tzinfo=UTC),
            ],
        ),
    ],
)
def test_month_day_schedules_follow_the_calendar_and_skip_missing_days(cron_expression, after, expected):
    result = get_next_run_times(
        cron_expression=cron_expression,
        timezone="UTC",
        after=after,
        count=3,
    )

    assert result == expected


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        (
            datetime(2026, 2, 1, tzinfo=UTC),
            [datetime(2026, 2, 28, 20, tzinfo=UTC)],
        ),
        (
            datetime(2028, 2, 1, tzinfo=UTC),
            [datetime(2028, 2, 29, 20, tzinfo=UTC)],
        ),
        (
            datetime(2026, 4, 1, tzinfo=UTC),
            [datetime(2026, 4, 30, 20, tzinfo=UTC)],
        ),
        (
            datetime(2026, 12, 1, tzinfo=UTC),
            [
                datetime(2026, 12, 31, 20, tzinfo=UTC),
                datetime(2027, 1, 31, 20, tzinfo=UTC),
            ],
        ),
        (
            datetime(2026, 2, 28, 20, 1, tzinfo=UTC),
            [datetime(2026, 3, 31, 20, tzinfo=UTC)],
        ),
    ],
)
def test_last_day_expression_returns_calendar_month_ends(after, expected):
    result = get_next_run_times(
        cron_expression="0 20 L * *",
        timezone="UTC",
        after=after,
        count=len(expected),
    )

    assert result == expected


def test_third_friday_hash_expression_returns_the_correct_calendar_dates():
    result = get_next_run_times(
        cron_expression="0 9 * * 5#3",
        timezone="UTC",
        after=datetime(2026, 1, 1, tzinfo=UTC),
        count=3,
    )

    assert result == [
        datetime(2026, 1, 16, 9, tzinfo=UTC),
        datetime(2026, 2, 20, 9, tzinfo=UTC),
        datetime(2026, 3, 20, 9, tzinfo=UTC),
    ]


def test_calendar_schedule_does_not_drift_by_fixed_seconds():
    result = get_next_run_times(
        cron_expression="0 9 1 * *",
        timezone="UTC",
        after=datetime(2026, 1, 31, 9, tzinfo=UTC),
        count=3,
    )

    assert result == [
        datetime(2026, 2, 1, 9, tzinfo=UTC),
        datetime(2026, 3, 1, 9, tzinfo=UTC),
        datetime(2026, 4, 1, 9, tzinfo=UTC),
    ]


def test_yearly_schedule_crosses_into_the_next_year():
    result = get_next_run_times(
        cron_expression="0 0 1 1 *",
        timezone="UTC",
        after=datetime(2026, 12, 31, 23, 59, tzinfo=UTC),
        count=3,
    )

    assert result == [
        datetime(2027, 1, 1, tzinfo=UTC),
        datetime(2028, 1, 1, tzinfo=UTC),
        datetime(2029, 1, 1, tzinfo=UTC),
    ]


def test_cron_results_are_converted_back_to_after_timezone():
    after_timezone = timezone(timedelta(hours=8))
    after = datetime(2026, 1, 1, 20, tzinfo=after_timezone)

    result = get_next_run_times(
        cron_expression="0 9 * * *",
        timezone="America/New_York",
        after=after,
        count=2,
    )

    assert result == [
        datetime(2026, 1, 1, 22, tzinfo=after_timezone),
        datetime(2026, 1, 2, 22, tzinfo=after_timezone),
    ]
    assert all(run.tzinfo == after.tzinfo for run in result)


def test_new_york_nine_am_stays_local_across_daylight_saving_time():
    new_york = ZoneInfo("America/New_York")
    result = get_next_run_times(
        cron_expression="0 9 * * *",
        timezone="America/New_York",
        after=datetime(2026, 3, 7, 8, tzinfo=new_york),
        count=3,
    )

    assert result == [
        datetime(2026, 3, 7, 9, tzinfo=new_york),
        datetime(2026, 3, 8, 9, tzinfo=new_york),
        datetime(2026, 3, 9, 9, tzinfo=new_york),
    ]
    assert [run.hour for run in result] == [9, 9, 9]
    assert result[0].utcoffset() == timedelta(hours=-5)
    assert result[1].utcoffset() == timedelta(hours=-4)


def test_interval_schedule_preserves_count_and_interval():
    after = datetime(2026, 1, 2, 12, tzinfo=UTC)

    result = get_next_run_times(interval_seconds=90, after=after, count=3)

    assert result == [
        after + timedelta(seconds=90),
        after + timedelta(seconds=180),
        after + timedelta(seconds=270),
    ]
    assert result[0] < result[1] < result[2]


def test_interval_run_once_returns_only_one_future_run():
    after = datetime(2026, 1, 2, 12, tzinfo=UTC)

    result = get_next_run_times(interval_seconds=3600, run_once=True, after=after, count=3)

    assert result == [after + timedelta(hours=1)]


@pytest.mark.parametrize(
    "expression",
    [
        "60 0 * * *",
        "0 9 * *",
        "0 9 * * * *",
        "0 9 * * * * *",
        "@daily",
        "@weekly",
        "H * * * *",
        "R * * * *",
        "H(1-5)/2 * * * *",
    ],
)
def test_invalid_cron_expressions_are_rejected(expression):
    with pytest.raises(ParameterException) as exc_info:
        normalize_cron_expression(expression)

    assert exc_info.value.message == ERR_SCHEDULED_TASK_CRON_INVALID


def test_impossible_calendar_date_is_rejected():
    with pytest.raises(ParameterException) as exc_info:
        validate_schedule(
            interval_seconds=None,
            cron_expression="0 0 31 4 *",
            timezone="UTC",
        )

    assert exc_info.value.message == ERR_SCHEDULED_TASK_CRON_INVALID


@pytest.mark.parametrize("timezone_name", [None, "", "Mars/Olympus"])
def test_cron_requires_a_valid_timezone(timezone_name):
    with pytest.raises(ParameterException) as exc_info:
        validate_schedule(
            interval_seconds=None,
            cron_expression="0 9 * * *",
            timezone=timezone_name,
        )

    assert exc_info.value.message == ERR_SCHEDULED_TASK_TIMEZONE_INVALID


def test_interval_rejects_a_timezone():
    with pytest.raises(ParameterException) as exc_info:
        validate_schedule(interval_seconds=60, cron_expression=None, timezone="UTC")

    assert exc_info.value.message == ERR_SCHEDULED_TASK_TIMEZONE_INVALID


@pytest.mark.parametrize(
    "schedule",
    [
        {
            "interval_seconds": 60,
            "cron_expression": "0 9 * * *",
            "timezone": "UTC",
        },
        {
            "interval_seconds": None,
            "cron_expression": None,
            "timezone": None,
        },
    ],
)
def test_interval_and_cron_schedule_modes_are_mutually_exclusive(schedule):
    with pytest.raises(ParameterException) as exc_info:
        validate_schedule(**schedule)

    assert exc_info.value.message == ERR_SCHEDULED_TASK_SCHEDULE_INVALID


def test_cron_run_once_is_rejected():
    with pytest.raises(ParameterException) as exc_info:
        validate_schedule(
            interval_seconds=None,
            cron_expression="0 9 * * *",
            timezone="UTC",
            run_once=True,
        )

    assert exc_info.value.message == ERR_SCHEDULED_TASK_SCHEDULE_INVALID


@pytest.mark.parametrize("interval_seconds", [59, True])
def test_interval_requires_at_least_one_minute(interval_seconds):
    with pytest.raises(ParameterException) as exc_info:
        validate_schedule(interval_seconds=interval_seconds, cron_expression=None, timezone=None)

    assert exc_info.value.message == ERR_SCHEDULED_TASK_SCHEDULE_INVALID
