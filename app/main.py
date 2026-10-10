"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (real environment variables win over .env)
"""
from __future__ import annotations

import calendar
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
VALID_STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def parse_hhmm(value: str) -> time:
    h, m = map(int, value.split(":"))
    return time(hour=h, minute=m)


def parse_date_str(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def truncate_seconds(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.replace(microsecond=0)


def ms_to_datetime(value: int) -> datetime:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("epoch milliseconds must be an integer")
    if value < 100000000000 or value > 4102444800000:
        raise ValueError("epoch milliseconds out of range")
    dt = datetime.fromtimestamp(value / 1000, tz=UTC)
    return truncate_seconds(dt)


def datetime_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def get_ist_datetime(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return truncate_seconds(dt.astimezone(IST))


def get_record_date_for_punch_in(punch_in_dt: datetime, shift_start: str, shift_end: str) -> date:
    punch_in_ist = get_ist_datetime(punch_in_dt)
    shift_end_t = parse_hhmm(shift_end)
    if shift_end_t <= parse_hhmm(shift_start):
        if punch_in_ist.time() < shift_end_t:
            return (punch_in_ist.date() - timedelta(days=1))
    return punch_in_ist.date()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, record_date: Optional[str] = None) -> int:
    punch_in_ist = get_ist_datetime(punch_in_dt)
    shift_start_dt = datetime.combine(parse_date_str(record_date) if record_date else punch_in_ist.date(), parse_hhmm(shift_start), tzinfo=IST)
    elapsed_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    if elapsed_seconds <= 600:
        return 0
    return int(elapsed_seconds // 60)


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    seconds = (truncate_seconds(punch_out_dt) - truncate_seconds(punch_in_dt)).total_seconds()
    hours = (Decimal(str(seconds)) / Decimal("3600")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime_minutes(punch_out_dt: datetime, shift_end: str, record_date: Optional[str] = None) -> int:
    shift_end_t = parse_hhmm(shift_end)
    punch_out_ist = get_ist_datetime(punch_out_dt)
    if record_date is None:
        record_date = punch_out_ist.date().isoformat()
    record_day = parse_date_str(record_date)
    day_to_use = record_day
    if shift_end_t <= parse_hhmm("00:00"):
        pass
    # Determine shift end datetime according to overnight rule.
    shift_end_dt = datetime.combine(day_to_use, shift_end_t, tzinfo=IST)
    if shift_end_t <= parse_hhmm("00:00"):
        shift_end_dt += timedelta(days=1)
    if shift_end_t > parse_hhmm("00:00"):
        end_is_overnight = False
    else:
        end_is_overnight = True
    if shift_end_t <= parse_hhmm("00:00"):
        # explicit overnight is represented by shift_end <= shift_start; shift end falls on next day
        pass
    # Recompute with employee shift semantics.
    if shift_end_t <= parse_hhmm("00:00"):
        shift_end_dt = datetime.combine(record_day + timedelta(days=1), shift_end_t, tzinfo=IST)
    else:
        shift_end_dt = datetime.combine(record_day, shift_end_t, tzinfo=IST)
    diff_min = int((punch_out_ist - shift_end_dt).total_seconds() / 60)
    return diff_min if diff_min >= 30 else 0


def compute_overtime_minutes_for_shift(punch_out_dt: datetime, shift_end: str, record_date: str, shift_start: str) -> int:
    shift_end_t = parse_hhmm(shift_end)
    shift_start_t = parse_hhmm(shift_start)
    record_day = parse_date_str(record_date)
    if shift_end_t <= shift_start_t:
        shift_end_dt = datetime.combine(record_day + timedelta(days=1), shift_end_t, tzinfo=IST)
    else:
        shift_end_dt = datetime.combine(record_day, shift_end_t, tzinfo=IST)
    diff_min = int((get_ist_datetime(punch_out_dt) - shift_end_dt).total_seconds() / 60)
    return diff_min if diff_min >= 30 else 0


def is_weekday(date_value: str) -> bool:
    return parse_date_str(date_value).weekday() < 5


def quantize_decimal(value: Decimal, places: str) -> Decimal:
    return value.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def round_work_hours(value: Decimal) -> Decimal:
    return quantize_decimal(value, "0.01")


def round_rate(value: Decimal) -> Decimal:
    return quantize_decimal(value, "0.0001")


def safe_float(value: Optional[Decimal]) -> Optional[float]:
    if value is None:
        return None
    return float(value)


def serialize_history(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "at": datetime_to_ms(doc.get("at")),
        "by": doc.get("by"),
        "reason": doc.get("reason"),
        "changes": {
            key: {
                "from": datetime_to_ms(val["from"]) if isinstance(val.get("from"), datetime) else val.get("from"),
                "to": datetime_to_ms(val["to"]) if isinstance(val.get("to"), datetime) else val.get("to"),
            }
            for key, val in (doc.get("changes") or {}).items()
        },
    }


def serialize_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": datetime_to_ms(doc.get("punch_in")),
        "punch_out": datetime_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history(entry) for entry in (doc.get("history") or [])],
    }


def serialize_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_ms(doc["created_at"]),
    }


def expected_month_bounds(month: str) -> Tuple[str, str]:
    year, mon = month.split("-")
    year_i = int(year)
    month_i = int(mon)
    last_day = calendar.monthrange(year_i, month_i)[1]
    return f"{year}-{mon}-01", f"{year}-{mon}-{last_day:02d}"


def month_range_from_str(month: str) -> Tuple[date, date]:
    dt = datetime.strptime(month, "%Y-%m")
    first = dt.replace(day=1).date()
    last = (first.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return first, last


def to_decimal_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    return float(value)


def build_derived_for_fields(status: str, punch_in_dt: Optional[datetime], punch_out_dt: Optional[datetime], shift_start: str, shift_end: str, record_date: str) -> Dict[str, Any]:
    derived = {
        "late_minutes": 0,
        "overtime_minutes": 0,
        "work_hours": None,
        "half_day": False,
    }
    if status in PRESENCE_STATUSES and punch_in_dt is not None:
        derived["late_minutes"] = compute_late_minutes(punch_in_dt, shift_start, record_date)
        if punch_out_dt is not None:
            derived["work_hours"] = compute_work_hours(punch_in_dt, punch_out_dt)
            derived["overtime_minutes"] = compute_overtime_minutes_for_shift(punch_out_dt, shift_end, record_date, shift_start)
            if derived["work_hours"] is not None:
                derived["half_day"] = Decimal(str(derived["work_hours"])) < Decimal("4.50")
    return derived


def ensure_indexes() -> None:
    db.employees.create_index("emp_code", unique=True, name="u_emp_code")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="i_department_emp")
    db.employees.create_index([("joined_on", 1), ("department", 1)], name="i_joined_department")

    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="u_emp_date")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="i_date_emp")
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", 1)], name="i_emp_punchin")
    db.attendance_logs.create_index([("status", 1), ("date", 1), ("emp_code", 1)], name="i_status_date_emp")
    db.attendance_logs.create_index([("late_minutes", 1), ("emp_code", 1), ("date", 1)], name="i_late_emp_date")


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            parse_date_str(value)
            return value
        except ValueError as exc:
            raise ValueError("joined_on must be YYYY-MM-DD") from exc

    @model_validator(mode="after")
    def validate_shift_range(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return value
        try:
            ms_to_datetime(value)
        except ValueError as exc:
            raise ValueError("punched_at must be an integer epoch millisecond value") from exc
        return value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in PRESENCE_STATUSES:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY for punch-in")
        return value


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return value
        try:
            ms_to_datetime(value)
        except ValueError as exc:
            raise ValueError("punched_at must be an integer epoch millisecond value") from exc
        return value


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if value not in VALID_STATUSES:
            raise ValueError("status must be one of PRESENT, ABSENT, LEAVE, WFH, ON_DUTY")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_epoch(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return value
        try:
            ms_to_datetime(value)
        except ValueError as exc:
            raise ValueError("time values must be integer epoch milliseconds") from exc
        return value


# --------------------------------------------------------------------------- #
# Route helpers
# --------------------------------------------------------------------------- #
def _employee_or_404(emp_code: str) -> Dict[str, Any]:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    return emp


def _validate_query_date(value: Optional[str], name: str) -> Optional[date]:
    if value is None:
        return None
    try:
        return parse_date_str(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{name} must be YYYY-MM-DD") from exc


def _validate_month(value: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m")
        return value
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="month must be YYYY-MM") from exc


@app.on_event("startup")
def startup_events() -> None:
    ensure_indexes()


@app.get("/health")
def health() -> Dict[str, str]:
    try:
        db.command("ping")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="MongoDB unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate) -> Dict[str, Any]:
    payload = body.model_dump()
    created_at = datetime.now(tz=UTC)
    payload["created_at"] = created_at
    try:
        result = db.employees.insert_one(payload)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="employee already exists") from exc
    doc = db.employees.find_one({"_id": result.inserted_id})
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> Dict[str, Any]:
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = [serialize_employee(doc) for doc in db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest) -> Dict[str, Any]:
    emp = _employee_or_404(body.emp_code)
    punched_at_dt = ms_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(tz=UTC)
    attendance_date = get_record_date_for_punch_in(punched_at_dt, emp["shift_start"], emp["shift_end"])
    attendance_key = attendance_date.isoformat()
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": attendance_key}):
        raise HTTPException(status_code=409, detail="already punched in for this date")
    late_minutes = compute_late_minutes(punched_at_dt, emp["shift_start"], attendance_key)
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_key,
        "status": body.status,
        "punch_in": truncate_seconds(punched_at_dt),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="already punched in for this date") from exc
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest) -> Dict[str, Any]:
    emp = _employee_or_404(body.emp_code)
    punched_at_dt = ms_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(tz=UTC)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": truncate_seconds(punched_at_dt)}, "punch_out": None},
        sort=[("punch_in", -1)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no open punch-in found")
    if record["status"] not in PRESENCE_STATUSES:
        raise HTTPException(status_code=404, detail="no open punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")
    punch_in_dt = truncate_seconds(record["punch_in"])
    if punched_at_dt <= punch_in_dt:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
    if (punched_at_dt - punch_in_dt) > timedelta(hours=24):
        raise HTTPException(status_code=422, detail="punch_out is more than 24 hours after punch_in")
    derived = build_derived_for_fields(record["status"], punch_in_dt, punched_at_dt, emp["shift_start"], emp["shift_end"], record["date"])
    update = {
        "punch_out": truncate_seconds(punched_at_dt),
        "work_hours": derived["work_hours"],
        "overtime_minutes": derived["overtime_minutes"],
        "half_day": bool(derived["half_day"]),
    }
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        raise HTTPException(status_code=409, detail="already punched out")
    return serialize_record(result)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> Dict[str, Any]:
    q: Dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = parse_date_str(date_from).isoformat()
        if date_to is not None:
            q["date"]["$lte"] = parse_date_str(date_to).isoformat()
        if q["date"].get("$gte") and q["date"].get("$lte"):
            if parse_date_str(date_from) > parse_date_str(date_to):
                raise HTTPException(status_code=422, detail="date_from must be <= date_to")
    if status is not None:
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    items = [serialize_record(doc) for doc in db.attendance_logs.find(q, {"_id": 0}).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest) -> Dict[str, Any]:
    _employee_or_404(emp_code)
    target_date = parse_date_str(date)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": target_date.isoformat()})
    if record is None:
        raise HTTPException(status_code=404, detail="attendance record not found")
    original = {key: record.get(key) for key in ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]}
    proposed_status = body.status if body.status is not None else record.get("status")
    proposed_punch_in = record.get("punch_in") if body.punch_in is None else ms_to_datetime(body.punch_in)
    proposed_punch_out = record.get("punch_out") if body.punch_out is None else ms_to_datetime(body.punch_out)

    if proposed_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="ABSENT and LEAVE records cannot include punch times")
        proposed_punch_in = None
        proposed_punch_out = None
    else:
        if proposed_punch_in is None:
            if record.get("punch_in") is None:
                raise HTTPException(status_code=422, detail="presence status requires punch_in")
            proposed_punch_in = record.get("punch_in")
        if proposed_punch_out is not None and proposed_punch_in is None:
            raise HTTPException(status_code=422, detail="presence status requires punch_in")
        if proposed_punch_in is not None:
            emp = _employee_or_404(emp_code)
            expected_date = get_record_date_for_punch_in(proposed_punch_in, emp["shift_start"], emp["shift_end"])
            if expected_date.isoformat() != date:
                raise HTTPException(status_code=422, detail="punch_in must remain on the attendance date")
            if proposed_punch_out is not None:
                if proposed_punch_out <= proposed_punch_in:
                    raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
                if proposed_punch_out - proposed_punch_in > timedelta(hours=24):
                    raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")
    new_status = proposed_status
    if new_status in {"ABSENT", "LEAVE"}:
        new_punch_in = None
        new_punch_out = None
    else:
        new_punch_in = proposed_punch_in
        new_punch_out = proposed_punch_out
    if new_punch_in is None:
        new_late = 0
    else:
        emp = _employee_or_404(emp_code)
        new_late = compute_late_minutes(new_punch_in, emp["shift_start"], date)
    work_hours = None
    overtime = 0
    half_day = False
    if new_punch_in is not None and new_punch_out is not None:
        work_hours = compute_work_hours(new_punch_in, new_punch_out)
        overtime = compute_overtime_minutes_for_shift(new_punch_out, emp["shift_end"], date, emp["shift_start"])
        half_day = Decimal(str(work_hours)) < Decimal("4.50")
    updated = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": work_hours,
        "late_minutes": new_late,
        "overtime_minutes": overtime,
        "half_day": half_day,
    }
    changes = {}
    for key in ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]:
        old_value = original.get(key)
        new_value = updated.get(key)
        if old_value != new_value:
            changes[key] = {"from": old_value, "to": new_value}
    if not changes:
        raise HTTPException(status_code=422, detail="no changes applied")
    entry = {
        "at": datetime.now(tz=UTC),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": {k: {"from": v["from"], "to": v["to"]} for k, v in changes.items()},
    }
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": record.get("history", [])},
        {"$set": {"status": new_status, "punch_in": new_punch_in, "punch_out": new_punch_out, "work_hours": work_hours, "late_minutes": new_late, "overtime_minutes": overtime, "half_day": half_day}, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        raise HTTPException(status_code=409, detail="concurrent update conflict")
    return serialize_record(result)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)) -> Dict[str, Any]:
    _employee_or_404(emp_code)
    _validate_month(month)
    first_day, last_day = month_range_from_str(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_day.isoformat(), "$lte": last_day.isoformat()}}},
        {"$group": {
            "_id": "$emp_code",
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "present_days": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}}
        }}
    ]
    row = db.attendance_logs.aggregate(pipeline)
    result = next(row, None)
    joined = db.employees.find_one({"emp_code": emp_code})
    working_days = 0
    for single_date in [first_day + timedelta(days=i) for i in range((last_day - first_day).days + 1)]:
        if single_date.weekday() < 5 and single_date.isoformat() >= joined["joined_on"]:
            working_days += 1
    present_days = 0.0
    if result is not None:
        for single_date in [first_day + timedelta(days=i) for i in range((last_day - first_day).days + 1)]:
            if single_date.weekday() >= 5:
                continue
            recs = list(db.attendance_logs.find({"emp_code": emp_code, "date": single_date.isoformat()}))
            if not recs:
                continue
            rec = recs[0]
            if rec.get("status") in PRESENCE_STATUSES:
                present_days += 0.5 if rec.get("half_day") else 1.0
    attendance_pct = None if working_days == 0 else (Decimal(str(present_days)) / Decimal(str(working_days)) * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int((result or {}).get("leave_days", 0) or 0),
        "late_count": int((result or {}).get("late_count", 0) or 0),
        "total_late_minutes": int((result or {}).get("total_late_minutes", 0) or 0),
        "total_overtime_minutes": int((result or {}).get("total_overtime_minutes", 0) or 0),
        "attendance_pct": None if working_days == 0 else float(attendance_pct),
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None) -> Dict[str, Any]:
    _validate_month(month)
    first_day, last_day = month_range_from_str(month)
    departments = []
    if department is not None:
        employees = list(db.employees.find({"department": department, "joined_on": {"$lte": last_day.isoformat()}}))
        if not employees:
            return {"month": month, "items": []}
        departments = [department]
    else:
        employees = list(db.employees.find({"joined_on": {"$lte": last_day.isoformat()}}))
        departments = sorted({emp["department"] for emp in employees})

    results = []
    for dept in departments:
        dept_employees = [emp for emp in employees if emp["department"] == dept] if department is None else [emp for emp in employees if emp["department"] == dept]
        headcount = len(dept_employees)
        if headcount == 0:
            continue
        rows = list(db.attendance_logs.find({"date": {"$gte": first_day.isoformat(), "$lte": last_day.isoformat()}, "emp_code": {"$in": [emp["emp_code"] for emp in dept_employees]}}))
        present_days = sum((0.5 if rec.get("half_day") else 1.0) for rec in rows if rec.get("status") in PRESENCE_STATUSES and rec.get("date", "") and parse_date_str(rec["date"]).weekday() < 5)
        avg_work = [Decimal(str(rec["work_hours"])) for rec in rows if rec.get("status") in PRESENCE_STATUSES and rec.get("work_hours") is not None]
        avg = None if not avg_work else float((sum(avg_work) / Decimal(len(avg_work))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        late_count = sum(1 for rec in rows if rec.get("late_minutes", 0) > 0)
        total_late = sum(rec.get("late_minutes", 0) for rec in rows)
        leave_count = sum(1 for rec in rows if rec.get("status") == "LEAVE")
        on_duty_count = sum(1 for rec in rows if rec.get("status") == "ON_DUTY")
        results.append({
            "department": dept,
            "headcount": headcount,
            "present_days": present_days,
            "avg_work_hours": avg,
            "late_count": late_count,
            "total_late_minutes": total_late,
            "leave_count": leave_count,
            "on_duty_count": on_duty_count,
        })
    results.sort(key=lambda x: x["department"])
    return {"month": month, "items": results}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(...), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None) -> Dict[str, Any]:
    _validate_month(month)
    first_day, last_day = month_range_from_str(month)
    pipeline = [
        {"$match": {"date": {"$gte": first_day.isoformat(), "$lte": last_day.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    employee_map = {emp["emp_code"]: emp for emp in db.employees.find({})}
    if department is not None:
        rows = [row for row in rows if employee_map.get(row["_id"]) and employee_map[row["_id"]]["department"] == department]
    items = []
    for row in rows:
        emp = employee_map.get(row["_id"])
        if emp is None:
            continue
        items.append({
            "emp_code": row["_id"],
            "name": emp["name"],
            "department": emp["department"],
            "total_late_minutes": row["total_late_minutes"],
            "late_count": row["late_count"],
        })
    items.sort(key=lambda x: (-x["total_late_minutes"], x["emp_code"]))
    ranked = []
    last_total = None
    last_rank = 0
    for idx, item in enumerate(items, start=1):
        if last_total is None or item["total_late_minutes"] != last_total:
            last_total = item["total_late_minutes"]
            last_rank = idx
        item["rank"] = last_rank
        ranked.append(item)
    filtered = [item for item in ranked if item["rank"] <= limit]
    return {"month": month, "items": [{"rank": item["rank"], "emp_code": item["emp_code"], "name": item["name"], "department": item["department"], "total_late_minutes": item["total_late_minutes"], "late_count": item["late_count"]} for item in filtered]}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")) -> Dict[str, Any]:
    start = _validate_query_date(from_date, "from")
    end = _validate_query_date(to_date, "to")
    if end < start:
        raise HTTPException(status_code=422, detail="from must be <= to")
    if (end - start).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range must not exceed 92 days")
    employees = list(db.employees.find({"department": department, "joined_on": {"$lte": end.isoformat()}}))
    if not employees:
        raise HTTPException(status_code=404, detail="department not found")
    rows = []
    d = start
    while d <= end:
        rows.append({"date": d.isoformat(), "is_working_day": d.weekday() < 5})
        d += timedelta(days=1)
    attendance_by_date = {}
    emp_codes = {emp["emp_code"] for emp in employees}
    for rec in db.attendance_logs.find({"emp_code": {"$in": list(emp_codes)}, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}):
        attendance_by_date.setdefault(rec["date"], []).append(rec)
    result = []
    for day in rows:
        day_str = day["date"]
        headcount = sum(1 for emp in employees if emp["joined_on"] <= day_str)
        recs = attendance_by_date.get(day_str, [])
        present_count = sum((0.5 if rec.get("half_day") else 1) for rec in recs if rec.get("status") in PRESENCE_STATUSES)
        late_count = sum(1 for rec in recs if rec.get("late_minutes", 0) > 0)
        attendance_rate = None
        if day["is_working_day"] and headcount > 0:
            attendance_rate = (Decimal(str(present_count)) / Decimal(str(headcount))).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            if present_count == 0:
                attendance_rate = Decimal("0.0000")
        result.append({
            "date": day_str,
            "is_working_day": day["is_working_day"],
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": None if attendance_rate is None else float(attendance_rate),
            "moving_avg_7d": None,
        })
    window = []
    for item in result:
        window.append(item)
        if len(window) > 7:
            window.pop(0)
        rates = [entry["attendance_rate"] for entry in window if entry["attendance_rate"] is not None]
        if not rates:
            item["moving_avg_7d"] = None
        else:
            avg = (sum(Decimal(str(rate)) for rate in rates) / Decimal(len(rates))).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            item["moving_avg_7d"] = float(avg)
    return {"department": department, "items": result}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None, limit: Optional[int] = Query(None, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None, from_date: Optional[str] = Query(None, alias="from"), to_date: Optional[str] = Query(None, alias="to"), page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)) -> Dict[str, Any]:
    if endpoint == "attendance_list":
        q: Dict[str, Any] = {}
        if emp_code is not None:
            q["emp_code"] = emp_code
        if date_from is not None or date_to is not None:
            q["date"] = {}
            if date_from is not None:
                q["date"]["$gte"] = parse_date_str(date_from).isoformat()
            if date_to is not None:
                q["date"]["$lte"] = parse_date_str(date_to).isoformat()
        if status is not None:
            q["status"] = status
        explain = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
    if endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        first_day, last_day = month_range_from_str(_validate_month(month))
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": first_day.isoformat(), "$lte": last_day.isoformat()}}},
            {"$group": {"_id": "$emp_code", "present_days": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}, "total_late_minutes": {"$sum": "$late_minutes"}, "total_overtime_minutes": {"$sum": "$overtime_minutes"}, "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}}}}
        ]
        explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
    if endpoint == "department_summary":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        first_day, last_day = month_range_from_str(_validate_month(month))
        query = {"date": {"$gte": first_day.isoformat(), "$lte": last_day.isoformat()}}
        if department is not None:
            dep_emp_codes = [emp["emp_code"] for emp in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]
            query["emp_code"] = {"$in": dep_emp_codes}
        pipeline = [
            {"$match": query},
            {"$group": {"_id": "$emp_code", "present_days": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}, "total_late_minutes": {"$sum": "$late_minutes"}, "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}}, "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}}}}
        ]
        explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
    if endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        first_day, last_day = month_range_from_str(_validate_month(month))
        pipeline = [
            {"$match": {"date": {"$gte": first_day.isoformat(), "$lte": last_day.isoformat()}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
        ]
        explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
    if endpoint == "department_trend":
        if department is None or from_date is None or to_date is None:
            raise HTTPException(status_code=422, detail="department, from and to are required")
        start = parse_date_str(from_date)
        end = parse_date_str(to_date)
        if end < start:
            raise HTTPException(status_code=422, detail="from must be <= to")
        if (end - start).days + 1 > 92:
            raise HTTPException(status_code=422, detail="range exceeds 92 days")
        pipeline = [
            {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
            {"$group": {"_id": "$date", "present_count": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}}},
            {"$sort": {"_id": 1}}
        ]
        explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
    raise HTTPException(status_code=422, detail="unsupported endpoint")


