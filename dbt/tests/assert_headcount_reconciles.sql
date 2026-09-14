-- THE most important test in this project.
--
-- A dbt singular test passes when it returns ZERO rows. So this query is
-- written to return the offending rows and nothing else.
--
-- What it checks: the headcount the scorecard publishes for the most recent
-- complete month must match a completely independent count taken straight from
-- the deduplicated employee list. Two different routes to the same number.
--
-- Why this matters more than a `unique` test: unique tests catch defects you
-- already thought of. A reconciliation test catches the defect you DIDN'T
-- think of — most often a join that fanned out and quietly multiplied rows.
-- If someone changes a join in the mart six months from now and headcount
-- jumps from 500 to 1,400, this is what stops the number reaching leadership.
--
-- Interview answer this test gives you: "a reconciliation test comparing the
-- mart against an independent count of the source."

with reference_count as (

    -- independent route: count directly off the dimension
    select
        (date_trunc('month', current_date) - interval '1 day')::date as as_of,
        count(*) as expected_headcount
    from {{ ref('dim_employee') }}
    where join_date <= (date_trunc('month', current_date) - interval '1 day')::date
      and (
            exit_date is null
            or exit_date > (date_trunc('month', current_date) - interval '1 day')::date
          )
      and tenure_months >= 0

),

reported as (

    -- published route: read what the scorecard actually says
    select
        month_end as as_of,
        headcount_eom as reported_headcount
    from {{ ref('mart_attrition_scorecard') }}
    where month_start = (date_trunc('month', current_date) - interval '1 month')::date

)

select
    r.as_of,
    r.reported_headcount,
    c.expected_headcount,
    r.reported_headcount - c.expected_headcount as difference
from reported r
join reference_count c
    on r.as_of = c.as_of
where r.reported_headcount <> c.expected_headcount
