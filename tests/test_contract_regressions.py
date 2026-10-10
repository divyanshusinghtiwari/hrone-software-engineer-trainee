import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

import app.main as app_main
from app.main import (
    compute_late_minutes,
    compute_overtime_minutes_for_shift,
    compute_work_hours,
    get_record_date_for_punch_in,
)

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture
def isolated_db(monkeypatch):
    mongo_uri = "mongodb://localhost:27017"
    db_name = f"hrone_test_{uuid.uuid4().hex}"
    monkeypatch.setenv("MONGO_URI", mongo_uri)
    monkeypatch.setenv("MONGO_DB", db_name)

    app_main.MONGO_URI = mongo_uri
    app_main.MONGO_DB = db_name
    app_main.client = MongoClient(mongo_uri, tz_aware=True)
    app_main.db = app_main.client[db_name]
    try:
        with TestClient(app_main.app):
            yield app_main
    finally:
        app_main.client.drop_database(db_name)
        app_main.client.close()


def _ist_ms(day: str, clock: str) -> int:
    instant = datetime.fromisoformat(f"{day}T{clock}:00").replace(tzinfo=IST)
    return int(instant.timestamp() * 1000)


def _employee_payload(emp_code: str, department: str = "Engineering", shift_start: str = "09:30", shift_end: str = "18:30"):
    return {
        "emp_code": emp_code,
        "name": emp_code,
        "email": f"{emp_code.lower()}@example.com",
        "department": department,
        "shift_start": shift_start,
        "shift_end": shift_end,
        "joined_on": "2026-07-01",
    }


def test_overnight_record_date_mapping_for_early_morning_punch_in():
    punch_in = datetime(2026, 7, 7, 0, 30, tzinfo=IST)
    assert get_record_date_for_punch_in(punch_in, "22:00", "06:00") == datetime(2026, 7, 6, tzinfo=IST).date()

    punch_in = datetime(2026, 7, 6, 23, 30, tzinfo=IST)
    assert get_record_date_for_punch_in(punch_in, "22:00", "06:00") == datetime(2026, 7, 6, tzinfo=IST).date()


def test_late_minutes_are_strictly_more_than_ten_minutes():
    assert compute_late_minutes(datetime(2026, 7, 6, 9, 40, tzinfo=IST), "09:30", "2026-07-06") == 0
    assert compute_late_minutes(datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST), "09:30", "2026-07-06") == 10
    assert compute_late_minutes(datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST), "09:30", "2026-07-06") == 45


def test_work_hours_round_half_up_not_bankers_rounding():
    punch_in = datetime(2026, 7, 6, 9, 0, tzinfo=IST)
    punch_out = datetime(2026, 7, 6, 12, 16, 0, tzinfo=IST)
    assert compute_work_hours(punch_in, punch_out) == 3.27

    punch_in = datetime(2026, 7, 6, 9, 0, tzinfo=IST)
    punch_out = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)
    assert compute_work_hours(punch_in, punch_out) == 0.5


def test_overtime_requires_30_minutes_and_overnight_is_supported():
    assert compute_overtime_minutes_for_shift(datetime(2026, 7, 6, 18, 29, tzinfo=IST), "18:30", "2026-07-06", "09:30") == 0
    assert compute_overtime_minutes_for_shift(datetime(2026, 7, 6, 19, 0, tzinfo=IST), "18:30", "2026-07-06", "09:30") == 30
    assert compute_overtime_minutes_for_shift(datetime(2026, 7, 7, 6, 35, tzinfo=IST), "06:00", "2026-07-06", "22:00") == 35


def test_live_local_mongodb_is_used_for_database_checks(isolated_db):
    client = TestClient(isolated_db.app)

    health = client.get("/health")
    assert health.status_code == 200, health.text
    assert health.json() == {"status": "ok"}

    ping = isolated_db.db.command("ping")
    assert ping["ok"] == 1.0

    employee_info = isolated_db.db.employees.index_information()
    attendance_info = isolated_db.db.attendance_logs.index_information()
    assert "u_emp_code" in employee_info
    assert "u_emp_date" in attendance_info


