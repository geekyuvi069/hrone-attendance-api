"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import calendar
import json

from bson import json_util
from fastapi import FastAPI, HTTPException, Path, Query
from dotenv import load_dotenv
from pydantic import BaseModel
from pymongo import MongoClient
from decimal import Decimal , ROUND_HALF_UP
from typing import Annotated , Literal 
from pydantic import Field  , StrictInt , field_validator , model_validator
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.environ["MONGO_URI"], tz_aware=True , serverSelectionTimeoutMS=3000)
db = client[os.environ["MONGO_DB"]]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")
def create_indexes():
    db.employees.create_index("emp_code",unique=True)
    db.employees.create_index([("department",1),("emp_code",1)])
    db.employees.create_index("joined_on")
    db.attendance_logs.create_index([("emp_code",1),("date",1)],unique=True)
    db.attendance_logs.create_index([("date",-1),("emp_code",1)])

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)  # // truncates to whole seconds (R1)

def record_out(r: dict) -> dict:
    history = []
    for h in r.get("history", []):
        changes = {}
        for k, v in h["changes"].items():
            if k in ("punch_in", "punch_out"):
                v = {"from": to_ms(v["from"]), "to": to_ms(v["to"])}
            changes[k] = v
        history.append({"at": to_ms(h["at"]), "by": h["by"], "reason": h["reason"], "changes": changes})
    return {
        "emp_code": r["emp_code"],
        "date": r["date"],
        "status": r["status"],
        "punch_in": to_ms(r.get("punch_in")),
        "punch_out": to_ms(r.get("punch_out")),
        "work_hours": r.get("work_hours"),
        "late_minutes": r.get("late_minutes", 0),
        "overtime_minutes": r.get("overtime_minutes", 0),
        "half_day": r.get("half_day", False),
        "history": history,
    }

def to_ms(dt):
    return None if dt is None else round(dt.timestamp() * 1000)

def attendance_date(punch_in:datetime, shift_start:str , shift_end:str) -> str:
    ist = punch_in.astimezone(IST)
    overnight = shift_end <= shift_start
    if overnight and ist.strftime("%H:%M:%S") < shift_end +":00":
       return (ist.date() - timedelta(days=1)).isoformat()
    return ist.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str,date_str: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    secs = (punch_in.replace(microsecond=0) - start).total_seconds()
    return int(secs / 60) if secs > 600 else 0

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    secs = (punch_out.replace(microsecond=0) - punch_in.replace(microsecond=0)).total_seconds()
    hours = Decimal(int(secs))/Decimal(3600)
    return float(hours.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))   


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    if shift_end <= shift_start:
        end += timedelta(days=1)
    mins = int((punch_out.replace(microsecond=0) - end).total_seconds() // 60 )
    return mins if mins >= 30 else 0


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
# class EmployeeIn(BaseModel):
#     emp_code: str
#     name: str
#     email: str
#     department: str
#     shift_start: str = "09:30"
#     shift_end: str = "18:30"
#     joined_on: str


# class PunchInIn(BaseModel):
#     emp_code: str
#     punched_at: Optional[int] = None
#     status: str = "PRESENT"
HHMM  = r"^(?:[01]\d|2[0-3]):[0-5]\d$"
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]  # 1973-03-03 to 2100-01-01S

def check_date(v :str) -> str:
    if len(v) != 10 :
        raise ValueError("data must be yyyy-mm-dd")
    datetime.strptime(v,"%Y-%m-%d")
    return v 

class EmployeeIn(BaseModel):
    emp_code : str = Field(pattern=r"^EMP\d{4,6}$")
    name : str = Field(min_length=1,max_length=100)
    email : str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department : str = Field(min_length=1,max_length=50)
    shift_start : str = Field("09:30",pattern=HHMM)
    shift_end : str = Field("18:30",pattern=HHMM)
    joined_on : str 

    _joined = field_validator("joined_on")(check_date)

    @model_validator(mode="after")
    def shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self

