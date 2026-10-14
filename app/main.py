"""Employee Attendance & Analytics API - reviewed implementation."""

import os, re, calendar
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import json_util

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(
    MONGO_URI, tz_aware=True, tzinfo=timezone.utc, serverSelectionTimeoutMS=3000
)
db = client[MONGO_DB]
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
EPOCH_MIN, EPOCH_MAX = 100_000_000_000, 4_102_444_800_000


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)


def trunc_dt(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def epoch_ms(dt):
    if dt is None:
        return None
    return int(trunc_dt(dt).timestamp() * 1000)


def from_ms(value: int) -> datetime:
    if type(value) is not int or not EPOCH_MIN <= value <= EPOCH_MAX:
        raise ValueError(
            "must be an integer epoch-millisecond timestamp in the supported range"
        )
    return datetime.fromtimestamp(value / 1000, timezone.utc).replace(microsecond=0)


def half_up(value, places=2):
    if value is None:
        return None
    return float(
        Decimal(str(value)).quantize(
            Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP
        )
    )


def parse_date(s):
    try:
        d = date.fromisoformat(s)
        if d.isoformat() != s:
            raise ValueError()
        return d
    except Exception:
        raise HTTPException(422, "date must be YYYY-MM-DD")


def month_bounds(month):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    y, m = map(int, month.split("-"))
    last = calendar.monthrange(y, m)[1]
    return date(y, m, 1), date(y, m, last)


def shift_dt(day: date, hhmm: str):
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(day, time(h, m), IST)


def attendance_date(punch_in, emp):
    local = punch_in.astimezone(IST)
    d = local.date()

    if (
        emp["shift_end"] <= emp["shift_start"]
        and local.strftime("%H:%M") < emp["shift_end"]
    ):
        d -= timedelta(days=1)

    return d.isoformat()


def shift_end_dt(att_date: str, emp: dict) -> datetime:
    day = parse_date(att_date)
    end = shift_dt(day, emp["shift_end"])

    if emp["shift_end"] <= emp["shift_start"]:
        end += timedelta(days=1)

    return end


def compute_late_minutes(punch_in: datetime, emp: dict, att_date: str) -> int:
    # PyMongo naive datetimes represent UTC.
    if punch_in.tzinfo is None:
        punch_utc = punch_in.replace(tzinfo=timezone.utc)
    else:
        punch_utc = punch_in.astimezone(timezone.utc)

    punch_ist = punch_utc.astimezone(IST)

    start_h, start_m = map(int, emp["shift_start"].split(":"))
    end_h, end_m = map(int, emp["shift_end"].split(":"))

    shift_day = parse_date(att_date)
    start = datetime(
        shift_day.year, shift_day.month, shift_day.day, start_h, start_m, tzinfo=IST
    )

    # Early-morning punches on overnight shifts belong
    # to the previous day's shift.
    if (end_h, end_m) <= (start_h, start_m):
        if (punch_ist.hour, punch_ist.minute) < (end_h, end_m):
            start -= timedelta(days=1)

    elapsed_seconds = (punch_ist - start).total_seconds()

    # Strictly more than 10 minutes late.
    if elapsed_seconds > 10 * 60:
        return int(elapsed_seconds // 60)

    return 0


def derived_fields(punch_in, punch_out, status, emp, att_date):
    if status not in PRESENCE:
        return {
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    late = compute_late_minutes(punch_in, emp, att_date) if punch_in else 0
    if not punch_out or not punch_in:
        return {
            "work_hours": None,
            "late_minutes": late,
            "overtime_minutes": 0,
            "half_day": False,
        }
    seconds = (punch_out - punch_in).total_seconds()
    hours = half_up(seconds / 3600, 2)
    punch_out_ist = (
        punch_out.replace(tzinfo=timezone.utc).astimezone(IST)
        if punch_out.tzinfo is None
        else punch_out.astimezone(IST)
    )

    diff = (punch_out_ist - shift_end_dt(att_date, emp)).total_seconds() / 60

    overtime = int(diff // 1) if diff >= 30 else 0

    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": hours < 4.50,
    }
    # overtime=int(diff//1) if diff >= 30 else 0
    # return {"work_hours":hours,"late_minutes":late,"overtime_minutes":overtime,"half_day":hours < 4.50}


def public_employee(doc):
    return {
        k: (epoch_ms(v) if k == "created_at" else v)
        for k, v in doc.items()
        if k != "_id"
    }


def public_attendance(doc):
    d = {k: v for k, v in doc.items() if k != "_id"}
    d.setdefault("punch_in", None)
    d.setdefault("punch_out", None)
    d.setdefault("work_hours", None)
    d["punch_in"] = epoch_ms(d["punch_in"])
    d["punch_out"] = epoch_ms(d["punch_out"])
    d.setdefault("late_minutes", 0)
    d.setdefault("overtime_minutes", 0)
    d.setdefault("half_day", False)
    d.setdefault("history", [])
    for h in d["history"]:
        h["at"] = epoch_ms(h.get("at"))
        for fld in ("punch_in", "punch_out"):
            if fld in h.get("changes", {}):
                for side in ("from", "to"):
                    h["changes"][fld][side] = epoch_ms(h["changes"][fld].get(side))
    return d


def date_range_query(date_from, date_to):
    q = {}
    if date_from:
        parse_date(date_from)
        q["$gte"] = date_from
    if date_to:
        parse_date(date_to)
        q["$lte"] = date_to
    return q


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_join_date(cls, v):
        try:
            if date.fromisoformat(v).isoformat() != v:
                raise ValueError()
        except Exception:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def different_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(StrictModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_ts(cls, v):
        if v is not None:
            from_ms(v)
        return v


class PunchOutIn(StrictModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def valid_ts(cls, v):
        if v is not None:
            from_ms(v)
        return v


class RegularizeIn(StrictModel):
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_ts(cls, v):
        if v is not None:
            from_ms(v)
        return v


@asynccontextmanager
async def lifespan(app):
    client.admin.command("ping")
    db.employees.create_index(
        [("emp_code", ASCENDING)], unique=True, name="uq_employee_code"
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined"
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_emp"
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_status_date",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="employee_punch_in"
    )
    yield
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan
)


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return public_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    items = [
        public_employee(x)
        for x in db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    ts = trunc_dt(
        from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    )
    d = attendance_date(ts, emp)
    fields = derived_fields(ts, None, body.status, emp, d)
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **fields,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return public_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    ts = trunc_dt(
        from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    )
    doc = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "status": {"$in": list(PRESENCE)},
            "punch_in": {"$ne": None, "$lte": ts},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if not doc:
        raise HTTPException(404, "no punch-in found")
    if doc.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if ts <= doc["punch_in"] or ts > doc["punch_in"] + timedelta(hours=24):
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    vals = derived_fields(doc["punch_in"], ts, doc["status"], emp, doc["date"])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": doc["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **vals}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return public_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    dr = date_range_query(date_from, date_to)
    if dr:
        q["date"] = dr
    total = db.attendance_logs.count_documents(q)
    docs = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [public_attendance(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not old:
        raise HTTPException(404, "attendance record not found")
    status = body.status if body.status is not None else old["status"]
    if status not in PRESENCE and (
        body.punch_in is not None or body.punch_out is not None
    ):
        raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
    pin = (
        trunc_dt(from_ms(body.punch_in))
        if body.punch_in is not None
        else old.get("punch_in")
    )
    pout = (
        trunc_dt(from_ms(body.punch_out))
        if body.punch_out is not None
        else old.get("punch_out")
    )
    if status not in PRESENCE:
        pin = pout = None
    elif pin is None:
        raise HTTPException(422, "presence status requires punch_in")
    if pin is not None and attendance_date(pin, emp) != date:
        raise HTTPException(422, "punch_in must belong to the record attendance date")
    if pout is not None and (
        pin is None or pout <= pin or pout > pin + timedelta(hours=24)
    ):
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    derived = derived_fields(pin, pout, status, emp, date)
    old_values = {
        "status": old.get("status"),
        "punch_in": old.get("punch_in"),
        "punch_out": old.get("punch_out"),
        **{
            k: old.get(k, False if k == "half_day" else 0)
            for k in ("work_hours", "late_minutes", "overtime_minutes", "half_day")
        },
    }
    new_values = {"status": status, "punch_in": pin, "punch_out": pout, **derived}
    changes = {}
    for k, v in new_values.items():
        ov = old_values.get(k)
        if ov != v:
            changes[k] = {"from": ov, "to": v}
    if not changes:
        raise HTTPException(422, "request does not change the record")
    entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    update = {"$set": new_values, "$push": {"history": entry}}
    # Compare-and-set the old history length to prevent concurrent edits losing audit entries.
    old_history = old.get("history", [])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": old["_id"], "history": {"$size": len(old_history)}},
        update,
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record changed concurrently; retry")
    return public_attendance(updated)


# Aggregation-based analytics: use stored derived fields as the source of truth.
def present_expr():
    return {"$in": ["$status", list(PRESENCE)]}


def weekday_count(start: date, end: date):
    if start > end:
        return 0
    days = (end - start).days + 1
    weeks, rem = divmod(days, 7)
    count = weeks * 5
    for i in range(rem):
        if (start.weekday() + i) % 7 < 5:
            count += 1
    return count


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    working = weekday_count(max(start, parse_date(emp["joined_on"])), end)
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    present_expr(),
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]
    a = list(db.attendance_logs.aggregate(pipeline))
    x = a[0] if a else {}
    pct = half_up(x.get("present_days", 0) / working * 100, 4) if working else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working,
        "present_days": half_up(x.get("present_days", 0), 2),
        "leave_days": x.get("leave_days", 0),
        "late_count": x.get("late_count", 0),
        "total_late_minutes": x.get("total_late_minutes", 0),
        "total_overtime_minutes": x.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start, end = month_bounds(month)
    match = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None:
        match["department"] = department
    pipeline = [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code", "joined": "$joined_on"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start.isoformat()]},
                                    {"$lte": ["$date", end.isoformat()]},
                                    {"$gte": ["$date", "$$joined"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE)]},
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": {
                                                            "$ifNull": [
                                                                "$logs.date",
                                                                start.isoformat(),
                                                            ]
                                                        }
                                                    }
                                                }
                                            },
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "work_hours": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE)]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            None,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": {"$size": "$headcount"},
                "present_days": 1,
                "avg_work_hours": {"$avg": "$work_hours"},
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]
    out = []
    for x in db.employees.aggregate(pipeline):
        if x["headcount"]:
            x.pop("_id", None)
            x["present_days"] = half_up(x.get("present_days", 0), 2)
            x["avg_work_hours"] = half_up(x.get("avg_work_hours"), 2)
            out.append(x)
    return {"month": month, "items": out}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None
):
    start, end = month_bounds(month)
    pipeline = [
        {
            "$match": {
                "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str, from_: str = Query(alias="from"), to: str = Query(...)
):
    start = parse_date(from_)
    end = parse_date(to)
    if end < start:
        raise HTTPException(422, "to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "unknown department")
    pipeline = [
        {"$match": {"department": department, "joined_on": {"$lte": end.isoformat()}}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start.isoformat()]},
                                    {"$lte": ["$date", end.isoformat()]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": {"$ifNull": ["$logs.date", None]},
                "emp_codes": {"$addToSet": "$emp_code"},
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$logs.status", list(PRESENCE)]},
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$match": {"_id": {"$ne": None}}},
        {"$project": {"date": "$_id", "present_count": 1, "late_count": 1}},
    ]
    # Build day rows in MongoDB with $documents + $densify, then join per-day aggregates.
    # $documents is supported by MongoDB 5.1+; no Python loop is used to generate dates.
    pipeline = [
        {
            "$documents": [
                {
                    "date": start.isoformat(),
                    "end": end.isoformat(),
                    "department": department,
                }
            ]
        },
        {
            "$set": {
                "_start": {"$dateFromString": {"dateString": "$date"}},
                "_end": {"$dateFromString": {"dateString": "$end"}},
            }
        },
        {
            "$densify": {
                "field": "_start",
                "partitionByFields": ["department"],
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.combine(start, time.min),
                        datetime.combine(end + timedelta(days=1), time.min),
                    ],
                },
            }
        },
        {
            "$set": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_start"}}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date", "dept": "$department"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", "$$dept"]},
                                    {"$lte": ["$joined_on", "$$day"]},
                                ]
                            }
                        }
                    },
                    {"$count": "n"},
                ],
                "as": "hc",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$date", "dept": "$department"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$day"]},
                                    {
                                        "$in": [
                                            "$emp_code",
                                            db.employees.distinct(
                                                "emp_code", {"department": department}
                                            ),
                                        ]
                                    },
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", list(PRESENCE)]},
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "agg",
            }
        },
        {
            "$set": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
                "present_count": {
                    "$ifNull": [{"$arrayElemAt": ["$agg.present_count", 0]}, 0]
                },
                "late_count": {
                    "$ifNull": [{"$arrayElemAt": ["$agg.late_count", 0]}, 0]
                },
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$isoDayOfWeek": "$_start"}, 1]},
                        {"$lte": [{"$isoDayOfWeek": "$_start"}, 5]},
                    ]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_start": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]
    # $documents must run through the database-level aggregate command (not a collection aggregate).
    cursor = db.aggregate(pipeline)
    rows = list(cursor)
    for row in rows:
        row["present_count"] = half_up(row.get("present_count", 0), 2)
        row["attendance_rate"] = half_up(row.get("attendance_rate"), 4)
        row["moving_avg_7d"] = half_up(row.get("moving_avg_7d"), 4)
    return {"department": department, "items": rows}


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None, alias="to"),
):
    verbosity = "executionStats"

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be <= date_to")

        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status

        dr = date_range_query(date_from, date_to)
        if dr:
            q["date"] = dr

        exp = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            verbosity=verbosity,
        )
        collection = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")

        start, end = month_bounds(month)
        q = {
            "emp_code": emp_code,
            "date": {
                "$gte": start.isoformat(),
                "$lte": end.isoformat(),
            },
        }

        exp = db.command(
            "explain",
            {"find": "attendance_logs", "filter": q},
            verbosity=verbosity,
        )
        collection = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")

        start, end = month_bounds(month)
        match = {"joined_on": {"$lte": end.isoformat()}}
        if department is not None:
            match["department"] = department

        pipeline = [
            {"$match": match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {
                        "code": "$emp_code",
                        "joined": "$joined_on",
                    },
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$code"]},
                                        {"$gte": ["$date", start.isoformat()]},
                                        {"$lte": ["$date", end.isoformat()]},
                                        {"$gte": ["$date", "$$joined"]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {
                "$unwind": {
                    "path": "$logs",
                    "preserveNullAndEmptyArrays": True,
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$addToSet": "$emp_code"},
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$logs.status", list(PRESENCE)]},
                                        {
                                            "$in": [
                                                {
                                                    "$dayOfWeek": {
                                                        "$dateFromString": {
                                                            "dateString": {
                                                                "$ifNull": [
                                                                    "$logs.date",
                                                                    start.isoformat(),
                                                                ]
                                                            }
                                                        }
                                                    }
                                                },
                                                [2, 3, 4, 5, 6],
                                            ]
                                        },
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$logs.half_day", True]},
                                        0.5,
                                        1,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "work_hours": {
                        "$push": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$logs.status", list(PRESENCE)]},
                                        {"$ne": ["$logs.work_hours", None]},
                                    ]
                                },
                                "$logs.work_hours",
                                None,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                                1,
                                0,
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                    },
                    "leave_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$logs.status", "LEAVE"]},
                                1,
                                0,
                            ]
                        }
                    },
                    "on_duty_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$logs.status", "ON_DUTY"]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {
                "$project": {
                    "department": "$_id",
                    "headcount": {"$size": "$headcount"},
                    "present_days": 1,
                    "avg_work_hours": {"$avg": "$work_hours"},
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1,
                }
            },
            {"$sort": {"department": 1}},
        ]

        exp = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity=verbosity,
        )
        collection = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")

        start, end = month_bounds(month)

        pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": start.isoformat(),
                        "$lte": end.isoformat(),
                    },
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "_id": 1,
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
        ]

        if department is not None:
            pipeline.append({"$match": {"employee.department": department}})

        pipeline += [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ]

        exp = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity=verbosity,
        )
        collection = "attendance_logs"

    elif endpoint == "department_trend":
        # Explicitly validate the query parameters.
        if department is None or not department.strip():
            raise HTTPException(422, "department is required")
        if not from_:
            raise HTTPException(422, "from is required")
        if not to:
            raise HTTPException(422, "to is required")

        s = parse_date(from_)
        e = parse_date(to)

        if e < s:
            raise HTTPException(422, "to must be on or after from")
        if (e - s).days + 1 > 92:
            raise HTTPException(422, "date range cannot exceed 92 days")

        if not db.employees.find_one({"department": department}):
            raise HTTPException(404, "unknown department")

        # Explain the trend aggregation, including its indexed lookups.
        emp_codes = db.employees.distinct("emp_code", {"department": department})

        pipeline = [
            {
                "$documents": [
                    {
                        "date": s.isoformat(),
                        "end": e.isoformat(),
                        "department": department,
                    }
                ]
            },
            {
                "$set": {
                    "_start": {"$dateFromString": {"dateString": "$date"}},
                    "_end": {"$dateFromString": {"dateString": "$end"}},
                }
            },
            {
                "$densify": {
                    "field": "_start",
                    "partitionByFields": ["department"],
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [
                            datetime.combine(s, time.min),
                            datetime.combine(e + timedelta(days=1), time.min),
                        ],
                    },
                }
            },
            {
                "$set": {
                    "date": {
                        "$dateToString": {
                            "format": "%Y-%m-%d",
                            "date": "$_start",
                        }
                    }
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "let": {"day": "$date", "dept": "$department"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$department", "$$dept"]},
                                        {"$lte": ["$joined_on", "$$day"]},
                                    ]
                                }
                            }
                        },
                        {"$count": "n"},
                    ],
                    "as": "hc",
                }
            },
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"day": "$date"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$date", "$$day"]},
                                        {"$in": ["$emp_code", emp_codes]},
                                    ]
                                }
                            }
                        },
                        {
                            "$group": {
                                "_id": None,
                                "present_count": {
                                    "$sum": {
                                        "$cond": [
                                            {"$in": ["$status", list(PRESENCE)]},
                                            {
                                                "$cond": [
                                                    {"$eq": ["$half_day", True]},
                                                    0.5,
                                                    1,
                                                ]
                                            },
                                            0,
                                        ]
                                    }
                                },
                                "late_count": {
                                    "$sum": {
                                        "$cond": [
                                            {
                                                "$gt": [
                                                    {"$ifNull": ["$late_minutes", 0]},
                                                    0,
                                                ]
                                            },
                                            1,
                                            0,
                                        ]
                                    }
                                },
                            }
                        },
                    ],
                    "as": "agg",
                }
            },
            {
                "$set": {
                    "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
                    "present_count": {
                        "$ifNull": [
                            {"$arrayElemAt": ["$agg.present_count", 0]},
                            0,
                        ]
                    },
                    "late_count": {
                        "$ifNull": [
                            {"$arrayElemAt": ["$agg.late_count", 0]},
                            0,
                        ]
                    },
                    "is_working_day": {
                        "$and": [
                            {"$gte": [{"$isoDayOfWeek": "$_start"}, 1]},
                            {"$lte": [{"$isoDayOfWeek": "$_start"}, 5]},
                        ]
                    },
                }
            },
            {
                "$set": {
                    "attendance_rate": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_working_day",
                                    {"$gt": ["$headcount", 0]},
                                ]
                            },
                            {"$divide": ["$present_count", "$headcount"]},
                            None,
                        ]
                    }
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"_start": 1},
                    "output": {
                        "moving_avg_7d": {
                            "$avg": "$attendance_rate",
                            "window": {"documents": [-6, 0]},
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "is_working_day": 1,
                    "attendance_rate": 1,
                    "moving_avg_7d": 1,
                }
            },
            {"$sort": {"date": 1}},
        ]

        exp = db.command(
            "explain",
            {
                "aggregate": 1,
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity=verbosity,
        )
        collection = "employees"

    else:
        raise HTTPException(422, "unsupported endpoint")

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json_util.loads(json_util.dumps(exp)),
    }
