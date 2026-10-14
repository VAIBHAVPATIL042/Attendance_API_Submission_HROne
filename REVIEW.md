# Review

The implementation was compared with the assignment contract and the starter.
The following concrete defects were fixed:

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | Analytics endpoints | Monthly, department, leaderboard, and trend calculations were performed partly in application code or did not preserve zero-record employees. | Large datasets caused unnecessary document materialization and department headcounts omitted employees without logs. | The calculations now use MongoDB aggregation pipelines; department summary starts from employees and trend uses densification and window stages. |
| 2 | Punch-out selection | The lookup selected the newest open record, which could skip a newer eligible record that was already closed. | With multiple eligible records, a punch-out could close the wrong older record. | The query now selects the newest eligible punch-in first, then returns 409 if that exact record is already closed. |
| 3 | Timestamp input | Plain integer validation was insufficient protection against Pydantic coercion of strings, floats, or booleans. | Non-epoch-millisecond JSON values could reach timestamp conversion. | Timestamp fields use `StrictInt` and retain range validation and whole-second truncation. |
| 4 | Regularization history | Seeded records may omit `history`, and an update must append rather than replace history. | Regularizing a seeded record without `history` could fail or lose prior entries during a concurrent update. | Missing history is treated as empty for the atomic update, and `$push` appends each correction. |
| 5 | Explain contract | The department-summary explain response identified the attendance collection although its aggregation starts from employees. | Consumers could not tell which collection owned the explained query. | The response now reports `employees` for that endpoint and uses executionStats verbosity. |

Reviewed and retained: IST conversion, ten-minute grace-period handling, whole-minute late calculation, atomic duplicate punch-in protection, atomic punch-out updates, and startup index creation were already aligned with the contract. A standalone status-only index was not added because the principal queries constrain dates or employee/department fields as well.
