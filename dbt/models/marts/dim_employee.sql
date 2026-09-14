-- Dimension: one row per employee, enriched with derived attributes.
--
-- Two things to notice:
--
-- 1. LEFT join to departments, not inner. DEFECT-5 gave 6 employees a NULL
--    dept_id. An inner join would silently drop them and headcount would be
--    6 short with no error anywhere. We keep them and label them 'Unassigned'
--    so the gap is visible instead of invisible.
--
-- 2. tenure_months is allowed to come out NEGATIVE for DEFECT-3. We do NOT
--    clamp it with greatest(0, ...). Hiding a bad row makes the test pass and
--    the data wrong. The custom test assert_no_negative_tenure catches it and
--    the pipeline stops.

with employees as (

    select * from {{ ref('stg_employees') }}

),

departments as (

    select * from {{ ref('stg_departments') }}

),

joined as (

    select
        e.employee_id,
        e.full_name,
        e.email,
        e.dept_id,
        coalesce(d.dept_name, 'Unassigned')   as dept_name,
        coalesce(d.cost_centre, 'CC-UNKNOWN') as cost_centre,
        e.manager_id,
        e.job_level,
        e.location,
        e.join_date,
        e.exit_date,

        (e.exit_date is null)                 as is_active,

        -- DEFECT-3d is not a defect, it is a DEFINITION problem.
        -- Someone has accepted an offer and has a join_date in the future.
        -- The row is correct; what is wrong is treating them as headcount.
        -- So: keep the row, flag it, and let each consumer decide. HR genuinely
        -- wants to see the joining pipeline; the attrition scorecard must not
        -- count people who have not started.
        (e.join_date > current_date)          as is_future_hire,

        -- full months between join and exit (or today, if still employed)
        (
            extract(year  from age(coalesce(e.exit_date, current_date), e.join_date)) * 12
          + extract(month from age(coalesce(e.exit_date, current_date), e.join_date))
        )::int                                as tenure_months

    from employees e
    left join departments d
        on e.dept_id = d.dept_id

)

select
    *,

    -- Window function: rank employees by tenure WITHIN their department.
    -- Useful on its own, and it demonstrates partitioned ranking.
    dense_rank() over (
        partition by dept_name
        order by tenure_months desc
    ) as tenure_rank_in_dept,

    case
        when tenure_months < 12 then '0-1y'
        when tenure_months < 24 then '1-2y'
        when tenure_months < 48 then '2-4y'
        else '4y+'
    end as tenure_band

from joined
