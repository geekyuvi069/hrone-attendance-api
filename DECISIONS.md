<!-- # DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
5. **One thing you would change** if this had to serve 100x the data. -->


# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (duplicate → 409, fast lookups); `department+emp_code` (filtered, sorted list and trend); `joined_on` (summary headcount match). `attendance_logs`: unique `emp_code+date` (one record per day, the punch-in race, per-employee monthly queries and lookups); `date desc+emp_code` (attendance list sort and month-range analytics). I rejected an index on `status`: it has only five values, so it filters poorly.

2. **Punch-in race.** Both requests compute the same attendance date and both call `insert_one`. The unique `(emp_code, date)` index lets exactly one succeed; the other gets `DuplicateKeyError`, which I return as 409. There is no check-then-insert.

3. **Ties.** `$setWindowFields` with `$rank` gives competition ranks (1, 2, 2, 4). I filter `rank <= limit` after ranking, so everyone tied at the cutoff is returned, even if that is more than `limit` rows. Ties are ordered by `emp_code`.

4. **Headcount.** The pipeline starts from `employees` (`joined_on <= month end`) and `$lookup`s each person's logs. People with no logs get empty stats but are still counted by `$sum: 1`.

5. **100x data.** Maintain a daily per-employee rollup collection updated on punch/patch, so analytics read small pre-aggregated documents instead of raw logs.
