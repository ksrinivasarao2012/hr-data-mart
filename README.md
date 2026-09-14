# HR Data Mart — a pipeline that refuses to publish numbers it can't vouch for

A dbt + Postgres analytics pipeline that builds a monthly attrition scorecard
from deliberately messy HR data, and **blocks publication when its own quality
checks fail**.

The interesting part isn't the transformation. It's what happens when the data
is wrong.

---

## The problem

Someone at HR produces an attrition number every month by hand. The source data
is dirty in the ways real HR data is dirty — people entered twice after a team
transfer, managers who have already left, dates typed in the wrong locale,
a nightly load that ran twice. A single naive `SELECT` over that produces a
number that is confidently, silently wrong, and it lands in a leadership deck.

This project automates the number **and** automates the reasons to distrust it.

---

## Architecture

```
raw.*  (messy, as-loaded)
  │
  ├─ stg_employees          dedupe on employee_id, map sentinel dates to NULL
  ├─ stg_departments        normalise
  ├─ stg_attendance_events  dedupe on BUSINESS key, not surrogate key
  └─ stg_exit_surveys       whitespace + length normalisation
        │
        ├─ dim_employee              one row per employee, tenure, is_future_hire
        ├─ fct_attendance_daily      one row per employee-day, 5-day rolling avg
        └─ mart_attrition_scorecard  ← THE published report
                │
          [ 26 dbt tests ]  ← quality gate
                │
          publish  (only if the gate passes)
```

`run_pipeline.py` orchestrates it. `dags/hr_mart_daily.py` is the equivalent
Airflow DAG — see *Honest scope* at the bottom.

---

## The SQL worth reading

**`mart_attrition_scorecard.sql`** — a month spine built with `generate_series`
so months with no activity show zero rather than vanishing from the report;
`lag()` to carry closing headcount into the next month's opening; a 3-month
rolling average over a `rows between 2 preceding and current row` frame; and a
year-partitioned cumulative `sum()` for leavers-to-date.

**`stg_attendance_events.sql`** — deduplicates on `(employee_id, event_ts,
event_type)` rather than on `event_id`. The re-run assigned *fresh* event ids,
so a `unique` test on `event_id` passes while the data is still doubled.
Surrogate-key uniqueness and business-key uniqueness are different questions.

**`tests/generic/test_unique_combination_of_columns.sql`** — a hand-written
generic test, because dbt's built-in `unique` only handles one column and
compound grain is normal in fact tables.

---

## Findings

The dataset is synthetic and defective by design. Defect counts and placement
are **randomised on every run** (clock-seeded; `--seed N` to reproduce), so the
tests have to catch defect *categories*, not the specific rows I planted.

The findings below came from investigating the data after the tests were
already green.

### Finding 1 — a month-based check misses a 17-day error

`assert_no_negative_tenure` originally keyed off `tenure_months < 0`. Employee
1118 has an exit date **17 days before** the join date — a DD/MM vs MM/DD entry
error. Seventeen days rounds to **0 whole months**, so the row sailed through.

```
 employee_id | join_date  | exit_date  | tenure_months
        1118 | 2024-12-23 | 2024-12-06 |             0
```

Confirmed by running two queries side by side: the direct date comparison
returned 4 rows, the `tenure_months < 0` version returned 3.

- **Impact:** an impossible employment record counted as a valid 0-month tenure,
  inflating the `0-1y` band and contributing a leaver to the wrong month.
- **Why it was missed:** the test asserted on a *derived, rounded* value instead
  of the raw fact. Rounding destroyed the evidence before the test saw it.
- **Fixed:** both the test and the scorecard filter now compare
  `exit_date >= join_date` directly.

### Finding 2 — a reconciliation test that cannot fail

`assert_headcount_reconciles` compares the published scorecard headcount against
an independent count. It passes.

It passes on wrong data.

For seed 702371: the generator created **500 distinct people**, but
`dim_employee` holds **503 rows** — because some people were entered under two
*different* `employee_id`s (same person, same email, new id issued after a
transfer). Both sides of the reconciliation read from `dim_employee`, so both
are inflated by exactly 3, and the two wrong numbers agree perfectly.

- **Impact:** headcount over-reported by ~0.6%; attrition rate understated,
  because the denominator is too large.
- **Why every test missed it:** `unique(employee_id)` passes (the ids genuinely
  differ). The staging dedupe partitions by `employee_id`, so it does nothing.
  And the reconciliation test compares a mart to the dimension that feeds it —
  **not an independent source.**
- **The lesson:** two numbers agreeing is only evidence when they were derived
  independently. A reconciliation against your own upstream model reconciles a
  mistake with itself.
- **Status:** documented, not yet fixed. The correct fix is a natural-key
  uniqueness test (`email`, or `full_name` + `join_date`) and a reconciliation
  that reaches past `dim_employee` to `raw.employees`. See *Next steps*.

### Finding 3 — referential integrity is not semantic validity

