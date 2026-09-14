"""
Generate a synthetic-but-realistic HR dataset into Postgres, schema `raw`.

The important design choice: this data is DELIBERATELY DEFECTIVE.

Clean data proves nothing. Every defect injected below exists so that a
specific dbt test can catch it, and so that you have a concrete answer when an
interviewer asks "tell me about a data quality problem you've handled".

NON-DETERMINISM IS DELIBERATE. By default the RNG is seeded from the clock, so
the number and placement of defects changes on every run and even the author
does not know what is in the database until the tests report it. A test suite
that only ever sees one fixed dataset is a test suite tuned to that dataset.

Pass --seed N to reproduce an exact run (needed when you want a screenshot's
numbers to be repeatable, or when handing someone a bug report).

ANNOUNCED defects — these have tests written for them:
  DEFECT-1  duplicate employee rows          -> inflates headcount
  DEFECT-2  orphan manager_id                -> breaks the org hierarchy join
  DEFECT-3  impossible dates, 4 distinct causes -> negative / absurd tenure
              3a DD-MM swap  3b rehire on old row  3c 1900 sentinel
              3d future-dated joiner (offer accepted, not yet started)
  DEFECT-4  double-loaded attendance day(s)  -> doubles that day's hours
  DEFECT-5  NULL dept_id on a few employees  -> silently drops rows on inner join

UNANNOUNCED defects — see HIDDEN-A and HIDDEN-B below. These pass every test
currently in the project. They are here so that "all my tests pass" is not the
end of the story. Do not add tests for them until you have found them by
investigating the data. See INVESTIGATION.md.

Run:  python scripts/seed_data.py
      python scripts/seed_data.py --seed 42     # reproducible
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from datetime import date, datetime, timedelta

import psycopg2
from dotenv import load_dotenv
from faker import Faker
from psycopg2.extras import execute_values

load_dotenv()

_parser = argparse.ArgumentParser(add_help=True)
_parser.add_argument(
    "--seed", type=int, default=None,
    help="RNG seed. Omit for a clock-seeded (different every time) dataset.",
)
_args, _ = _parser.parse_known_args()

SEED = _args.seed if _args.seed is not None else random.randrange(1, 10**6)
random.seed(SEED)
fake = Faker("en_IN")
Faker.seed(SEED)

N_EMPLOYEES = 500
HISTORY_START = date(2022, 1, 1)
HISTORY_END = date(2026, 8, 31)

DEPARTMENTS = [
    (1, "Engineering", "CC-1001"),
    (2, "Operations", "CC-1002"),
    (3, "Sales", "CC-1003"),
    (4, "Customer Support", "CC-1004"),
    (5, "Finance", "CC-1005"),
    (6, "People & Culture", "CC-1006"),
]

# Free-text exit-survey templates. Written to be genuinely ambiguous in places —
# an easy dataset would make the LLM evaluation meaningless.
SURVEY_TEMPLATES = [
    "The pay just didn't keep up with the market. I liked the team a lot.",
    "I was doing the work of two people after the reorg and it never got fixed.",
    "My manager was supportive but there was no clear path to the next level.",
    "Honestly the culture changed after the new leadership came in.",
    "Got an offer with a 40% hike. Hard to say no to that.",
    "No learning happening. Same tickets for eighteen months.",
    "Workload was fine, compensation was fine, I just wanted to relocate.",
    "Skip-level never happened even once. Felt invisible.",
    "Too many late-night escalations, every single week.",
    "Manager micromanaged everything, could not make a single decision alone.",
    "Great people, great problems, but the salary band was capped.",
    "I wanted to move into a data role and there was no internal mobility.",
    "Burnt out. Three consecutive quarters of crunch.",
    "Family reasons, moving back to my hometown.",
    "The appraisal process felt arbitrary and nobody could explain the rating.",
]


# Announced defects append a line here; hidden ones deliberately do not.
DEFECT_LOG: list[str] = []


def get_conn():
    return psycopg2.connect(
        host=os.getenv("APP_DB_HOST", "localhost"),
        port=os.getenv("APP_DB_PORT", "5432"),
        user=os.getenv("APP_DB_USER", "postgres"),
        password=os.getenv("APP_DB_PASSWORD", "postgres"),
        dbname=os.getenv("APP_DB_NAME", "hr_mart"),
    )


DDL = """
drop schema if exists raw cascade;
create schema raw;

create table raw.departments (
    dept_id      integer,
    dept_name    text,
    cost_centre  text
);

