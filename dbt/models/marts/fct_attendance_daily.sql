-- Fact: one row per employee per day worked.
--
-- Pivots the check_in / check_out event pairs into a single day row with hours.
-- Uses conditional aggregation (min/max with a filter) rather than a self-join,
-- which is both faster and immune to the row-fanout bug a self-join invites.

with events as (

    select * from {{ ref('stg_attendance_events') }}

),

daily as (

    select
        employee_id,
        event_date,
        min(event_ts) filter (where event_type = 'check_in')  as first_check_in,
        max(event_ts) filter (where event_type = 'check_out') as last_check_out,
        count(*) filter (where event_type = 'check_in')       as check_in_count,
        max(load_count)                                       as max_load_count
    from events
    group by employee_id, event_date

),

with_hours as (

    select
        employee_id,
        event_date,
        first_check_in,
        last_check_out,
        check_in_count,
        max_load_count,
        round(
            extract(epoch from (last_check_out - first_check_in)) / 3600.0
        , 2) as hours_worked
    from daily
    where first_check_in is not null
      and last_check_out is not null
      and last_check_out > first_check_in

)

select
    h.employee_id,
    h.event_date,
    h.first_check_in,
    h.last_check_out,
    h.hours_worked,
    h.max_load_count,
    e.dept_name,
    e.job_level,

    -- Window function: 5-day rolling average hours per employee.
    -- This is the shape of question a scorecard actually asks
    -- ("is this team trending toward burnout?").
    round(avg(h.hours_worked) over (
        partition by h.employee_id
        order by h.event_date
        rows between 4 preceding and current row
    )::numeric, 2) as hours_worked_5d_avg

from with_hours h
left join {{ ref('dim_employee') }} e
    on h.employee_id = e.employee_id
