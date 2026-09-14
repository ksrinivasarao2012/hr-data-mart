-- Staging: departments. Small and already clean, but it gets a staging model
-- anyway so that every mart references stg_* and never a raw source directly.
-- That consistency is what makes the lineage graph readable.

select
    dept_id,
    trim(dept_name)   as dept_name,
    trim(cost_centre) as cost_centre
from {{ source('raw', 'departments') }}