create table raw.employees (
    employee_id  integer,
    full_name    text,
    email        text,
    dept_id      integer,
    manager_id   integer,
    job_level    text,
    location     text,
    join_date    date,
    exit_date    date,
    loaded_at    timestamp
);

create table raw.attendance_events (
    event_id     bigint,
    employee_id  integer,
    event_ts     timestamp,
    event_type   text,
    source_file  text
);

create table raw.exit_survey_responses (
    response_id   integer,
    employee_id   integer,
    submitted_at  timestamp,
    response_text text
);
"""


def random_date(start: date, end: date) -> date:
    return start + timedelta(days=random.randint(0, (end - start).days))


def build_employees():
    """Returns (rows, active_ids). Employee ids are 1001..1000+N."""
    rows = []
    levels = ["L1", "L2", "L3", "L4", "L5"]
    cities = ["Bengaluru", "Hyderabad", "Gurugram", "Pune", "Chennai"]

    for i in range(N_EMPLOYEES):
        emp_id = 1001 + i
        join_dt = random_date(HISTORY_START, HISTORY_END - timedelta(days=30))

        # ~28% of the population has left — a plausible attrition profile.
        exit_dt = None
        if random.random() < 0.28:
            max_exit = min(HISTORY_END, join_dt + timedelta(days=1400))
            if max_exit > join_dt + timedelta(days=45):
                exit_dt = random_date(join_dt + timedelta(days=45), max_exit)

        rows.append(
            {
                "employee_id": emp_id,
                "full_name": fake.name(),
                "email": f"emp{emp_id}@example.com",
                "dept_id": random.choice([d[0] for d in DEPARTMENTS]),
                "manager_id": None,  # filled in below
                "job_level": random.choice(levels),
                "location": random.choice(cities),
                "join_date": join_dt,
                "exit_date": exit_dt,
                "loaded_at": datetime.now(),
            }
        )

    # Assign managers: anyone at L4/L5 can manage. Everyone else reports to one.
    manager_pool = [r["employee_id"] for r in rows if r["job_level"] in ("L4", "L5")]
    for r in rows:
        if r["employee_id"] not in manager_pool:
            r["manager_id"] = random.choice(manager_pool)

    # ---------------- DEFECT-5: NULL dept_id ---------------------------------
    # Real cause: a department was merged and the mapping wasn't backfilled.
    # Why it matters: an INNER join to departments silently drops these people
    # from headcount. Nobody notices until the total is short.
    n_null_dept = random.randint(3, 12)
    for r in random.sample(rows, n_null_dept):
        r["dept_id"] = None
    DEFECT_LOG.append(f"DEFECT-5: {n_null_dept} employees with NULL dept_id")

    # ---------------- DEFECT-2: orphan manager_id ----------------------------
    # Real cause: the manager left and their row was hard-deleted upstream.
    # Caught by: dbt relationships test on dim_employee.manager_id.
    n_orphan = random.randint(2, 8)
    for r in random.sample([x for x in rows if x["manager_id"]], n_orphan):
        r["manager_id"] = 999000 + random.randint(1, 999)
    DEFECT_LOG.append(f"DEFECT-2: {n_orphan} employees with orphan manager_id")

    # ================= HIDDEN-A ==============================================
    # NOT announced at runtime. Passes every test currently in the project.
    #
    # The same human being, entered twice under two DIFFERENT employee_ids.
    # (Contrast with DEFECT-1, which is the same id twice — trivially caught.)
    #
    # Why every existing test passes:
    #   - unique(employee_id)      passes: the ids genuinely differ
    #   - the staging dedupe       does nothing: it partitions by employee_id
    #   - the reconciliation test  passes: BOTH sides of the comparison are
    #                              derived from dim_employee, so both are
    #                              inflated by exactly the same amount. Two
    #                              wrong numbers that agree with each other.
    #
    # That last point is the lesson. A reconciliation test is only as good as
    # the independence of its two sides. Comparing a mart to a dimension that
    # feeds it is not independent — it is the same mistake, counted twice.
    n_ghost = random.randint(2, 6)
    for src in random.sample([r for r in rows if r["exit_date"] is None], n_ghost):
        ghost = dict(src)
        ghost["employee_id"] = 8000 + random.randint(1, 1999)
        ghost["email"] = src["email"]                 # the only giveaway
        ghost["join_date"] = src["join_date"] + timedelta(days=random.randint(-3, 3))
        ghost["job_level"] = src["job_level"]
        rows.append(ghost)
    # deliberately NOT appended to DEFECT_LOG

    # ---------------- DEFECT-3: impossible dates (4 employees) ---------------
    # Four DIFFERENT real-world causes, because in real HR data a date problem
    # is never a single row with a single explanation — and a test that catches
    # exactly one planted row is a test written to the answer.
    #
    # Each of these needs a DIFFERENT remediation, which is the actual point:
    # you cannot write one blanket "fix bad dates" rule.
    # Each sub-case gets its OWN random count. An earlier version planted
    # exactly one of each, which meant the surviving-defect count was identical
    # on every seed — the randomisation looked real but the number the test
    # reported never moved. Randomising the counts is what makes "the tests
    # catch categories, not planted rows" an honest claim rather than a slogan.
    leavers_pool = [r for r in rows if r["exit_date"] is not None]
    n_swap     = random.randint(1, 4)
    n_rehire   = random.randint(1, 4)
    n_sentinel = random.randint(1, 3)
    n_future   = random.randint(1, 3)

    victims = random.sample(leavers_pool, n_swap + n_rehire + n_sentinel)
    cursor = 0

    # 3a. DD/MM vs MM/DD swap by an HR admin typing into the wrong locale.
    #     Remediation: correctable — the true date is recoverable by swapping.
    #     Gap is deliberately small (under a month) so that a month-based
    #     tenure check rounds it to zero and misses it. See README, Finding 1.
    for v in victims[cursor:cursor + n_swap]:
        v["exit_date"] = v["join_date"] - timedelta(days=random.randint(3, 27))
    cursor += n_swap

    # 3b. Rehire recorded against the ORIGINAL row instead of a new one, so the
    #     join_date jumped forward past the old exit_date.
    #     Remediation: needs a second employment record, not a date edit.
    for v in victims[cursor:cursor + n_rehire]:
        v["join_date"] = v["exit_date"] + timedelta(days=random.randint(120, 400))
    cursor += n_rehire

    # 3c. Exit date defaulted to 1900-01-01 by an upstream system that writes a
    #     sentinel instead of NULL. The classic "magic date" bug.
    #     Remediation: map the sentinel to NULL at the staging layer.
    for v in victims[cursor:cursor + n_sentinel]:
        v["exit_date"] = date(1900, 1, 1)

    # 3d. Future-dated join: an offer accepted but not yet started, loaded into
    #     the active-employee table. Inflates headcount for people who have not
    #     turned up yet.
    #     Remediation: filter join_date > today out of headcount, but keep the
    #     row — HR genuinely needs to see the pipeline.
    for v in random.sample([r for r in rows if r["exit_date"] is None], n_future):
        v["join_date"] = HISTORY_END + timedelta(days=random.randint(10, 90))

    DEFECT_LOG.append(
        f"DEFECT-3: {n_swap} date-swap, {n_rehire} rehire, "
        f"{n_sentinel} sentinel, {n_future} future-joiner"
    )

    # ================= HIDDEN-B ==============================================
    # NOT announced at runtime. Passes every test currently in the project.
    #
    # A management CYCLE: A reports to B, and B reports to A.
    #
    # Why every existing test passes:
    #   - relationships(manager_id -> employee_id) passes perfectly. Both ids
    #     exist. Referential integrity is intact. The data is still nonsense.
    #
    # The lesson: referential integrity is not the same as semantic validity.
    # A foreign key check asks "does this id exist?" It cannot ask "does this
    # org chart make sense?" Any recursive CTE walking the hierarchy to compute
    # reporting depth will spin until Postgres kills it.
    if len(manager_pool) >= 2:
        a, b = random.sample(manager_pool, 2)
        for r in rows:
            if r["employee_id"] == a:
                r["manager_id"] = b
            elif r["employee_id"] == b:
                r["manager_id"] = a
    # deliberately NOT appended to DEFECT_LOG

    return rows


def build_attendance(employees):
    """One check-in + check-out per working day, for a 60-day recent window."""
    rows = []
    event_id = 1
    window_end = HISTORY_END
    window_start = window_end - timedelta(days=60)

    for emp in employees:
        # only people employed during the window
        if emp["join_date"] > window_end:
            continue
        if emp["exit_date"] is not None and emp["exit_date"] < window_start:
            continue

        d = max(window_start, emp["join_date"])
        while d <= window_end:
            if d.weekday() < 5 and random.random() > 0.08:  # ~8% absence
                if emp["exit_date"] is None or d <= emp["exit_date"]:
                    check_in = datetime.combine(d, datetime.min.time()) + timedelta(
                        hours=9, minutes=random.randint(0, 75)
                    )
                    check_out = check_in + timedelta(
                        hours=random.uniform(7.0, 10.5)
                    )
                    rows.append((event_id, emp["employee_id"], check_in, "check_in", "attendance_2026.csv"))
                    event_id += 1
                    rows.append((event_id, emp["employee_id"], check_out, "check_out", "attendance_2026.csv"))
                    event_id += 1
            d += timedelta(days=1)

    # ---------------- DEFECT-4: one day loaded twice -------------------------
    # Real cause: the upstream job was re-run after a transient failure and the
    # load wasn't idempotent. THE most common data engineering bug there is.
    # Why it matters: that day's hours double, and a naive join to attendance
    # fans out every downstream row.
    n_days = random.randint(1, 3)
    dupe_days = [
        window_end - timedelta(days=random.randint(3, 55)) for _ in range(n_days)
    ]
    total_dupes = 0
    for dupe_day in set(dupe_days):
        dupes = [r for r in rows if r[2].date() == dupe_day]
        for r in dupes:
            rows.append((event_id, r[1], r[2], r[3], "attendance_2026_RERUN.csv"))
            event_id += 1
        total_dupes += len(dupes)
    DEFECT_LOG.append(
        f"DEFECT-4: re-loaded {total_dupes} attendance events "
        f"across {len(set(dupe_days))} day(s)"
    )

    return rows


def build_surveys(employees):
    rows = []
    rid = 1
    leavers = [e for e in employees if e["exit_date"] is not None]
    for emp in leavers:
        if random.random() < 0.75:  # not everyone fills the survey
            text = random.choice(SURVEY_TEMPLATES)
            if random.random() < 0.3:  # make some responses multi-reason
                text += " " + random.choice(SURVEY_TEMPLATES)
            rows.append(
                (
                    rid,
                    emp["employee_id"],
                    datetime.combine(emp["exit_date"], datetime.min.time()),
                    text,
                )
            )
            rid += 1
    return rows


def main():
    print("Connecting to Postgres...")
    conn = get_conn()
    conn.autocommit = False
    cur = conn.cursor()

    print("Creating schema `raw`...")
    cur.execute(DDL)

    print("Building employees...")
    employees = build_employees()
    emp_tuples = [
        (
            e["employee_id"], e["full_name"], e["email"], e["dept_id"],
            e["manager_id"], e["job_level"], e["location"],
            e["join_date"], e["exit_date"], e["loaded_at"],
        )
        for e in employees
    ]

    # ---------------- DEFECT-1: 7 duplicated employee rows -------------------
    # Real cause: employees who transferred teams got re-inserted instead of
    # updated. Why it matters: headcount reads 507 when the truth is 500, and
    # attrition % is computed off the wrong denominator.
    # Caught by: dbt unique test on dim_employee.employee_id, and by the
    # headcount reconciliation test.
    n_dupes = random.randint(3, 15)
    dupes = random.sample(emp_tuples, n_dupes)
    emp_tuples.extend(dupes)
    DEFECT_LOG.append(f"DEFECT-1: duplicated {n_dupes} employee rows")

    execute_values(
        cur,
        "insert into raw.employees (employee_id, full_name, email, dept_id, "
        "manager_id, job_level, location, join_date, exit_date, loaded_at) values %s",
        emp_tuples,
    )

    print("Inserting departments...")
    execute_values(
        cur, "insert into raw.departments (dept_id, dept_name, cost_centre) values %s",
        DEPARTMENTS,
    )

    print("Building attendance events...")
    att = build_attendance(employees)
    execute_values(
        cur,
        "insert into raw.attendance_events (event_id, employee_id, event_ts, "
        "event_type, source_file) values %s",
        att,
        page_size=5000,
    )

    print("Building exit surveys...")
    surveys = build_surveys(employees)
    execute_values(
        cur,
        "insert into raw.exit_survey_responses (response_id, employee_id, "
        "submitted_at, response_text) values %s",
        surveys,
    )

    conn.commit()

    print("\n--- Loaded ---")
    for tbl in ["departments", "employees", "attendance_events", "exit_survey_responses"]:
        cur.execute(f"select count(*) from raw.{tbl}")
        print(f"  raw.{tbl:<24} {cur.fetchone()[0]:>7} rows")

    print(f"\n--- Announced defects (tests exist for these) ---")
    for line in DEFECT_LOG:
        print(f"  {line}")

    print(
        f"\n  RNG seed: {SEED}"
        f"   (re-run this exact dataset with: python scripts/seed_data.py --seed {SEED})"
    )
    print(
        "\n  NOTE: the announced list above is NOT the complete set of defects\n"
        "        in this database. Some were injected without being logged and\n"
        "        pass every test currently in the project. See INVESTIGATION.md."
    )
    print("\nNext:  dbt run --project-dir dbt --profiles-dir dbt")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