The `relationships` test on `manager_id` passes: every manager id exists. The
org chart still contains a **cycle** — A reports to B and B reports to A. A
foreign key can ask "does this id exist?"; it cannot ask "does this hierarchy
make sense?" Any recursive CTE walking the chain runs until Postgres kills it.

- **Status:** documented, not yet fixed. Needs a recursive-CTE cycle-detection
  test.

### Finding 4 — an exclusion rule that hid a real defect

After adding the `is_future_hire` flag and excluding those rows from
`assert_no_negative_tenure`, the test warned on **1** row where it should have
warned on 2.

Employee 1130 is a rehire recorded on the original employment row, which pushed
`join_date` to 2026-10-08 — in the future. So the row satisfied
`is_future_hire`, and the exclusion written for legitimate future joiners
silently swallowed a genuinely broken record.

- **Impact:** a known-bad row stopped being reported, with no error anywhere.
  Worse than never having written the exclusion, because the suite now looked
  *cleaner* while checking less.
- **Why it happened:** the exclusion was written against a *symptom* (a future
  join date) rather than the *condition it meant to exempt* (an accepted offer
  that has not started). Those overlap, and the difference is exactly the bug.
- **Fixed:** `where not (is_future_hire and exit_date is null)`. A future join
  date combined with an exit date is impossible and must always surface.
- **Generalises to:** every exclusion in a test suite widens the set of things
  you are no longer checking. That set needs to be as narrow as the exemption
  you actually intended, and it deserves the same scrutiny as the test itself.

---

## Defect handling — four rows, four different remediations

The four impossible-date rows each needed a different response. This is the
part that doesn't generalise into one rule:

| Row | Defect | Root cause | Remediation | Severity |
|---|---|---|---|---|
| 1187 | `exit_date = 1900-01-01` | upstream writes a sentinel instead of NULL | `nullif()` in **staging** — fixed once for every downstream model | resolved |
| 1308 | join date 45 days in the future | accepted offer loaded into the active table | **not a defect** — a definition problem. New `is_future_hire` flag; excluded from headcount, row retained | resolved |
| 1118 | exit 17 days before join | DD/MM vs MM/DD locale error | needs HR to correct the source; excluded from the maths | **warn** |
| 1130 | join 200 days after exit | rehire recorded on the original row | needs a second employment record, not a date edit | **warn** |

**The severity rule used throughout:** *warn* when data is known-bad and
contained so it cannot reach a published figure; *error* when the output itself
cannot be trusted. `assert_headcount_reconciles` stays at error for that reason.
Blocking leadership's entire report indefinitely on two bad rows out of 500,
waiting on another team's inbox, is not a defensible default.

---

## The tests aren't tuned to one dataset

Because the seed is clock-based, every run plants different defect counts in
different places. Two runs, same tests, different numbers caught:

<!-- TODO: paste screenshots of two `dbt test` runs with different seeds -->

```
run A (--seed 702371):  assert_no_negative_tenure   WARN 2
                        assert_attendance_...        WARN 2
                        relationships_manager_id     WARN 7

run B (--seed ______):  assert_no_negative_tenure   WARN _
                        assert_attendance_...        WARN _
                        relationships_manager_id     WARN _
```

---

## Running it

Full setup: **[SETUP.md](SETUP.md)**. Investigation exercise:
**[INVESTIGATION.md](INVESTIGATION.md)**.

```bash
python scripts/seed_data.py                              # generate messy data
dbt run  --project-dir dbt --profiles-dir dbt            # build models
dbt test --project-dir dbt --profiles-dir dbt            # quality gate
python run_pipeline.py                                   # all of the above, gated
```

Requires PostgreSQL 16 and Python 3.11. No Docker.

---

## Honest scope

Worth stating plainly rather than letting a reader assume:

- **The data is synthetic.** Real HR records would be a privacy problem. The
  defects are injected deliberately so the tests can be verified to actually
  fire — but they're randomised each run, and the tests target defect
  *categories*, not specific planted rows.
- **Airflow is written, not operated.** `dags/hr_mart_daily.py` mirrors the
  Python runner exactly — same task names, same dependency order, same failure
  semantics — and parses cleanly. It has **not** been executed on a live
  scheduler; Airflow doesn't run natively on Windows and Docker wasn't
  available on the build machine. Orchestration here is `run_pipeline.py`.
- **Findings 2 and 3 are open**, deliberately. They're documented above rather
  than quietly fixed, because how they evaded a green test suite is more
  interesting than the fix.

---

## Next steps

- Natural-key uniqueness test on `email` to catch Finding 2
- Rewrite `assert_headcount_reconciles` to count from `raw.employees`, making
  the two sides genuinely independent
- Recursive-CTE cycle detection for Finding 3
- LLM enrichment of exit-survey free text with schema validation, a closed label
  set, an abstain path and a quarantine table (`scripts/enrich_surveys.py`),
  scored against a 40-row hand-labelled set (`eval/run_eval.py`) — written, not
  yet run end to end
