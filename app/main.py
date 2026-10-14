
import logging
import os
import re
import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError
from pymongo.collection import ReturnDocument

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
AttendanceStatus = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def epoch_ms_to_datetime(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(422, "Timestamp must be an integer in epoch milliseconds")
    if not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise HTTPException(422, "Timestamp is outside the permitted range")
    return datetime.fromtimestamp(value // 1000, UTC)


def datetime_to_epoch_ms(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp()) * 1000


def trunc_seconds(value: datetime) -> datetime:
    return value.replace(microsecond=0)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def as_ist(value: datetime) -> datetime:
    return as_utc(value).astimezone(IST)


def round_half_up(value, places=2):
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def shift_datetime(day: date, shift_time: str) -> datetime:
    hour, minute = map(int, shift_time.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


def shift_start_for_punch(punch_in: datetime, employee: dict) -> datetime:
    local = as_ist(punch_in)
    day = local.date()
    start_text = employee["shift_start"]
    end_text = employee["shift_end"]
    start_h, start_m = map(int, start_text.split(":"))
    end_h, end_m = map(int, end_text.split(":"))
    overnight = (end_h, end_m) <= (start_h, start_m)

    if overnight and (local.hour, local.minute) < (end_h, end_m):
        day -= timedelta(days=1)

    return shift_datetime(day, start_text)


def attendance_date_for_punch(punch_in: datetime, employee: dict) -> str:
    local = as_ist(punch_in)
    start_h, start_m = map(int, employee["shift_start"].split(":"))
    end_h, end_m = map(int, employee["shift_end"].split(":"))
    overnight = (end_h, end_m) <= (start_h, start_m)

    day = local.date()
    if overnight and (local.hour, local.minute) < (end_h, end_m):
        day -= timedelta(days=1)

    return day.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str) -> int:
    local = as_ist(punch_in)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    overnight = (end_h, end_m) <= (start_h, start_m)

    day = local.date()
    if overnight and (local.hour, local.minute) < (end_h, end_m):
        day -= timedelta(days=1)

    start = shift_datetime(day, shift_start)
    if local <= start + timedelta(minutes=10):
        return 0

    elapsed_seconds = (local - start).total_seconds()
    return int(elapsed_seconds // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end: str, attendance_day: str,
                     shift_start: str) -> int:
    day = date.fromisoformat(attendance_day)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    end_day = day + (timedelta(days=1) if (eh, em) <= (sh, sm) else timedelta())
    end = shift_datetime(end_day, shift_end).astimezone(UTC)
    seconds = int((as_utc(punch_out) - end).total_seconds())
    return seconds // 60 if seconds >= 1800 else 0


def recompute_fields(doc: dict, employee: dict) -> dict:
    pin = doc.get("punch_in")
    pout = doc.get("punch_out")

    if pin is not None:
        doc["late_minutes"] = compute_late_minutes(
            pin, employee["shift_start"], employee["shift_end"]
        )
    else:
        doc["late_minutes"] = 0

    if pin is not None and pout is not None:
        hours = compute_work_hours(pin, pout)
        doc["work_hours"] = hours
        doc["overtime_minutes"] = compute_overtime(
            pout, employee["shift_end"], doc["date"], employee["shift_start"]
        )
        doc["half_day"] = hours < 4.50
    else:
        doc["work_hours"] = None
        doc["overtime_minutes"] = 0
        doc["half_day"] = False

    return doc


def attendance_response(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    for key in ("punch_in", "punch_out"):
        result[key] = datetime_to_epoch_ms(result.get(key))

    for entry in result.get("history", []):
        entry["at"] = datetime_to_epoch_ms(entry.get("at"))
        for change in entry.get("changes", {}).values():
            for side in ("from", "to"):
                if isinstance(change.get(side), datetime):
                    change[side] = datetime_to_epoch_ms(change[side])
    return result


def employee_response(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    if "created_at" in result:
        result["created_at"] = datetime_to_epoch_ms(result["created_at"])
    return result


def date_range(month: str):
    first = date.fromisoformat(month + "-01")
    last = date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])
    return first, last


def weekdays_between(first: date, last: date) -> int:
    count = 0
    current = first
    while current <= last:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def is_present_status(status: str) -> bool:
    return status in ("PRESENT", "WFH", "ON_DUTY")


def valid_month(month: str):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "month must be YYYY-MM")
    return date_range(month)


def validate_epoch(value):
    if value is not None:
        epoch_ms_to_datetime(value)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: date

    @model_validator(mode="after")
    def check_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: PresenceStatus = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def check_timestamp(cls, value):
        validate_epoch(value)
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def check_timestamp(cls, value):
        validate_epoch(value)
        return value


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[AttendanceStatus] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def check_timestamp(cls, value):
        validate_epoch(value)
        return value


# ---------------------------------------------------------------------------
# Startup / indexes
# ---------------------------------------------------------------------------

@app.on_event("startup")
def create_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)]
    )


