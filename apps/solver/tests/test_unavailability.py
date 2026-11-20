from datetime import date, datetime, time, timedelta, timezone

from models import Unavailability
from services.unavailability import unavailability_intervals
from tests.helpers import NEW_YORK

UTC = timezone.utc
APRIL = (date(2027, 4, 1), date(2027, 4, 30))


def row(**columns) -> Unavailability:
    return Unavailability(target_type="organization", **columns)


def spans(intervals):
    return [(i.start, i.end) for i in intervals]


def test_weekly_row_repeats_on_its_weekday_inside_the_window():
    intervals = unavailability_intervals(row(day_of_week=2, start_time=time(16), end_time=time(18)), *APRIL, NEW_YORK)
    assert [i.start.astimezone(NEW_YORK).date() for i in intervals] == [
        date(2027, 4, 7), date(2027, 4, 14), date(2027, 4, 21), date(2027, 4, 28)
    ]
    assert spans(intervals)[0] == (datetime(2027, 4, 7, 20, tzinfo=UTC), datetime(2027, 4, 7, 22, tzinfo=UTC))


def test_whole_day_runs_from_local_midnight_to_midnight():
    (interval,) = unavailability_intervals(
        row(specific_date=date(2027, 5, 31)), date(2027, 5, 1), date(2027, 6, 30), NEW_YORK
    )
    assert (interval.start, interval.end) == (
        datetime(2027, 5, 31, 4, tzinfo=UTC),
        datetime(2027, 6, 1, 4, tzinfo=UTC),
    )


def test_whole_day_on_spring_forward_is_23_hours():
    (interval,) = unavailability_intervals(
        row(specific_date=date(2027, 3, 14)), date(2027, 3, 1), date(2027, 3, 31), NEW_YORK
    )
    assert interval.end - interval.start == timedelta(hours=23)


def test_date_range_is_clipped_to_the_window_and_keeps_its_reason():
    intervals = unavailability_intervals(
        row(specific_date=date(2027, 3, 29), end_date=date(2027, 4, 2), reason="Spring break"), *APRIL, NEW_YORK
    )
    assert [i.start.astimezone(NEW_YORK).date() for i in intervals] == [date(2027, 4, 1), date(2027, 4, 2)]
    assert {i.reason for i in intervals} == {"Spring break"}


def test_dates_outside_the_window_produce_nothing():
    assert unavailability_intervals(row(specific_date=date(2027, 7, 4)), *APRIL, NEW_YORK) == []
