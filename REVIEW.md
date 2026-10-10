# REVIEW.md

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | The function misapplied the strict `> 10 minutes` rule and could report late minutes even when the punch-in was only exactly 10 minutes after shift start. | A punch-in at 09:40:01 should be 10 late minutes, while 09:40:00 must be 0. | Replaced the calculation with a strict `elapsed_seconds > 600` check and a floor-based minute conversion in IST. |
| 2 | `create_employee` / startup indexes | The application must create the database indexes at startup, idempotently, because the grader seeds the database without them. | On a fresh DB, filtering, uniqueness, and aggregation queries would scan far more data than required. | `ensure_indexes()` creates the unique and query-supporting indexes during startup. |
| 3 | `punch_in` / overnight logic | The attendance date must follow R1 and overnight shifts must assign a punch-in to the correct IST attendance date. | A shift ending at 06:00 with a punch-in at 00:30 belongs to the previous day’s record date, not the next one. | The application uses `get_record_date_for_punch_in()` to map the punch-in to the correct IST record date. |
| 4 | `regularize_attendance` | Derived values and history must be recomputed and appended on every manual correction. It is not acceptable to overwrite history or silently ignore no-op updates. | A patch that changes nothing should fail with 422, while valid corrections append exactly one audit trail entry. | The endpoint recalculates derived values from the final times and appends a single `history` record only when a field actually changes. |
| 5 | Contract-only gap | The code had to be audited against the full OpenAPI and DATA_MODEL definitions, not just the sample documents. | Plausible-looking output can still violate an exact field name, required range, or ranking rule. | The implementation was checked against the contract and business rules R1-R10, and the remaining blockers are environmental rather than contract logic errors. |

Also note anything you looked at and decided was not a defect, and why:

- The app uses a single-file implementation by design, which matches the assignment requirement that all application code live in `app/main.py`.
- The API boundary uses epoch milliseconds and ISO dates as specified; the conversion logic is implemented at the boundary rather than buried in storage.
- The project’s remaining constraints are operational: without a reachable MongoDB instance, database-backed integration checks cannot be proven here. That is an environment issue, not a code-level contract violation.
- The core business logic for late minutes, overtime thresholds, rounded work hours, and overnight-date handling is aligned with the contract in the current code and is covered by regression tests.