# ---------------------------------------------------------------------------
# System and employees
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB is unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["joined_on"] = doc["joined_on"].isoformat()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_response(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    docs = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING)
    docs = docs.skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [employee_response(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    ts = trunc_seconds(epoch_ms_to_datetime(body.punched_at)) if body.punched_at is not None else now_utc()
    attendance_day = attendance_date_for_punch(ts, emp)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this attendance date")
    return attendance_response(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    ts = trunc_seconds(epoch_ms_to_datetime(body.punched_at)) if body.punched_at is not None else now_utc()

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": ts},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "No punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "This record is already punched out")

    elapsed = (ts - as_utc(record["punch_in"])).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 3600:
        raise HTTPException(422, "Punch-out must be after punch-in and within 24 hours")

    updated = dict(record)
    updated["punch_out"] = ts
    recompute_fields(updated, emp)

    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": updated["work_hours"],
            "overtime_minutes": updated["overtime_minutes"],
            "half_day": updated["half_day"],
        }},
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        raise HTTPException(409, "This record was punched out concurrently")
    return attendance_response(result)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[AttendanceStatus] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")

    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if status is not None:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from.isoformat()
        if date_to:
            q["date"]["$lte"] = date_to.isoformat()

    total = db.attendance_logs.count_documents(q)
    docs = db.attendance_logs.find(q).sort(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    )
    docs = docs.skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [attendance_response(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{attendance_date}")
def regularize_attendance(emp_code: str, attendance_date: date, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": attendance_date.isoformat()}
    )
    if record is None:
        raise HTTPException(404, "Attendance record not found")

    changes_requested = body.model_dump(exclude={"reason", "regularized_by"}, exclude_unset=True)
    if not changes_requested:
        raise HTTPException(422, "No changes supplied")

    old = dict(record)
    new = dict(record)
    for field, value in changes_requested.items():
        if field in ("punch_in", "punch_out"):
            value = epoch_ms_to_datetime(value) if value is not None else None
        if value is not None or field in body.model_fields_set:
            new[field] = value

    if new.get("status") in ("ABSENT", "LEAVE"):
        if "punch_in" in changes_requested or "punch_out" in changes_requested:
            raise HTTPException(422, "ABSENT and LEAVE cannot be supplied with punch times")
        new["punch_in"] = None
        new["punch_out"] = None
    elif is_present_status(new.get("status", "")) and new.get("punch_in") is None:
        raise HTTPException(422, "Presence status requires punch_in")

    if new.get("punch_in") is not None:
        expected_date = attendance_date_for_punch(new["punch_in"], emp)
        if expected_date != attendance_date.isoformat():
            raise HTTPException(422, "punch_in must belong to the record's attendance date")

    if new.get("punch_out") is not None:
        if new.get("punch_in") is None:
            raise HTTPException(422, "punch_out requires punch_in")
        seconds = (as_utc(new["punch_out"]) - as_utc(new["punch_in"])).total_seconds()
        if seconds <= 0 or seconds > 24 * 3600:
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    recompute_fields(new, emp)

    tracked = ("status", "punch_in", "punch_out", "work_hours", "late_minutes",
               "overtime_minutes", "half_day")
    actual_changes = {}
    for field in tracked:
        if old.get(field) != new.get(field):
            actual_changes[field] = {
                "from": old.get(field),
                "to": new.get(field),
            }

    if not actual_changes:
        raise HTTPException(422, "Request does not change the record")

    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": actual_changes,
    }

    history_match = {"$or": [
        {"history": record.get("history", [])},
        {"history": {"$exists": False}},
    ]}
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], **history_match},
        {
            "$set": {
                "status": new["status"],
                "punch_in": new.get("punch_in"),
                "punch_out": new.get("punch_out"),
                "work_hours": new["work_hours"],
                "late_minutes": new["late_minutes"],
                "overtime_minutes": new["overtime_minutes"],
                "half_day": new["half_day"],
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        raise HTTPException(409, "Record changed concurrently; retry the request")
    return attendance_response(result)


# ---------------------------------------------------------------------------
# Analytics helpers
# ---------------------------------------------------------------------------

def _working_day_expr(field):
    return {"$and": [
        {"$gte": [{"$dayOfWeek": field}, 2]},
        {"$lte": [{"$dayOfWeek": field}, 6]},
    ]}


def _rounded_positive_expr(value, places):
    scale = 10 ** places
    return {"$divide": [
        {"$floor": {"$add": [{"$multiply": [value, scale]}, 0.5]}},
        scale,
    ]}


def employee_monthly_pipeline(emp_code, first, last):
    day = {"$dateFromString": {"dateString": "$date"}}
    present = {"$and": [
        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
        _working_day_expr(day),
    ]}
    return [
        {"$match": {"emp_code": emp_code, "date": {
            "$gte": first.isoformat(), "$lte": last.isoformat()
        }}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                present, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]}, 0
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(first, last, department=None):
    employee_match = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        employee_match["department"] = department
    log_match = {"$expr": {"$and": [
        {"$eq": ["$emp_code", "$$emp_code"]},
        {"$gte": ["$date", first.isoformat()]},
        {"$lte": ["$date", last.isoformat()]},
    ]}}
    log_day = {"$dateFromString": {"dateString": "$logs.date"}}
    present = {"$and": [
        {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
        _working_day_expr(log_day),
    ]}
    qualifying_hours = {"$and": [
        {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
        {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
    ]}
    return [
        {"$match": employee_match},
        {"$lookup": {"from": "attendance_logs", "let": {"emp_code": "$emp_code"},
                      "pipeline": [{"$match": log_match}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$ne": [{"$ifNull": ["$logs.date", None]}, None]}, present]},
                {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1.0]}, 0
            ]}},
            "avg_work_hours": {"$avg": {"$cond": [qualifying_hours, "$logs.work_hours", None]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1,
                      "present_days": 1, "avg_work_hours": 1, "late_count": 1,
                      "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(first, last, department=None, limit=10):
    employee_match = {"$expr": {"$eq": ["$emp_code", "$$emp_code"]}}
    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
                     "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"},
                     "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "let": {"emp_code": "$_id"},
                      "pipeline": [{"$match": employee_match}], "as": "employee"}},
        {"$unwind": "$employee"},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
                               "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id",
                      "name": "$employee.name", "department": "$employee.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]


def department_trend_pipeline(department, from_date, to_date):
    start = datetime(from_date.year, from_date.month, from_date.day, tzinfo=UTC)
    end = datetime(to_date.year, to_date.month, to_date.day, tzinfo=UTC) + timedelta(days=1)
    real_logs = [
        {"$match": {"date": {"$gte": from_date.isoformat(), "$lte": to_date.isoformat()}}},
        {"$lookup": {"from": "employees", "let": {"code": "$emp_code"},
                      "pipeline": [{"$match": {"$expr": {"$and": [
                          {"$eq": ["$emp_code", "$$code"]}, {"$eq": ["$department", department]}
                      ]}}}], "as": "employee"}},
        {"$match": {"employee.0": {"$exists": True}}},
        {"$project": {"_day": {"$dateFromString": {"dateString": "$date"}},
                      "present_count": {"$cond": [
                          {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                          {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]}, 0
                      ]},
                      "late_count": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}},
    ]
    day = {"$dateToString": {"format": "%Y-%m-%d", "date": "$_day"}}
    working = _working_day_expr("$_day")
    return [
        {"$documents": [{"_day": start, "present_count": 0.0, "late_count": 0}]},
        {"$unionWith": {"coll": "attendance_logs", "pipeline": real_logs}},
        {"$group": {"_id": "$_day", "present_count": {"$sum": "$present_count"},
                     "late_count": {"$sum": "$late_count"}}},
        {"$project": {"_id": 0, "_day": "$_id", "present_count": 1, "late_count": 1}},
        {"$densify": {"field": "_day", "range": {"step": 1, "unit": "day",
                                                    "bounds": [start, end]}}},
        {"$set": {"present_count": {"$ifNull": ["$present_count", 0.0]},
                   "late_count": {"$ifNull": ["$late_count", 0]}}},
        {"$lookup": {"from": "employees", "let": {"day": day},
                      "pipeline": [{"$match": {"department": department,
                                                  "$expr": {"$lte": ["$joined_on", "$$day"]}}},
                                   {"$count": "value"}], "as": "headcount_docs"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$headcount_docs.value", 0]}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": [working, {"$gt": ["$headcount", 0]}]},
            _rounded_positive_expr({"$divide": ["$present_count", "$headcount"]}, 4), None
        ]}}},
        {"$setWindowFields": {"sortBy": {"_day": 1}, "output": {
            "moving_avg": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}
        }}},
        {"$project": {"_id": 0, "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_day"}},
                      "is_working_day": working, "headcount": 1, "present_count": 1, "late_count": 1,
                      "attendance_rate": 1,
                      "moving_avg_7d": {"$cond": [
                          {"$ne": ["$moving_avg", None]}, _rounded_positive_expr("$moving_avg", 4), None
                      ]}}},
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    first, last = valid_month(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    joined = date.fromisoformat(emp["joined_on"])
    working_start = max(first, joined)
    working_days = weekdays_between(working_start, last) if working_start <= last else 0

    result = next(iter(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, first, last))), {})
    present_days = result.get("present_days", 0.0)
    leave_days = result.get("leave_days", 0)
    late_count = result.get("late_count", 0)
    late_minutes = result.get("total_late_minutes", 0)
    overtime = result.get("total_overtime_minutes", 0)
    pct = round_half_up(present_days / working_days * 100, 2) if working_days else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": late_minutes,
        "total_overtime_minutes": overtime,
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    first, last = valid_month(month)
    employee_filter = {}
    if department is not None:
        employee_filter["department"] = department

    pipeline = department_summary_pipeline(first, last, department)
    pipeline.insert(-2, {"$set": {"avg_work_hours": {"$cond": [
        {"$ne": ["$avg_work_hours", None]},
        _rounded_positive_expr("$avg_work_hours", 2), None
    ]}}})
    return {"month": month, "items": list(db.employees.aggregate(pipeline))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = valid_month(month)
    return {"month": month, "items": list(db.attendance_logs.aggregate(
        late_leaderboard_pipeline(first, last, department, limit)
    ))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: date = Query(..., alias="from"),
                     to_date: date = Query(..., alias="to")):
    if to_date < from_date:
        raise HTTPException(422, "'to' must be on or after 'from'")
    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "Department not found")
    return {"department": department, "items": list(db.attendance_logs.aggregate(
        department_trend_pipeline(department, from_date, to_date)
    ))}


# ---------------------------------------------------------------------------
# Admin explain endpoint
# ---------------------------------------------------------------------------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[AttendanceStatus] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
):
    if endpoint == "attendance_list":
        q = {}
        if emp_code is not None:
            q["emp_code"] = emp_code
        if status is not None:
            q["status"] = status
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not be after date_to")
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from.isoformat()
            if date_to:
                q["date"]["$lte"] = date_to.isoformat()
        explain = db.command({
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        first, last = valid_month(month)
        pipeline = employee_monthly_pipeline(emp_code, first, last)
        explain = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        first, last = valid_month(month)
        pipeline = department_summary_pipeline(first, last, department)
        explain = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "employees", "explain": explain}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        first, last = valid_month(month)
        pipeline = late_leaderboard_pipeline(first, last, department, limit)
        explain = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from and to are required")
        if to_date < from_date or (to_date - from_date).days + 1 > 92:
            raise HTTPException(422, "Invalid trend date range")
        pipeline = department_trend_pipeline(department, from_date, to_date)
        explain = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    raise HTTPException(422, "Unknown endpoint")
