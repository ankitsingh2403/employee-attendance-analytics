# REVIEW.md

## Defects Found in the Starter Implementation

| # | Where | What is wrong | How to notice | How fixed |
|---|---|---|---|---|
| 1 | `compute_late_minutes()` | Incorrect IST conversion, overnight shift handling, and shift-date boundaries. | Test punch-in times around shift start and overnight boundaries. | Added timezone-aware calculations, shift-date handling, the 10-minute grace rule, and whole-minute calculations. |
| 2 | `compute_work_hours()` | Built-in `round()` uses banker's rounding instead of half-up rounding. | Test durations that produce rounding ties. | Added explicit half-up rounding to two decimal places. |
| 3 | `compute_overtime()` | Missing 30-minute minimum overtime threshold and overnight shift handling. | Test punch-outs before shift end, 29 minutes after, and 30 minutes after. | Added correct shift-end calculations and the 30-minute threshold. |
| 4 | `create_employee()` | Duplicate checks are vulnerable to concurrent requests; creation timestamp is naive. | Send concurrent requests with the same employee code. | Added a unique index, duplicate-key handling, and UTC timestamps. |
| 5 | `list_employees()` | Incorrect pagination offset and unfiltered total count. | Test multiple pages and department filtering. | Corrected the offset and applied filters to the count query. |
| 6 | `punch_in()` | Unknown employees and invalid statuses are not handled properly; duplicate checks are race-prone. | Test unknown employee codes, invalid statuses, and concurrent requests. | Added employee and status validation, a unique index, and duplicate-key handling. |
| 7 | `punch_in()` | Timestamp conversion and attendance dates do not reliably follow epoch-millisecond, IST, and overnight rules. | Test timestamps near midnight IST and overnight shift boundaries. | Added consistent timestamp conversion, second-level truncation, and IST-based date calculation. |
| 8 | `list_attendance()` | Loads and sorts matching records in Python; pagination parameters are not validated. | Test large datasets and invalid page values. | Added database-side filtering, sorting, pagination, and parameter validation. |
| 9 | `health()` | Reports success without checking MongoDB connectivity. | Stop MongoDB and call `/health`. | Added a database ping and HTTP 503 on database failure. |
| 10 | Application startup | Required database indexes are not created. | Start with a fresh database and inspect indexes. | Added idempotent index creation for employee codes and attendance natural keys. |
| 11 | Application endpoints | Required endpoints from the API contract are missing. | Compare implemented routes with `openapi.yaml`. | Implemented missing endpoints, including punch-out, regularization, analytics, and query-plan inspection. |

## Items Reviewed but Not Considered Defects

| # | Item | Reason |
|---|---|---|
| 1 | `list_attendance()` pagination offset | The starter already uses `(page - 1) * page_size`, which is correct. The issue is in-memory filtering, sorting, and pagination. |
| 2 | `list_attendance()` filtered total | The starter counts matching records after applying filters. This is logically correct, although loading all matches into memory is inefficient. |
| 3 | API version | Version `2.0.0` is not a defect unless it conflicts with an explicit assignment requirement. |
