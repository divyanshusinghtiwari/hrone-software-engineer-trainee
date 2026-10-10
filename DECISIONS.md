1. **Indexes.** The app creates a unique `emp_code` index on `employees` and a unique compound `(emp_code, date)` index on `attendance_logs`, plus the date, department, and employee lookup index patterns required for filtering and aggregation work. These are the specific index families needed for the contract’s month, department, and attendance filters while remaining idempotent across startup.

2. **Punch-in race.** A concurrent duplicate punch-in is handled through the unique compound key on `attendance_logs`, so one request wins and the other hits a duplicate-key `409`. This avoids a race-prone check-then-insert and matches the requirement that exactly one record exists for a given employee/date.

3. **Late-minute semantics.** The implementation uses the strict rule from R2: a punch-in is late only if it is more than 10 minutes after `shift_start`. The stored `late_minutes` is the floor of elapsed minutes since shift start, and the strict check is separate from the display value. This keeps 09:40:00 at 0 while 09:40:01 becomes 10.

4. **Overnight and date semantics.** The attendance date follows R1: the record belongs to the IST date of the shift start, except overnight shifts where an early-morning punch-in is mapped to the previous calendar day. The same logic is used for `late_minutes`, `overtime_minutes`, and the employee’s record date.

5. **Operational blocker.** The full MongoDB-backed validation is not provable in this environment because there is no reachable MongoDB service and no Docker daemon available. The correct approach is therefore: keep the code contract-compliant, run pure-logic and import/syntax checks locally, and leave database-backed integration tests explicitly skipped unless a live database is available.
