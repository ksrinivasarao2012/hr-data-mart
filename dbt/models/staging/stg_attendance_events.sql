-- Staging: attendance events.
--
-- Fixes DEFECT-4 (a day re-loaded from a re-run, with new event_ids but
-- otherwise identical rows).
--
-- Note WHY we can't dedupe on event_id: the re-run assigned fresh ids, so the
-- ids are unique and a `unique` test on event_id passes while the data is
-- still wrong. We dedupe on the BUSINESS key instead —
-- (employee_id, event_ts, event_type). This distinction (surrogate key unique
-- vs business key unique) is a classic interview question.

with source as (

    select * from {{ source('raw', 'attendance_events') }}

),

deduped as (

    select
        employee_id,
        event_ts,
        event_type,
        min(event_id)      as event_id,     -- keep the earliest-loaded id
        min(source_file)   as source_file,
        count(*)           as load_count    -- >1 means this row was re-loaded
    from source
    where employee_id is not null
      and event_ts is not null
      and event_type in ('check_in', 'check_out')
    group by employee_id, event_ts, event_type

)

select
    event_id,
    employee_id,
    event_ts,
    event_ts::date as event_date,
    event_type,
    source_file,
    load_count
from deduped
