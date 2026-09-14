"""
Classify free-text exit-survey responses into a fixed set of themes using an
LLM — with the guardrails that make the output safe for a pipeline to trust.

The classification itself is the easy part. The point of this script is
everything wrapped around it:

  GUARDRAIL 1  Closed label set.     The model may only return labels from
                                     ALLOWED_THEMES. Anything else is rejected,
                                     not "cleaned up" or fuzzy-matched.
  GUARDRAIL 2  Schema validation.    Output must parse as JSON and satisfy a
                                     Pydantic model. Prose, markdown fences and
                                     half-JSON are rejected.
  GUARDRAIL 3  Abstain path.         The model must be able to say "unclear".
                                     A model with no way to decline will invent
                                     an answer, and an invented reason for
                                     attrition is worse than no reason.
  GUARDRAIL 4  Confidence floor.     Below MIN_CONFIDENCE the row is quarantined
                                     even if it parsed cleanly.
  GUARDRAIL 5  Quarantine, not crash. Bad rows go to raw.survey_quarantine with
                                     the reason. The run continues. Downstream
                                     reports read only the clean table.

Outputs two tables:
    raw.survey_themes      — validated rows, one per (response_id, theme)
    raw.survey_quarantine  — rejected rows with a machine-readable reason

Run:  python scripts/enrich_surveys.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Literal

import psycopg2
from dotenv import load_dotenv
from groq import Groq
from psycopg2.extras import execute_values
from pydantic import BaseModel, Field, ValidationError, field_validator

load_dotenv()

# --- GUARDRAIL 1: the closed label set --------------------------------------
# Fixed, small, and mutually comprehensible. If HR wants a new theme they
# change this list and re-run — the model does not get to invent categories,
# because a category that appears in one month's report and not the next is
# useless for tracking a trend.
ALLOWED_THEMES = [
    "compensation",
    "manager",
    "workload",
    "growth",
    "culture",
    "personal",     # relocation, family — not an org problem
]

MIN_CONFIDENCE = 0.60          # GUARDRAIL 4
MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")


# --- GUARDRAIL 2: the output contract ---------------------------------------
class ThemeClassification(BaseModel):
    """The only shape we will accept back from the model."""

    themes: list[str] = Field(
        default_factory=list,
        description="Themes present in the response. Empty list means unclear.",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    abstain: bool = Field(
        default=False,
        description="True when the response is too vague to classify.",
    )

    @field_validator("themes")
    @classmethod
    def themes_must_be_allowed(cls, v: list[str]) -> list[str]:
        # GUARDRAIL 1 enforced in code, not just in the prompt.
        # A prompt is a request; a validator is a rule.
        bad = [t for t in v if t not in ALLOWED_THEMES]
        if bad:
            raise ValueError(f"invented theme(s) not in allowed set: {bad}")
        if len(v) > 3:
            raise ValueError(f"too many themes ({len(v)}); max 3")
        return v


SYSTEM_PROMPT = f"""You classify employee exit-survey responses into themes.

Allowed themes (use ONLY these exact strings):
{json.dumps(ALLOWED_THEMES, indent=2)}

Rules:
- Return between 0 and 3 themes. Most responses have 1 or 2.
- If the response is too vague, generic, or ambiguous to classify confidently,
  return an empty themes list and set "abstain": true.
- Do NOT invent themes outside the list above.
- "personal" is for relocation/family/health-neutral reasons that are not an
  organisational problem. Do not use it as a catch-all.
- confidence is your honest probability that your labels are correct, 0.0-1.0.

Respond with ONLY a JSON object, no markdown fences, no commentary:
{{"themes": ["..."], "confidence": 0.0, "abstain": false}}"""


def get_conn():
    return psycopg2.connect(
        host=os.getenv("APP_DB_HOST", "localhost"),
        port=os.getenv("APP_DB_PORT", "5432"),
        user=os.getenv("APP_DB_USER", "postgres"),
        password=os.getenv("APP_DB_PASSWORD", "postgres"),
        dbname=os.getenv("APP_DB_NAME", "hr_mart"),
    )


DDL = """
drop table if exists raw.survey_themes;
drop table if exists raw.survey_quarantine;

