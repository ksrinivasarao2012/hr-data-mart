-- Staging: exit survey free text.
-- Light normalisation only. The actual meaning is extracted downstream by the
-- LLM enrichment step (scripts/enrich_surveys.py), which writes into
-- raw.survey_themes and raw.survey_quarantine.

select
    response_id,
    employee_id,
    submitted_at,
    submitted_at::date                      as submitted_date,
    regexp_replace(trim(response_text), '\s+', ' ', 'g') as response_text,
    length(trim(response_text))             as response_length
from {{ source('raw', 'exit_survey_responses') }}
where response_text is not null
  and length(trim(response_text)) > 0
