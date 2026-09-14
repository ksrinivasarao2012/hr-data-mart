# Setup — do these in order

No Docker required. Windows + native Postgres + your existing `.venv`.
Budget: ~25 minutes to a green pipeline.

---

## Step 1 — Install PostgreSQL  (~10 min)

Download the Windows installer: <https://www.postgresql.org/download/windows/>
(the EDB installer).

During install:
- **Port:** leave it at `5432`
- **Superuser password:** pick something simple and **write it down** — you need it in Step 2
- Components: PostgreSQL Server + Command Line Tools are enough. pgAdmin is optional but handy.

Verify (open a NEW PowerShell window so PATH refreshes):

```powershell
psql --version
```

If `psql` isn't found, add this to PATH (adjust the version number):
`C:\Program Files\PostgreSQL\16\bin`

---

## Step 2 — Create the database

```powershell
psql -U postgres -c "CREATE DATABASE hr_mart;"
```

It will prompt for the password from Step 1.

Verify:

```powershell
psql -U postgres -d hr_mart -c "SELECT version();"
```

---

## Step 3 — Configure `.env`

```powershell
cd D:\Swiggy\hr-data-mart
copy .env.example .env
notepad .env
```

Set `APP_DB_PASSWORD` to your Step 1 password. Get a free Groq key from
<https://console.groq.com> and paste it into `GROQ_API_KEY`.

(No Groq key? Fine — run with `--no-llm` and skip the enrichment. The dbt half
is the important half.)

### Step 3b — Load `.env` into your shell  ← DON'T SKIP THIS

`python-dotenv` reads `.env` inside **Python scripts**. dbt is a separate
program — its `env_var()` reads the real process environment, and it has no
idea your `.env` file exists.

Symptom if you skip this: the Python scripts connect fine, but `dbt debug`
fails with `password authentication failed` and you spend twenty minutes
convinced your password is wrong. It isn't.

Run this once **in every new terminal**:

```powershell
Get-Content .env | Where-Object { $_ -match '^\s*[^#].*=' } | ForEach-Object {
  $k,$v = $_ -split '=',2
  [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), "Process")
}
```

Verify:

```powershell
echo $env:APP_DB_NAME     # should print: hr_mart
```

(If your Postgres password happens to be literally `postgres`, the defaults in
`dbt/profiles.yml` cover you and this step is optional. Run it anyway — it also
loads your Groq key.)

---

## Step 4 — Install Python dependencies

```powershell
cd D:\Swiggy\hr-data-mart
.venv\Scripts\activate          # or ..\.venv\Scripts\activate if it's in D:\Swiggy
uv pip install -r requirements.txt
```

This now includes `dbt-core` and `dbt-postgres`, which weren't in the earlier
Docker-oriented version of the file.

Verify:

```powershell
dbt --version
```

---

## Step 5 — First run, step by step

Run these one at a time so you see what each does. Don't jump to
`run_pipeline.py` yet — you learn nothing from a script that works first time.

```powershell
# 5a. install the dbt_utils package
dbt deps --project-dir dbt --profiles-dir dbt

# 5b. check dbt can reach Postgres
dbt debug --project-dir dbt --profiles-dir dbt

# 5c. generate the messy data
python scripts/seed_data.py

# 5d. build the models
dbt run --project-dir dbt --profiles-dir dbt

# 5e. run the tests  -->  SOME OF THESE SHOULD FAIL. That is correct.
dbt test --project-dir dbt --profiles-dir dbt
```

### Expect step 5e to fail. That's the whole point.

The seed script injects five defects on purpose. A green test run on the first
try would mean the tests aren't checking anything. You should see failures from
`assert_no_negative_tenure` (DEFECT-3) and `assert_attendance_not_double_loaded`
(DEFECT-4), and a warning on the manager relationship test (DEFECT-2).

**Read each failure.** dbt prints the SQL it ran and how many rows came back.
Run that SQL yourself in psql and look at the actual bad rows. This is the
single most valuable 15 minutes in the whole build — it's what turns "I used
dbt" into "I debugged a dbt failure", which is what the interview is about.

---

## Step 6 — See the report

```powershell
psql -U postgres -d hr_mart -c "SELECT month_label, headcount_bom, headcount_eom, joiners, leavers, attrition_rate_pct, attrition_rate_3mo_rolling FROM analytics_marts.mart_attrition_scorecard ORDER BY month_start DESC LIMIT 12;"
```

Screenshot this. It goes in the README.

---

## Step 7 — LLM enrichment + eval  (skip if no Groq key)

```powershell
python scripts/enrich_surveys.py
python eval/run_eval.py
```

`run_eval.py` prints real accuracy numbers. **Put those exact numbers in your
README and your resume bullet — do not estimate them.**

Look at what got quarantined:

```powershell
psql -U postgres -d hr_mart -c "SELECT reject_reason, count(*) FROM raw.survey_quarantine GROUP BY 1 ORDER BY 2 DESC;"
```

---

## Step 8 — The full pipeline

Now run the orchestrator end to end:

```powershell
python run_pipeline.py
```

It will halt at `dbt_test` and refuse to publish. **That is the correct
behaviour** — screenshot it, it's your best single image for the README.

Then fix the defects (see below), re-run, and watch it go green.

---

## Step 9 — Fix the planted defects

This is real work, not busywork — it's what you'll be describing in the
interview.

- **DEFECT-3 (negative tenure):** decide the policy. Exclude the row from the
  mart *and* surface it in a data-quality table for HR to correct? Or hard-fail
  until a human fixes the source? Either is defensible. Write down which you
  chose and why — that written decision is worth more than the code.
- **DEFECT-4 (double-loaded day):** staging already dedupes it. Change
  `assert_attendance_not_double_loaded` to `severity: warn` so it flags the
  upstream problem without blocking the report, and note why in a comment.
- **DEFECT-2 (orphan manager):** already a warning. Leave it.

Re-run `python run_pipeline.py` until it's green.

---

## Step 10 — dbt docs (the screenshot that sells it)

```powershell
dbt docs generate --project-dir dbt --profiles-dir dbt
dbt docs serve --project-dir dbt --profiles-dir dbt --port 8081
```

Opens in your browser. Click **the lineage graph** (bottom-right icon).
Screenshot it. That single image communicates more than three paragraphs of
README.

---

## Troubleshooting

| Error | Fix |
|---|---|
| `psql: command not found` | Add `C:\Program Files\PostgreSQL\16\bin` to PATH, open a new terminal |
| `FATAL: password authentication failed` | `APP_DB_PASSWORD` in `.env` doesn't match your install password |
| `database "hr_mart" does not exist` | You skipped Step 2 |
| `Compilation Error ... dbt_utils` | You skipped `dbt deps` |
| `relation "raw.employees" does not exist` | You skipped `seed_data.py` |
| `dbt: command not found` | venv not activated, or `uv pip install -r requirements.txt` not run |
| `dbt debug` says auth failed but Python scripts work | You skipped Step 3b. dbt doesn't read `.env`. |
| Worked earlier, fails in a new terminal | Re-run Step 3b — process env vars don't persist across terminals |
| Groq `401` | Bad or missing key in `.env` |
| Groq `429` | Free-tier rate limit. Increase the `time.sleep` in `enrich_surveys.py` to `0.3` |
