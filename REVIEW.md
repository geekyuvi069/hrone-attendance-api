# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
|| 1 | Module setup (`MongoClient`) | Hard-coded fallback URI/DB; PyMongo returns naive datetimes | Unset `MONGO_DB` → app silently uses `attendance_db`; naive − aware datetime raises `TypeError` | `os.environ[...]` with no default; `tz_aware=True`; `serverSelectionTimeoutMS=3000` |
| 2 | Startup | No indexes created at all | Duplicate emp_code / double punch-in get in; explain shows COLLSCAN | Idempotent `create_index` calls on startup (unique emp_code, unique emp_code+date, date+emp_code, department+emp_code, joined_on) |
| 3 | `compute_late_minutes` | Shift start built in the punch's timezone (UTC), not IST | Shift 09:30, punch 09:45 IST → wrong late value | Build shift start from the attendance date in IST |
| 4 | `compute_late_minutes` | Grace check on floored minutes (`> 10`); no truncation to seconds | 09:40:30 → 0, should be 10 | Compare seconds `> 600`, truncate microseconds |
| 5 | `compute_late_minutes` | Overnight shifts not handled | Shift 22:00, punch 00:10 → 0, should be 130 | New `attendance_date()` helper moves early-morning punches to the previous day |
| 6 | `compute_work_hours` | `round()` is banker's/float rounding, not half-up | 9h07m30s → 9.12, should be 9.13 | `Decimal` + `ROUND_HALF_UP` |
| 7 | `compute_overtime` | No 30-minute minimum | 5 min after shift end → 5, should be 0 | Return 0 unless `>= 30` |
| 8 | `compute_overtime` | Overnight shift end placed on the same day | 22:00–06:00 shift → ~24h fake overtime | Add one day when `shift_end <= shift_start` |
| 9 | `EmployeeIn` | No validation | `emp_code: "abc"`, `shift_start: "25:99"`, `joined_on: "2026-02-30"` all accepted | Field patterns/lengths per openapi, date check, `shift_start != shift_end` |
| 10 | `PunchInIn` | `punched_at` accepts strings, floats, seconds; `status` accepts ABSENT | `"1783312500000"` or `1783312500` → 201 | `StrictInt` with min/max range; `Literal["PRESENT","WFH","ON_DUTY"]` |
| 11 | `health` | Always 200, never pings Mongo | Stop Mongo → still "ok" | `ping`, 503 on failure |
| 12 | `create_employee` | Check-then-insert race | 8 parallel creates → several 201s / 500 | Rely on unique index, catch `DuplicateKeyError` → 409 |
| 13 | `create_employee` | `datetime.now()` local naive; `created_at` not epoch ms | Response has an ISO string; stored 5:30 off on an IST server | `datetime.now(timezone.utc)`, return `to_ms()` |
| 14 | `list_employees` | `skip = page * page_size` | `page=1` skips the first 20 | `(page - 1) * page_size` |
| 15 | `list_employees` | `total` ignores the filter | `?department=Sales` → total = all employees | `count_documents(q)` |
| 16 | `list_employees` | No sort | Order changes between calls | `.sort("emp_code", 1)` |
| 17 | `list_employees` | No paging limits | `page=0`, `page_size=5000` accepted | `Query(ge=1)`, `le=100` |
| 18 | `punch_in` | Unknown employee → `None["shift_start"]` | 500 | 404 |
| 19 | `punch_in` | `fromtimestamp` uses server tz; date not IST; no overnight rule; no truncation | Punch at 00:30 IST on a UTC server gets the previous date | `from_ms()` (UTC, whole seconds) + `attendance_date()` |
| 20 | `punch_in` | Check-then-insert race | 2 parallel punch-ins → two 201s or a 500 | Unique (emp_code, date) index, `DuplicateKeyError` → 409 |
| 21 | `punch_in` | Leaks `_id` as `id`; datetimes not epoch ms | Response has `"id"` and an ISO date | `record_out()` serializer |
| 22 | `list_attendance` | Loads all matches into memory, sorts in Python; wrong order | Slow at 100k; ties on date not ordered by emp_code | Mongo `sort/skip/limit` on (date desc, emp_code) index |
| 23 | `list_attendance` | No validation | `date_from > date_to`, `status=FOO`, `page=0` → 200 | Query patterns, `Literal`, explicit 422 |
| 24 | `list_attendance` | Leaks `id`; ms not used; legacy docs miss `half_day`/`history` | Legacy record returns no `half_day` | `record_out()` with defaults |

**Looked at and decided were fine**
- `load_dotenv()`: it does not override real environment variables, which is what the contract asks for.
- `IST = timezone(timedelta(hours=5, minutes=30))`: correct.
- Comparing `YYYY-MM-DD` strings with `$gte`/`$lte`: correct, because the format sorts the same as calendar order.
- `status_code=201` on the two create endpoints: matches the contract.