def test_employee_workflow_and_analytics_run_against_isolated_db(isolated_db):
    api = TestClient(isolated_db.app)

    employee = {
        "emp_code": "EMP9001",
        "name": "DB Test User",
        "email": "dbtest@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    }

    create_response = api.post("/employees", json=employee)
    assert create_response.status_code == 201, create_response.text
    assert create_response.json()["emp_code"] == "EMP9001"

    list_response = api.get("/employees", params={"department": "Engineering"})
    assert list_response.status_code == 200, list_response.text
    assert any(item["emp_code"] == "EMP9001" for item in list_response.json()["items"])

    punch_in_ts = int(datetime(2026, 7, 6, 9, 40, tzinfo=timezone.utc).timestamp() * 1000)
    punch_in_response = api.post(
        "/attendance/punch-in",
        json={
            "emp_code": "EMP9001",
            "punched_at": punch_in_ts,
            "status": "PRESENT",
        },
    )
    assert punch_in_response.status_code == 201, punch_in_response.text
    assert punch_in_response.json()["date"] == "2026-07-06"

    duplicate_punch_in = api.post(
        "/attendance/punch-in",
        json={
            "emp_code": "EMP9001",
            "punched_at": punch_in_ts,
            "status": "PRESENT",
        },
    )
    assert duplicate_punch_in.status_code == 409, duplicate_punch_in.text

    punch_out_ts = int(datetime(2026, 7, 6, 18, 45, tzinfo=timezone.utc).timestamp() * 1000)
    punch_out_response = api.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP9001",
            "punched_at": punch_out_ts,
        },
    )
    assert punch_out_response.status_code == 200, punch_out_response.text
    punch_out_body = punch_out_response.json()
    assert punch_out_body["work_hours"] is not None
    assert punch_out_body["overtime_minutes"] >= 0

    regularize_response = api.patch(
        "/attendance/EMP9001/2026-07-06",
        json={
            "punch_in": int(datetime(2026, 7, 6, 9, 35, tzinfo=timezone.utc).timestamp() * 1000),
            "punch_out": int(datetime(2026, 7, 6, 18, 50, tzinfo=timezone.utc).timestamp() * 1000),
            "reason": "adjusted times",
            "regularized_by": "manager",
        },
    )
    assert regularize_response.status_code == 200, regularize_response.text
    assert regularize_response.json()["history"]

    monthly_response = api.get("/analytics/employees/EMP9001/monthly", params={"month": "2026-07"})
    assert monthly_response.status_code == 200, monthly_response.text
    monthly_body = monthly_response.json()
    assert monthly_body["emp_code"] == "EMP9001"
    assert monthly_body["working_days"] >= 1

    department_summary = api.get("/analytics/departments/summary", params={"month": "2026-07"})
    assert department_summary.status_code == 200, department_summary.text
    assert any(item["department"] == "Engineering" for item in department_summary.json()["items"])

    leaderboard = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 10})
    assert leaderboard.status_code == 200, leaderboard.text

    trend = api.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-01", "to": "2026-07-07"})
    assert trend.status_code == 200, trend.text
    assert len(trend.json()["items"]) == 7

    explain = api.get("/admin/explain/attendance_list", params={"page": 1, "page_size": 20})
    assert explain.status_code == 200, explain.text
    assert "explain" in explain.json()