class PunchInIn(BaseModel):
    emp_code : str 
    punched_at : Optional[EpochMs] = None
    status : Literal["PRESENT","WFH","ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try : 
        client.admin.command("ping")
    except Exception :
        raise HTTPException(503,"MongoDB is not reachable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try :
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") 
    doc.pop("_id", None)
    doc["created_at"] = to_ms(doc["created_at"])
    return doc
#
@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {} if department is None else {"department": department}
    total = db.employees.count_documents(q)
    cur = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    items = []
    for e in cur:
        e["created_at"] = to_ms(e.get("created_at"))
        items.append(e)
    return {"items": items, "total": total, "page": page, "page_size": page_size}
    


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    if body.punched_at is not None:
        ts = from_ms(body.punched_at)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)



DATE = r"^\d{4}-\d{2}-\d{2}$"
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


def attendance_query(emp_code, date_from, date_to, status) -> dict:
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return q


ATTENDANCE_SORT = [("date", -1), ("emp_code", 1)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE),
    date_to: Optional[str] = Query(None, pattern=DATE),
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    q = attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cur = (db.attendance_logs.find(q).sort(ATTENDANCE_SORT)
           .skip((page - 1) * page_size).limit(page_size))
    return {"items": [record_out(r) for r in cur], "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-out")
def  punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp :
        raise HTTPException(404,"employee not found")
    if body.punched_at is not None:
        ts = from_ms(body.punched_at)
    else :
        ts = datetime.now(timezone.utc).replace(microsecond=0)

    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort = [("date",-1)]

    )
    if not rec : 
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None :
        raise HTTPException(409, "already punched out for this date")
    if ts <= rec["punch_in"] or ts - rec['punch_in'] > timedelta(hours=24):
        raise HTTPException(422, "punch_out must be after punch_in and with in  24 hours")


    wh = compute_work_hours(rec["punch_in"], ts)
    fields = {
        "punch_out": ts,
        "work_hours": wh,
        "overtime_minutes": compute_overtime(ts, emp["shift_start"], emp["shift_end"], rec["date"]),
        "half_day": wh < 4.5,
    }
    res = db.attendance_logs.update_one({"_id": rec["_id"], "punch_out": None},{"$set": fields})
    if res.modified_count == 0:
        raise HTTPException(409, "already punched out")
    rec.update(fields)
    return record_out(rec)


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(body: RegularizeIn, emp_code: str, date: str = Path(pattern=DATE)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "no record for that date")

    status = body.status or rec["status"]
    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        pin = pout = None
    else:
        pin = from_ms(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        pout = from_ms(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if pin is None:
            raise HTTPException(422, "presence status requires punch_in")
        if attendance_date(pin, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in must stay on the record's date")
        if pout is not None and (pout <= pin or pout - pin > timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    new = {"status": status, "punch_in": pin, "punch_out": pout,
           "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if pin is not None:
        new["late_minutes"] = compute_late_minutes(pin, emp["shift_start"], date)
    if pin is not None and pout is not None:
        new["work_hours"] = compute_work_hours(pin, pout)
        new["overtime_minutes"] = compute_overtime(pout, emp["shift_start"], emp["shift_end"], date)
        new["half_day"] = new["work_hours"] < 4.5

    defaults = {"late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    changes = {}
    for k, v in new.items():
        old = rec.get(k, defaults.get(k))
        if old != v:
            changes[k] = {"from": old, "to": v}
    if not changes:
        raise HTTPException(422, "request changes nothing")
    entry = {"at": datetime.now(timezone.utc).replace(microsecond=0),
             "by": body.regularized_by, "reason": body.reason, "changes": changes}

    cond = {"_id": rec["_id"], "punch_out": rec.get("punch_out")}
    if "history" in rec:
        cond["history"] = {"$size": len(rec["history"])}
    else:
        cond["history"] = {"$exists": False}
    res = db.attendance_logs.update_one(cond, {"$set": new, "$push": {"history": entry}})
    if res.modified_count == 0:
        raise HTTPException(409, "record was changed concurrently, retry")

    rec.update(new)
    rec["history"] = rec.get("history", []) + [entry]
    return record_out(rec)


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
MONTH = r"^\d{4}-(0[1-9]|1[0-2])$"
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]}
PRESENT_VALUE = {"$cond": [{"$in": ["$status", PRESENCE]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}
IS_LATE = {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}
HAS_WORK_HOURS = {"$and": [{"$in": ["$status", PRESENCE]}, {"$isNumber": "$work_hours"}]}


def count_if(cond) -> dict:
    return {"$sum": {"$cond": [cond, 1, 0]}}


LOG_STATS = {
    "present_days": {"$sum": {"$cond": [IS_WEEKDAY, PRESENT_VALUE, 0]}},
    "leave_count": count_if({"$eq": ["$status", "LEAVE"]}),
    "on_duty_count": count_if({"$eq": ["$status", "ON_DUTY"]}),
    "late_count": count_if(IS_LATE),
    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
    "wh_sum": {"$sum": {"$cond": [HAS_WORK_HOURS, {"$toDecimal": "$work_hours"}, 0]}},
    "wh_count": count_if(HAS_WORK_HOURS),
}


def half_up(x, n: int) -> float:
    return float(Decimal(str(x)).quantize(Decimal(1).scaleb(-n), rounding=ROUND_HALF_UP))


def m_half_up(expr, n: int) -> dict:
    f = 10 ** n
    return {"$toDouble": {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$toDecimal": expr}, f]}, 0.5]}}, f]}}


def month_range(month: str) -> tuple:
    y, m = map(int, month.split("-"))
    return f"{month}-01", f"{month}-{calendar.monthrange(y, m)[1]:02d}"


def count_weekdays(start: str, end: str) -> int:
    d = datetime.strptime(start, "%Y-%m-%d")
    last = datetime.strptime(end, "%Y-%m-%d")
    n = 0
    while d <= last:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_range(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": None, **LOG_STATS}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    first, last = month_range(month)
    working = count_weekdays(max(first, emp["joined_on"]), last)
    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, month)))
    s = rows[0] if rows else {}
    present = s.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working,
        "present_days": present,
        "leave_days": s.get("leave_count", 0),
        "late_count": s.get("late_count", 0),
        "total_late_minutes": s.get("total_late_minutes", 0),
        "total_overtime_minutes": s.get("total_overtime_minutes", 0),
        "attendance_pct": half_up(Decimal(str(present)) / working * 100, 2) if working else None,
    }


def summary_pipeline(month: str, department: Optional[str]) -> list:
    first, last = month_range(month)
    match = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    sums = {k: {"$sum": f"$s.{k}"} for k in LOG_STATS}
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$group": {"_id": None, **LOG_STATS}},
            ],
            "as": "s",
        }},
        {"$set": {"s": {"$ifNull": [{"$first": "$s"}, {}]}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, **sums}},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$wh_count", 0]},
                                         m_half_up({"$divide": ["$wh_sum", "$wh_count"]}, 2), None]},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH), department: Optional[str] = None):
    items = list(db.employees.aggregate(summary_pipeline(month, department)))
    return {"month": month, "items": items}


def leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_range(month)
    p = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department is not None:
        p.append({"$match": {"emp.department": department}})
    p += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]
    return p


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


def trend_pipeline(department: str, from_: str, to: str) -> list:
    try:
        start = datetime.strptime(from_, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = datetime.strptime(to, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(422, "dates must be YYYY-MM-DD")
    if end < start or (end - start).days >= 92:
        raise HTTPException(422, "to must be >= from and the range at most 92 days")
    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "unknown department")
    codes = [e["emp_code"] for e in emps]
    joined = [e["joined_on"] for e in emps]
    day = {"$dateFromString": {"dateString": "$_id"}}
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": from_, "$lte": to}}},
        {"$unionWith": {"pipeline": [{"$documents": [{"date": from_}]}]}},
        {"$group": {"_id": "$date", "present_count": {"$sum": PRESENT_VALUE}, "late_count": count_if(IS_LATE)}},
        {"$set": {"day": day}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start, end + timedelta(days=1)]}}},
        {"$set": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$set": {"headcount": {"$size": {"$filter": {"input": joined, "cond": {"$lte": ["$$this", "$date"]}}}}}},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            m_half_up({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4),
            None,
        ]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"ma": {
            "$avg": {"$toDecimal": "$attendance_rate"}, "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"day": 1}},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": m_half_up("$ma", 4),
        }},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from", pattern=DATE),
    to: str = Query(..., pattern=DATE),
):
    items = list(db.attendance_logs.aggregate(trend_pipeline(department, from_, to)))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
def require(*values):
    if any(v is None for v in values):
        raise HTTPException(422, "missing required query parameter")


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=DATE),
    date_to: Optional[str] = Query(None, pattern=DATE),
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from", pattern=DATE),
    to: Optional[str] = Query(None, pattern=DATE),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    coll = "attendance_logs"
    if endpoint == "attendance_list":
        cmd = {
            "find": coll,
            "filter": attendance_query(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            require(emp_code, month)
            pipeline = monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            require(month)
            coll = "employees"
            pipeline = summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            require(month)
            pipeline = leaderboard_pipeline(month, limit, department)
        else:
            require(department, from_, to)
            pipeline = trend_pipeline(department, from_, to)
        cmd = {"aggregate": coll, "pipeline": pipeline, "cursor": {}}
    out = db.command("explain", cmd, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": coll, "explain": json.loads(json_util.dumps(out))}
