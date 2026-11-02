import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterError, croniter

from app.core.constants import (
    ERR_SCHEDULED_TASK_CRON_INVALID,
    ERR_SCHEDULED_TASK_SCHEDULE_INVALID,
    ERR_SCHEDULED_TASK_TIMEZONE_INVALID,
)
from app.core.exceptions import ParameterException
from app.core.utils.time import get_local_time

_HASH_EXPRESSION = re.compile(r"^(?:h|r)(?:\(\d+-\d+\))?(?:/\d+)?$")


def normalize_cron_expression(expression: str) -> str:
    if not isinstance(expression, str) or len(expression) > 255:
        raise ParameterException(message=ERR_SCHEDULED_TASK_CRON_INVALID)

    fields = expression.strip().lower().split()
    if len(fields) != 5 or any(_HASH_EXPRESSION.fullmatch(part) for field in fields for part in field.split(",")):
        raise ParameterException(message=ERR_SCHEDULED_TASK_CRON_INVALID)

    normalized = " ".join(fields)
    try:
        valid = croniter.is_valid(normalized, strict=True)
    except (CroniterError, ValueError) as exc:
        raise ParameterException(message=ERR_SCHEDULED_TASK_CRON_INVALID) from exc
    if not valid:
        raise ParameterException(message=ERR_SCHEDULED_TASK_CRON_INVALID)
    return normalized


def validate_schedule(
    *,
    interval_seconds: int | None,
    cron_expression: str | None,
    timezone: str | None,
    run_once: bool = False,
) -> str | None:
    has_interval = interval_seconds is not None
    has_cron = cron_expression is not None
    if has_interval == has_cron:
        raise ParameterException(message=ERR_SCHEDULED_TASK_SCHEDULE_INVALID)

    if has_interval:
        if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int) or interval_seconds < 60:
            raise ParameterException(message=ERR_SCHEDULED_TASK_SCHEDULE_INVALID)
        if timezone is not None:
            raise ParameterException(message=ERR_SCHEDULED_TASK_TIMEZONE_INVALID)
        return None

    normalized = normalize_cron_expression(cron_expression)
    if run_once:
        raise ParameterException(message=ERR_SCHEDULED_TASK_SCHEDULE_INVALID)
    if not isinstance(timezone, str) or not timezone:
        raise ParameterException(message=ERR_SCHEDULED_TASK_TIMEZONE_INVALID)
    try:
        task_timezone = ZoneInfo(timezone)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ParameterException(message=ERR_SCHEDULED_TASK_TIMEZONE_INVALID) from exc

    now = get_local_time()
    try:
        iterator = croniter(
            normalized,
            now.astimezone(task_timezone),
            ret_type=datetime,
            day_or=True,
            max_years_between_matches=50,
        )
        next_run = iterator.get_next(datetime)
    except (CroniterError, ValueError) as exc:
        raise ParameterException(message=ERR_SCHEDULED_TASK_CRON_INVALID) from exc
    if next_run <= now.astimezone(task_timezone):
        raise ParameterException(message=ERR_SCHEDULED_TASK_CRON_INVALID)
    return normalized


def get_next_run_times(
    *,
    interval_seconds: int | None = None,
    cron_expression: str | None = None,
    timezone: str | None = None,
    run_once: bool = False,
    after: datetime | None = None,
    count: int = 3,
) -> list[datetime]:
    normalized = validate_schedule(
        interval_seconds=interval_seconds,
        cron_expression=cron_expression,
        timezone=timezone,
        run_once=run_once,
    )

    if after is None:
        after = get_local_time()
    if after.tzinfo is None:
        after = after.replace(tzinfo=get_local_time().tzinfo)

    if normalized is None:
        result_count = 1 if run_once else count
        try:
            return [after + timedelta(seconds=interval_seconds * index) for index in range(1, result_count + 1)]
        except (OverflowError, TypeError) as exc:
            raise ParameterException(message=ERR_SCHEDULED_TASK_SCHEDULE_INVALID) from exc

    task_timezone = ZoneInfo(timezone)

    try:
        iterator = croniter(
            normalized,
            after.astimezone(task_timezone),
            ret_type=datetime,
            day_or=True,
            max_years_between_matches=50,
        )
        result = []
        previous_run = after
        for _ in range(1, count + 1):
            next_run = iterator.get_next(datetime).astimezone(after.tzinfo)
            if next_run <= previous_run:
                raise ParameterException(message=ERR_SCHEDULED_TASK_CRON_INVALID)
            result.append(next_run)
            previous_run = next_run
        return result
    except ParameterException:
        raise
    except (CroniterError, ValueError) as exc:
        raise ParameterException(message=ERR_SCHEDULED_TASK_CRON_INVALID) from exc