def test_employee_attendance_analytics_and_explain_endpoints(isolated_db):
    with TestClient(isolated_db.app) as api:
        health = api.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}

        for emp_code in ["EMP9101", "EMP9102", "EMP9103", "EMP9104"]:
            response = api.post("/employees", json=_employee_payload(emp_code))
            assert response.status_code == 201, response.text
            assert response.json()["emp_code"] == emp_code
            assert isinstance(response.json()["created_at"], int)
            assert "_id" not in response.json()

        duplicate_employee = api.post("/employees", json=_employee_payload("EMP9101"))
        assert duplicate_employee.status_code == 409
        invalid_employee = api.post("/employees", json=_employee_payload("EMP91"))
        assert invalid_employee.status_code == 422
        equal_shift = api.post("/employees", json=_employee_payload("EMP9105", shift_start="09:30", shift_end="09:30"))
        assert equal_shift.status_code == 422

        employee_page = api.get("/employees", params={"department": "Engineering", "page": 2, "page_size": 2})
        assert employee_page.status_code == 200
        assert employee_page.json()["total"] == 4
        assert [item["emp_code"] for item in employee_page.json()["items"]] == ["EMP9103", "EMP9104"]
        invalid_page = api.get("/employees", params={"page_size": 101})
        assert invalid_page.status_code == 422

        punch_times = {"EMP9101": "10:00", "EMP9102": "10:00", "EMP9103": "09:50"}
        statuses = {"EMP9101": "PRESENT", "EMP9102": "WFH", "EMP9103": "ON_DUTY"}
        for emp_code, clock in punch_times.items():
            response = api.post(
                "/attendance/punch-in",
                json={"emp_code": emp_code, "punched_at": _ist_ms("2026-07-06", clock), "status": statuses[emp_code]},
            )
            assert response.status_code == 201, response.text
            assert response.json()["date"] == "2026-07-06"
            assert response.json()["late_minutes"] == (30 if clock == "10:00" else 20)
            assert response.json()["punch_out"] is None
            assert response.json()["work_hours"] is None

        duplicate_punch = api.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP9101", "punched_at": _ist_ms("2026-07-06", "10:00")},
        )
        assert duplicate_punch.status_code == 409
        invalid_timestamp = api.post("/attendance/punch-in", json={"emp_code": "EMP9104", "punched_at": 1000})
        assert invalid_timestamp.status_code == 422
        unknown_punch = api.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP9999", "punched_at": _ist_ms("2026-07-06", "09:30")},
        )
        assert unknown_punch.status_code == 404

        excessive_punch_out = api.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP9101", "punched_at": _ist_ms("2026-07-07", "10:01")},
        )
        assert excessive_punch_out.status_code == 422
        punch_out_times = {"EMP9101": "18:30", "EMP9102": "19:00", "EMP9103": "13:45"}
        for emp_code, clock in punch_out_times.items():
            response = api.post(
                "/attendance/punch-out",
                json={"emp_code": emp_code, "punched_at": _ist_ms("2026-07-06", clock)},
            )
            assert response.status_code == 200, response.text
            expected_hours = {"EMP9101": 8.5, "EMP9102": 9.0, "EMP9103": 3.92}[emp_code]
            assert response.json()["work_hours"] == expected_hours
            assert response.json()["half_day"] is (emp_code == "EMP9103")
        assert api.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP9104", "punched_at": _ist_ms("2026-07-06", "18:30")},
        ).status_code == 404

        attendance_page = api.get("/attendance", params={"page": 2, "page_size": 1})
        assert attendance_page.status_code == 200
        assert attendance_page.json()["total"] == 3
        assert attendance_page.json()["items"][0]["emp_code"] == "EMP9102"
        filtered_attendance = api.get(
            "/attendance",
            params={"date_from": "2026-07-06", "date_to": "2026-07-06", "status": "WFH"},
        )
        assert filtered_attendance.status_code == 200
        assert filtered_attendance.json()["total"] == 1
        assert filtered_attendance.json()["items"][0]["emp_code"] == "EMP9102"
        reversed_dates = api.get("/attendance", params={"date_from": "2026-07-07", "date_to": "2026-07-06"})
        assert reversed_dates.status_code == 422

        leaderboard = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 1})
        assert leaderboard.status_code == 200
        assert [(row["emp_code"], row["rank"], row["total_late_minutes"]) for row in leaderboard.json()["items"]] == [
            ("EMP9101", 1, 30),
            ("EMP9102", 1, 30),
        ]

        regularized = api.patch(
            "/attendance/EMP9101/2026-07-06",
            json={"punch_in": _ist_ms("2026-07-06", "09:45"), "reason": "corrected punch-in", "regularized_by": "manager"},
        )
        assert regularized.status_code == 200, regularized.text
        regularized_body = regularized.json()
        assert regularized_body["late_minutes"] == 15
        assert regularized_body["work_hours"] == 8.75
        assert len(regularized_body["history"]) == 1
        assert regularized_body["history"][0]["by"] == "manager"
        assert regularized_body["history"][0]["reason"] == "corrected punch-in"
        assert set(regularized_body["history"][0]["changes"]) == {"punch_in", "late_minutes", "work_hours"}
        no_change = api.patch(
            "/attendance/EMP9101/2026-07-06",
            json={"punch_in": _ist_ms("2026-07-06", "09:45"), "reason": "no change", "regularized_by": "manager"},
        )
        assert no_change.status_code == 422
        absent_with_time = api.patch(
            "/attendance/EMP9101/2026-07-06",
            json={"status": "ABSENT", "punch_in": _ist_ms("2026-07-06", "09:45"), "reason": "invalid absent", "regularized_by": "manager"},
        )
        assert absent_with_time.status_code == 422

        monthly = api.get("/analytics/employees/EMP9101/monthly", params={"month": "2026-07"})
        assert monthly.status_code == 200
        assert monthly.json() == {
            "emp_code": "EMP9101",
            "month": "2026-07",
            "working_days": 23,
            "present_days": 1.0,
            "leave_days": 0,
            "late_count": 1,
            "total_late_minutes": 15,
            "total_overtime_minutes": 0,
            "attendance_pct": 4.35,
        }

        summary = api.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Engineering"})
        assert summary.status_code == 200
        assert summary.json()["items"] == [{
            "department": "Engineering",
            "headcount": 4,
            "present_days": 2.5,
            "avg_work_hours": 7.22,
            "late_count": 3,
            "total_late_minutes": 65,
            "leave_count": 0,
            "on_duty_count": 1,
        }]

        trend = api.get(
            "/analytics/departments/Engineering/trend",
            params={"from": "2026-07-06", "to": "2026-07-08"},
        )
        assert trend.status_code == 200
        trend_rows = trend.json()["items"]
        assert [row["date"] for row in trend_rows] == ["2026-07-06", "2026-07-07", "2026-07-08"]
        assert [row["headcount"] for row in trend_rows] == [4, 4, 4]
        assert [row["present_count"] for row in trend_rows] == [2.5, 0, 0]
        assert [row["attendance_rate"] for row in trend_rows] == [0.625, 0.0, 0.0]
        assert [row["moving_avg_7d"] for row in trend_rows] == [0.625, 0.3125, 0.2083]
        invalid_trend = api.get(
            "/analytics/departments/Engineering/trend",
            params={"from": "2026-07-08", "to": "2026-07-06"},
        )
        assert invalid_trend.status_code == 422

        explain_calls = [
            ("attendance_list", {"page": 2, "page_size": 1, "date_from": "2026-07-06", "date_to": "2026-07-06"}),
            ("employee_monthly", {"emp_code": "EMP9101", "month": "2026-07"}),
            ("department_summary", {"department": "Engineering", "month": "2026-07"}),
            ("late_leaderboard", {"month": "2026-07", "limit": 1}),
            ("department_trend", {"department": "Engineering", "from": "2026-07-06", "to": "2026-07-08"}),
        ]
        for endpoint, params in explain_calls:
            response = api.get(f"/admin/explain/{endpoint}", params=params)
            assert response.status_code == 200, response.text
            assert response.json()["endpoint"] == endpoint
            assert response.json()["collection"] == "attendance_logs"
            assert isinstance(response.json()["explain"], dict)
            explain = response.json()["explain"]
            assert "queryPlanner" in explain or any(
                "queryPlanner" in stage.get("$cursor", {}) for stage in explain.get("stages", [])
            )
        missing_explain_params = api.get("/admin/explain/employee_monthly")
        assert missing_explain_params.status_code == 422
        unsupported_explain = api.get("/admin/explain/not_an_endpoint")
        assert unsupported_explain.status_code == 422


