{{ config(severity = 'warn') }}

-- SEVERITY DECISION: warn.
--
-- The staging model already deduplicates these rows, so no published number is
-- affected. But a non-idempotent upstream load will do this again next week,
-- and silence would mean nobody ever fixes the actual cause. Warning on every
-- run keeps the upstream problem visible without blocking a correct report.
--
-- Catches DEFECT-4 residue: if any (employee, timestamp, event_type) was seen
-- more than once in the source, the staging model deduped it — but we still
-- want to KNOW it happened, because a non-idempotent upstream load will do it
-- again next week.
--
-- This is the difference between fixing a symptom and noticing a pattern.
-- Configured as a warning rather than an error in dbt_project.yml would also
-- be defensible; kept as an error here so the run visibly stops the first time.

select
    event_date,
    count(*)            as affected_events,
    max(load_count)     as max_times_loaded
from {{ ref('stg_attendance_events') }}
where load_count > 1
group by event_date