create table raw.survey_themes (
    response_id  integer,
    employee_id  integer,
    theme        text,
    confidence   numeric(4,3),
    model        text,
    scored_at    timestamp default now()
);

create table raw.survey_quarantine (
    response_id     integer,
    employee_id     integer,
    response_text   text,
    raw_model_output text,
    reject_reason   text,
    model           text,
    scored_at       timestamp default now()
);
"""


def classify(client: Groq, text: str) -> tuple[ThemeClassification | None, str, str | None]:
    """
    Returns (parsed_or_None, raw_output, reject_reason_or_None).

    Never raises on a bad model response — a malformed answer is DATA about the
    model, not an exception. Only genuine transport failures bubble up.
    """
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0.0,          # classification: determinism over variety
            max_tokens=150,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or ""
    except Exception as exc:                      # transport / rate limit / auth
        return None, "", f"api_error: {type(exc).__name__}: {exc}"

    # GUARDRAIL 2a — must be valid JSON
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, raw, "invalid_json"

    # GUARDRAIL 2b + 1 — must satisfy the schema and the closed label set
    try:
        parsed = ThemeClassification(**payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        return None, raw, f"schema_violation: {first.get('msg', 'unknown')}"

    # GUARDRAIL 3 — explicit abstention
    if parsed.abstain or not parsed.themes:
        return parsed, raw, "model_abstained"

    # GUARDRAIL 4 — confidence floor
    if parsed.confidence < MIN_CONFIDENCE:
        return parsed, raw, f"low_confidence: {parsed.confidence:.2f} < {MIN_CONFIDENCE}"

    return parsed, raw, None


def main():
    if not os.getenv("GROQ_API_KEY"):
        sys.exit("GROQ_API_KEY not set. Put it in .env and re-run.")

    client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(DDL)
    conn.commit()

    cur.execute(
        "select response_id, employee_id, response_text "
        "from raw.exit_survey_responses order by response_id"
    )
    rows = cur.fetchall()
    print(f"Classifying {len(rows)} exit-survey responses with {MODEL}...\n")

    clean_rows, quarantined = [], []

    for i, (response_id, employee_id, text) in enumerate(rows, 1):
        parsed, raw, reason = classify(client, text)

        if reason is not None:
            # GUARDRAIL 5 — quarantine and keep going. Never abort the batch.
            quarantined.append(
                (response_id, employee_id, text, raw[:2000], reason, MODEL)
            )
        else:
            for theme in parsed.themes:
                clean_rows.append(
                    (response_id, employee_id, theme, parsed.confidence, MODEL)
                )

        if i % 25 == 0:
            print(f"  {i}/{len(rows)}  clean={len(clean_rows)} quarantined={len(quarantined)}")
        time.sleep(0.05)      # be polite to the free tier

    if clean_rows:
        execute_values(
            cur,
            "insert into raw.survey_themes "
            "(response_id, employee_id, theme, confidence, model) values %s",
            clean_rows,
        )
    if quarantined:
        execute_values(
            cur,
            "insert into raw.survey_quarantine (response_id, employee_id, "
            "response_text, raw_model_output, reject_reason, model) values %s",
            quarantined,
        )
    conn.commit()

    total = len(rows)
    q = len(quarantined)
    print(f"\n--- Enrichment summary ---")
    print(f"  responses processed : {total}")
    print(f"  theme rows written  : {len(clean_rows)}")
    print(f"  quarantined         : {q}  ({100*q/total:.1f}%)")

    cur.execute(
        "select reject_reason, count(*) from raw.survey_quarantine "
        "group by 1 order by 2 desc"
    )
    print("\n  quarantine reasons:")
    for reason, n in cur.fetchall():
        print(f"    {n:>4}  {reason}")

    print("\nNOTE: a non-zero quarantine rate is the system WORKING, not failing.")
    print("      A 0% quarantine rate usually means the guardrails aren't checking anything.")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
