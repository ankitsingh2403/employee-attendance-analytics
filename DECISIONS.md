# DECISIONS.md

### 1. Indexes
I created a unique index on `employees.emp_code` to prevent duplicate employees, and a unique compound index on `attendance_logs.emp_code` and `date` to enforce one attendance record per employee per day. Additional indexes support department and joining-date filters, attendance date sorting, status/date filtering, and employee punch-in lookups. I did not add a department index to attendance logs because department belongs to the employees collection.

### 2. Punch-in race
Both requests may pass employee validation, but the unique attendance index allows only one insert for the same employee and date. The successful request returns `201`; the other receives a duplicate-key error, which the API converts to `409 Conflict`.

### 3. Ties
The leaderboard uses MongoDB `$rank` on total late minutes. Ranking happens before applying the limit, so employees tied at the cutoff share the same rank and are all included. Tied results are ordered by employee code.

### 4. Headcount
The department summary starts from employees who joined by the end of the requested month, then looks up attendance records. Preserving employees with empty lookup results ensures employees with no logs still count toward headcount.

### 5. Scaling
For 100 times the data, I would benchmark with realistic data, inspect execution plans, and consider materialized daily or monthly summaries to reduce repeated aggregation work.
