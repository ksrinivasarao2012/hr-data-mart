-- Catches DEFECT-3: employment dates that are impossible.
--
-- This test does more than say "something is wrong". It CLASSIFIES each bad
-- row by its likely root cause, because the four causes need four different
-- fixes and the person reading this at 6am should not have to work that out
-- themselves:
--
--   sentinel_exit_date  -> upstream writes 1900-01-01 instead of NULL.
--                          Fix at the staging layer, once, forever.
--   probable_rehire     -> join_date is AFTER exit_date by a long gap.
--                          Needs a second employment record, not a date edit.
--   probable_date_swap  -> exit precedes join by a small margin.
--                          DD/MM vs MM/DD. Correctable by HR.
--   future_joiner       -> accepted offer loaded into the active table.
--                          Not an error — a definition problem. Exclude from
--                          headcount, keep the row.
--
-- A test that tells you WHICH problem you have is worth five tests that tell
-- you that you have one.

{{ config(severity = 'warn') }}

-- SEVERITY DECISION (worth being able to defend):
--
-- warn, not error. These rows need a HUMAN at HR to correct a date in the
-- source system — there is no code change that can recover the true value.
-- Blocking the entire attrition report indefinitely on 2 bad rows out of 500
-- would mean leadership gets no numbers at all until an unrelated team acts.
--
-- The rows are already excluded from the scorecard's maths, so they cannot
-- distort any published figure. The warning keeps them visible on every single
-- run so they cannot be quietly forgotten.
--
-- Contrast: assert_headcount_reconciles stays at error severity, because a
-- headcount mismatch means the published number itself is wrong. That is the
-- distinction — warn when data is known-bad and contained, error when the
-- OUTPUT cannot be trusted.

with flagged as (

    select
        employee_id,
        full_name,
        dept_name,
        join_date,
        exit_date,
        tenure_months,

        case
            when exit_date = date '1900-01-01'
                then 'sentinel_exit_date'
            when exit_date is not null
             and join_date > exit_date
             and join_date - exit_date > 90
                then 'probable_rehire'
            when exit_date is not null
             and exit_date < join_date
                then 'probable_date_swap'
            when tenure_months < 0
                then 'unclassified_negative_tenure'
        end as defect_class

    from {{ ref('dim_employee') }}

    -- A clean future hire (offer accepted, no exit date) is handled by the
    -- is_future_hire flag and excluded from the scorecard by definition, so
    -- warning about it every run would just train people to ignore alerts.
    --
    -- BUT the exclusion must be narrow. `where not is_future_hire` alone was
    -- too wide: employee 1130 is a rehire recorded on the original row, which
    -- pushed join_date into the future — so the row looked like a future hire
    -- AND had an exit date, and the exclusion silently swallowed a genuinely
    -- broken record. See README, Finding 4.
    --
    -- Rule: only exclude future hires that have NO exit date. A future join
    -- date combined with an exit date is impossible and must always surface.
    where not (is_future_hire and exit_date is null)

)

select *
from flagged
where defect_class is not null
order by defect_class, employee_id
