{% test unique_combination_of_columns(model, combination_of_columns) %}

-- A generic (reusable) test: asserts that a SET of columns is unique together.
-- dbt's built-in `unique` only handles a single column, and compound grain is
-- extremely common in fact tables.
--
-- Written by hand rather than pulled from dbt_utils so the project doesn't
-- depend on a package for its core grain check — and because writing a generic
-- test is the thing that shows you actually understand dbt's macro layer.

{% set columns_csv = combination_of_columns | join(', ') %}

select
    {{ columns_csv }},
    count(*) as n_rows
from {{ model }}
group by {{ columns_csv }}
having count(*) > 1

{% endtest %}
