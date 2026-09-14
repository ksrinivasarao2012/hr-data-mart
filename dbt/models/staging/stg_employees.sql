-- Staging: clean up raw.employees. Cleanup ONLY — no business logic.
--
-- Fixes DEFECT-1 (duplicate rows from upstream re-inserts on team transfer).
--
-- Dedupe strategy: keep the most recently loaded row per employee_id.
-- row_number() over a partition is the standard dbt dedupe pattern — worth
-- being able to write it from memory in an interview.

with source as (

    -- ctid is a Postgres SYSTEM column: it exists on a physical table but is
    -- not included by `select *`, so it must be named explicitly here or it
    -- won't survive into the CTE below.
    select
        *,
        ctid as _physical_row_id
    from {{ source('raw', 'employees') }}

),

ranked as (

    select
        employee_id,
        trim(full_name)                as full_name,
        lower(trim(email))             as email,
        dept_id,
        manager_id,
        job_level,
        location,
        join_date,

        -- FIX for DEFECT-3c: the upstream system writes 1900-01-01 as a
        -- sentinel instead of NULL for "has not left". Mapping it here, at the
        -- staging layer, fixes it once for every downstream model. Fixing it in
        -- each mart instead would mean fixing it again every time someone adds
        -- a new mart — and forgetting once.
        nullif(exit_date, date '1900-01-01') as exit_date,

        loaded_at,

        row_number() over (
            partition by employee_id
            -- loaded_at is identical across the injected duplicates, so it
            -- alone is not a deterministic tiebreak. _physical_row_id makes
            -- the choice stable: without it, two runs could keep different
            -- rows and the dedupe would be non-reproducible.
            order by loaded_at desc, _physical_row_id desc
        ) as rn

    from source
    where employee_id is not null

)

select
    employee_id,
    full_name,
    email,
    dept_id,
    manager_id,
    job_level,
    location,
    join_date,
    exit_date,
    loaded_at
from ranked
where rn = 1
