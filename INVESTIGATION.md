# Investigation log

> Do this **after** `dbt test` is passing on the announced defects, and
> **before** you write the README. Budget ~30 minutes.
>
> The point: `seed_data.py` injects defects it does not announce. They pass
> every test currently in the project. Your job is to find them by looking at
> the data, then write the tests that would have caught them.
>
> Keep notes as you go — the notes ARE the deliverable. "All my tests pass" is
> a weak story. "My tests passed, I went looking anyway, here's what I found
> and the test I added" is the story that gets you hired.

---

## Why bother

A passing test suite tells you the defects you thought of are absent. It tells
you nothing about the defects you didn't think of. Every real data incident is
in the second category — if someone had thought of it, there'd be a test.

So the skill being practised here isn't writing tests. It's **not trusting a
green test run.**

---

## Ground rules

- Don't read `seed_data.py` looking for the answer. That defeats the exercise.
  (If you get properly stuck for 20 minutes, fine — but try first.)
- Write down what you checked and what came back, including the checks that
  found nothing. Negative results are part of an honest investigation.
- When you find something, ask three questions in order:
  1. What would this do to a published number?
  2. Why did every existing test miss it?
  3. What is the smallest test that would have caught it?

---

## Investigation prompts

Not a checklist to tick — starting points. The interesting findings usually
come from a follow-up question you asked yourself, not from the query below.

### 1. Does the row count mean what you think it means?

```sql
select count(*) as rows, count(distinct employee_id) as ids
from analytics_marts.dim_employee;
```

They match. Good. **Now ask a harder question: is one row per `employee_id` the
same thing as one row per _person_?**

```sql
select email, count(*), array_agg(employee_id), array_agg(full_name)
from analytics_marts.dim_employee
group by email having count(*) > 1;
```

```sql
select full_name, join_date, count(*), array_agg(employee_id)
from analytics_marts.dim_employee
group by full_name, join_date having count(*) > 1;
```

> Surrogate uniqueness vs. natural uniqueness. A `unique` test on an id column
> proves the id is unique. It says nothing about whether the id is the right
> grain.

### 2. Does referential integrity mean the data makes sense?

The `relationships` test on `manager_id` passes. Every manager id exists.

```sql
-- who manages the people who manage other people?
select
    e.employee_id, e.full_name, e.manager_id,
    m.full_name as manager_name, m.manager_id as managers_manager
from analytics_marts.dim_employee e
join analytics_marts.dim_employee m on e.manager_id = m.employee_id
where m.manager_id = e.employee_id;
```

Then try walking the hierarchy properly:

```sql
-- CAUTION: add the depth guard, or this runs until Postgres kills it.
-- The fact that it needs a guard is itself the finding.
with recursive chain as (
    select employee_id, manager_id, 1 as depth,
           array[employee_id] as path
    from analytics_marts.dim_employee
    where manager_id is not null

    union all

    select c.employee_id, e.manager_id, c.depth + 1,
           c.path || e.employee_id
    from chain c
    join analytics_marts.dim_employee e on c.manager_id = e.employee_id
    where c.depth < 15
      and not e.employee_id = any(c.path)   -- remove this line and see what happens
)
select employee_id, max(depth) from chain group by 1 order by 2 desc limit 10;
```

> A foreign key asks "does this id exist?" It cannot ask "is this org chart
> coherent?" Those are different questions and only one of them has a built-in
> test.

### 3. Is your reconciliation test actually independent?

Open `dbt/tests/assert_headcount_reconciles.sql` and read both sides of the
comparison. Ask: **if `dim_employee` were wrong, would this test notice?**

> Two numbers agreeing is only evidence when they were derived independently.
> This is the most important idea in the whole project and it is worth being
> able to say out loud.
>
> Once you see the problem, think about what a genuinely independent check
> would look like. It has to reach past `dim_employee` to something upstream.

### 4. Sanity-check the report against the world

```sql
select month_label, headcount_bom, headcount_eom, joiners, leavers,
       attrition_rate_pct, net_change
from analytics_marts.mart_attrition_scorecard
order by month_start desc limit 18;
```

Check the arithmetic by hand for two or three months:
`headcount_bom + joiners - leavers = headcount_eom`?

If it doesn't balance, that's a finding — and the identity itself makes an
excellent dbt test, because it needs no external reference.

Also: does the monthly series move plausibly? A step change with no
corresponding joiner or leaver spike means something structural, not something
that happened to the company.

### 5. Look at the distributions, not just the constraints

```sql
select tenure_band, count(*) from analytics_marts.dim_employee group by 1;

select round(hours_worked) as hrs, count(*)
from analytics_marts.fct_attendance_daily group by 1 order by 1;

select dept_name, count(*) from analytics_marts.dim_employee group by 1 order by 2 desc;
```

> Constraint tests catch impossible values. They don't catch *implausible* ones.
> A distribution that's the wrong shape is how you notice a defect nobody
> specified a rule for.

---

## Write it up

For each thing you find, add a short entry to the README:

```
### Finding N — <one-line description>

How I found it   : <the query or observation>
Impact           : <what it does to a published number, quantified if possible>
Why tests missed : <the specific gap in the existing suite>
Test added       : <path to the new test file>
Verified by      : <re-ran with --seed X; test fires, catches N rows>
```

Then actually write the tests, in `dbt/tests/`, following the style of
`assert_no_negative_tenure.sql` — return the offending rows, and classify them
if there's more than one cause.

---

## Prove the tests aren't tuned to one dataset

The seed is clock-based, so every run produces different defect counts in
different places. Run it several times and confirm your tests fire each time
with different row counts:

```powershell
python scripts/seed_data.py --seed 101
dbt run --project-dir dbt --profiles-dir dbt
dbt test --project-dir dbt --profiles-dir dbt

python scripts/seed_data.py --seed 202
dbt run --project-dir dbt --profiles-dir dbt
dbt test --project-dir dbt --profiles-dir dbt
```

**Screenshot two runs side by side showing the same tests catching different
row counts.** That single image is the strongest evidence in the repo that the
tests check categories of defect rather than specific rows you planted — which
is the exact objection an interviewer will raise about synthetic data.

Put both screenshots in the README under a heading like
*"The tests aren't tuned to one dataset"*.

---

## What to say in the interview

> "I generated the dataset, so I deliberately injected some defects without
> logging them, and randomised the counts each run. Then I went hunting.
> The one that got me was [X] — [N] existing tests all passed on it, because
> [reason]. What I took from that is that a reconciliation test is only as
> good as the independence of its two sides."

That answer is worth more than a green test run, because it shows you don't
trust your own green test run. Which is the actual job.
