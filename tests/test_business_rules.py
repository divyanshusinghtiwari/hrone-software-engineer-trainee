from datetime import datetime, timedelta, timezone

from app.main import (
    compute_late_minutes,
    compute_overtime_minutes_for_shift,
    compute_work_hours,
    get_record_date_for_punch_in,
)

IST = timezone(timedelta(hours=5, minutes=30))


def test_overnight_date_mapping():
    ts = datetime(2026, 7, 6, 23, 30, tzinfo=IST)
    assert get_record_date_for_punch_in(ts, "22:00", "06:00") == datetime(2026, 7, 6, 23, 30, tzinfo=IST).date()

    overnight_ts = datetime(2026, 7, 7, 0, 30, tzinfo=IST)
    assert get_record_date_for_punch_in(overnight_ts, "22:00", "06:00") == datetime(2026, 7, 6, 0, 30, tzinfo=IST).date()


def test_late_minutes_is_strict_above_10_minutes():
    punch_in = datetime(2026, 7, 6, 9, 40, tzinfo=IST)
    assert compute_late_minutes(punch_in, "09:30", "2026-07-06") == 0

    punch_in_late = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
    assert compute_late_minutes(punch_in_late, "09:30", "2026-07-06") == 10

    punch_in_late_2 = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST)
    assert compute_late_minutes(punch_in_late_2, "09:30", "2026-07-06") == 45


def test_overtime_threshold_and_overnight_shift():
    punch_out = datetime(2026, 7, 6, 18, 29, tzinfo=IST)
    assert compute_overtime_minutes_for_shift(punch_out, "18:30", "2026-07-06", "09:30") == 0

    punch_out = datetime(2026, 7, 6, 19, 00, tzinfo=IST)
    assert compute_overtime_minutes_for_shift(punch_out, "18:30", "2026-07-06", "09:30") == 30

    overnight_punch_out = datetime(2026, 7, 7, 6, 35, tzinfo=IST)
    assert compute_overtime_minutes_for_shift(overnight_punch_out, "06:00", "2026-07-06", "22:00") == 35


def test_work_hours_round_half_up():
    punch_in = datetime(2026, 7, 6, 9, 0, tzinfo=IST)
    punch_out = datetime(2026, 7, 6, 12, 15, 0, tzinfo=IST)
    assert compute_work_hours(punch_in, punch_out) == 3.25

    punch_in = datetime(2026, 7, 6, 9, 0, tzinfo=IST)
    punch_out = datetime(2026, 7, 6, 12, 16, 0, tzinfo=IST)
    assert compute_work_hours(punch_in, punch_out) == 3.27