def test_overnight_punches_and_regularization_across_midnight(isolated_db):
    with TestClient(isolated_db.app) as api:
        employee = api.post(
            "/employees",
            json=_employee_payload("EMP9301", department="Night Shift", shift_start="22:00", shift_end="06:00"),
        )
        assert employee.status_code == 201

        punch_in = api.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP9301", "punched_at": _ist_ms("2026-07-07", "00:30")},
        )
        assert punch_in.status_code == 201
        assert punch_in.json()["date"] == "2026-07-06"
        assert punch_in.json()["late_minutes"] == 150
        duplicate = api.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP9301", "punched_at": _ist_ms("2026-07-07", "01:00")},
        )
        assert duplicate.status_code == 409

        punch_out = api.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP9301", "punched_at": _ist_ms("2026-07-07", "06:35")},
        )
        assert punch_out.status_code == 200
        assert punch_out.json()["date"] == "2026-07-06"
        assert punch_out.json()["work_hours"] == 6.08
        assert punch_out.json()["overtime_minutes"] == 35
        assert punch_out.json()["half_day"] is False

        regularized = api.patch(
            "/attendance/EMP9301/2026-07-06",
            json={
                "punch_in": _ist_ms("2026-07-07", "00:35"),
                "punch_out": _ist_ms("2026-07-07", "06:50"),
                "reason": "corrected overnight times",
                "regularized_by": "night manager",
            },
        )
        assert regularized.status_code == 200, regularized.text
        assert regularized.json()["work_hours"] == 6.25
        assert regularized.json()["late_minutes"] == 155
        assert regularized.json()["overtime_minutes"] == 50
        assert len(regularized.json()["history"]) == 1

        wrong_attendance_date = api.patch(
            "/attendance/EMP9301/2026-07-06",
            json={
                "punch_in": _ist_ms("2026-07-07", "23:00"),
                "reason": "wrong shift date",
                "regularized_by": "night manager",
            },
        )
        assert wrong_attendance_date.status_code == 422
