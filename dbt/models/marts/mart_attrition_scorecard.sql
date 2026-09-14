-- THE report. One row per calendar month.
--
-- This is the model the whole project exists to produce, and the one to be
-- able to explain line by line in an interview.
--
-- Attrition rate definition used here:
--     leavers in month / average headcount in month
-- Average headcount = (headcount at start + headcount at end) / 2.
--
-- There is no single "correct" attrition formula — what matters is that the
-- definition is written down and applied consistently. Being able to SAY that
-- is more impressive than picking the "right" one.

with employees as (

    select * from {{ ref('dim_employee') }}

    -- Exclusions, each for a DIFFERENT reason. Worth reading as a pair:
    --
    --   is_future_hire  -> a correct row that is not headcount yet.
    --                      Excluded by DEFINITION, not because it is bad.
    --
    --   exit before join -> genuinely broken rows awaiting human correction.
    --                      Excluded so one bad row cannot distort the whole
    --                      report, but the test still WARNS on every run so
    --                      they cannot be quietly forgotten.
    --
    -- Note this uses the date comparison, not `tenure_months < 0`: a 17-day
    -- reversal rounds to 0 whole months and slips through the month-based
    -- check. Found the hard way — see README, Finding 1.
    where not is_future_hire
      and (exit_date is null or exit_date >= join_date)
),

-- Build a continuous month spine. Without this, months where nobody joined
-- and nobody left would simply be MISSING from the report rather than showing
-- zero — a classic silent reporting bug.
month_spine as (

    select generate_series(
        date_trunc('month', (select min(join_date) from employees)),
        date_trunc('month', current_date),
        interval '1 month'
    )::date as month_start

),

months as (

    select
        month_start,
        (month_start + interval '1 month - 1 day')::date as month_end
    from month_spine

),

-- Headcount at the END of each month: joined on or before month end, and
-- either still active or exited after month end.
headcount as (

    select
        m.month_start,
        m.month_end,
        count(e.employee_id) as headcount_eom
    from months m
    left join employees e
        on e.join_date <= m.month_end
       and (e.exit_date is null or e.exit_date > m.month_end)
    group by m.month_start, m.month_end

),

joiners as (

    select
        date_trunc('month', join_date)::date as month_start,
        count(*) as joiners
    from employees
    group by 1

),

leavers as (

    select
        date_trunc('month', exit_date)::date as month_start,
        count(*) as leavers
    from employees
    where exit_date is not null
    group by 1

),

combined as (

    select
        h.month_start,
        h.month_end,
        h.headcount_eom,
        coalesce(j.joiners, 0) as joiners,
        coalesce(l.leavers, 0) as leavers,

        -- Window function: previous month's closing headcount, which is this
        -- month's opening headcount. lag() is the cleanest way to express it.
        lag(h.headcount_eom) over (order by h.month_start) as headcount_bom

    from headcount h
    left join joiners j on j.month_start = h.month_start
    left join leavers l on l.month_start = h.month_start

),

final as (

    select
        month_start,
        month_end,
        to_char(month_start, 'YYYY-MM')          as month_label,
        coalesce(headcount_bom, 0)               as headcount_bom,
        headcount_eom,
        joiners,
        leavers,

        headcount_eom - coalesce(headcount_bom, 0) as net_change,

        -- average headcount, guarding against divide-by-zero in month 1
        nullif((coalesce(headcount_bom, 0) + headcount_eom) / 2.0, 0) as avg_headcount,

        round(
            100.0 * leavers
            / nullif((coalesce(headcount_bom, 0) + headcount_eom) / 2.0, 0)
        , 2) as attrition_rate_pct

    from combined

)

select
    *,

    -- Window function: 3-month rolling attrition. Monthly attrition is noisy
    -- on a 500-person population; the rolling figure is what you'd actually
    -- put in front of a leadership team.
    round(
        avg(attrition_rate_pct) over (
            order by month_start
            rows between 2 preceding and current row
        )
    , 2) as attrition_rate_3mo_rolling,

    -- Window function: cumulative leavers year-to-date, resetting each year.
    sum(leavers) over (
        partition by extract(year from month_start)
        order by month_start
        rows between unbounded preceding and current row
    ) as leavers_ytd

from final
order by month_start
