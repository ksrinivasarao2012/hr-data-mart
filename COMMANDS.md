# Commands — HR Data Mart

Everything below assumes you're in the `hr-data-mart/` folder.
Windows users: use PowerShell. `make` targets are optional — the raw
`docker compose` command is shown under each one.

---

## 0. One-time setup

```powershell
# copy the env template and put your Groq key in it
copy .env.example .env
# (mac/linux: cp .env.example .env)
```

Then edit `.env` and paste your `GROQ_API_KEY`.

Check Docker is running:

```powershell
docker --version
docker compose version
```

---

## 1. Build the image  (~5–8 min the first time)

```powershell
docker compose build
```

This installs Airflow's extras, then dbt into its own venv at `/opt/dbt-venv`.
If it fails on the constraints URL, you're offline — check your connection and
re-run; do not remove the `--constraint` flag, it's what keeps Airflow bootable.

---

## 2. Start the stack

```powershell
docker compose up -d
docker compose logs -f airflow     # watch until you see "Airflow is ready"
```

Airflow UI: <http://localhost:8080> — user `admin`, password `admin`.

> Note: `command: standalone` prints a generated password to the logs on some
> versions. If `admin/admin` is rejected, grep the logs:
> `docker compose logs airflow | findstr -i password`

Postgres (your HR data) is on **localhost:5433** — connect with DBeaver/psql
using `hr_user` / `hr_pass` / `hr_mart` if you want to poke at tables directly.

---

## 3. Generate the messy data

```powershell
docker compose exec airflow python /opt/airflow/scripts/seed_data.py
```

This creates the `raw` schema: `employees`, `departments`,
`attendance_events`, `exit_survey_responses` — **with deliberate defects**
(duplicate employees, orphan manager ids, an exit date before a join date,
double-loaded attendance days). Those defects are the point; they're what your
tests catch.

Verify:

```powershell
docker compose exec postgres_app psql -U hr_user -d hr_mart -c "\dt raw.*"
docker compose exec postgres_app psql -U hr_user -d hr_mart -c "select count(*) from raw.employees;"
```

---

## 4. Run dbt by hand (before wiring Airflow)

Always develop dbt manually first. Only once it's green do you put it in a DAG.

```powershell
# does the connection work at all?
docker compose exec airflow /opt/dbt-venv/bin/dbt debug --project-dir /opt/airflow/dbt --profiles-dir /opt/airflow/dbt

# build every model
docker compose exec airflow /opt/dbt-venv/bin/dbt run --project-dir /opt/airflow/dbt --profiles-dir /opt/airflow/dbt

# run every test
docker compose exec airflow /opt/dbt-venv/bin/dbt test --project-dir /opt/airflow/dbt --profiles-dir /opt/airflow/dbt
```

Useful while iterating on one model:

```powershell
# build just one model
... dbt run --select stg_employees ...

# build a model AND everything downstream of it
... dbt run --select stg_employees+ ...

# build a model and everything it depends on
... dbt run --select +mart_attrition_scorecard ...

# see the compiled SQL dbt actually sent to Postgres (read this when a
# test fails — it's the single most useful debugging step in dbt)
... dbt compile --select mart_attrition_scorecard ...
# then open dbt/target/compiled/hr_mart/models/marts/mart_attrition_scorecard.sql
```

---

## 5. Generate the docs site (the screenshot for your README)

```powershell
docker compose exec airflow /opt/dbt-venv/bin/dbt docs generate --project-dir /opt/airflow/dbt --profiles-dir /opt/airflow/dbt
docker compose exec airflow /opt/dbt-venv/bin/dbt docs serve --project-dir /opt/airflow/dbt --profiles-dir /opt/airflow/dbt --port 8081 --no-browser
```

Add `- "8081:8081"` under the airflow service's `ports:` in
`docker-compose.yml` first, then open <http://localhost:8081>.
Screenshot the **lineage graph**. That one image sells the project.

---

## 6. Run the LLM enrichment + eval

```powershell
# tag the exit surveys (writes clean rows + a quarantine table)
docker compose exec airflow python /opt/airflow/scripts/enrich_surveys.py

# score the LLM against your 40 hand-labelled rows
docker compose exec airflow python /opt/airflow/eval/run_eval.py
```

The eval prints accuracy, abstain rate, and a per-label breakdown. **Put those
real numbers in your README and your resume bullet — don't guess them.**

---

## 7. Run the whole DAG

```powershell
# list DAGs (confirms Airflow parsed your file without syntax errors)
docker compose exec airflow airflow dags list

# trigger a run now
docker compose exec airflow airflow dags trigger hr_mart_daily

# watch it in the UI, or from the CLI:
docker compose exec airflow airflow dags list-runs -d hr_mart_daily
```

Test one task in isolation without a full DAG run (very handy):

```powershell
docker compose exec airflow airflow tasks test hr_mart_daily dbt_test 2026-09-14
```

---

## 8. Break it on purpose  ← do not skip this

This is where your interview story comes from.

```powershell
# inject a duplicate employee that violates the unique test
docker compose exec postgres_app psql -U hr_user -d hr_mart -c "insert into raw.employees select * from raw.employees limit 1;"

# run the DAG again -- dbt_test should FAIL and the publish step should be skipped
docker compose exec airflow airflow dags trigger hr_mart_daily
```

In the UI: click the red square → **Logs** → read the dbt failure → note which
test failed and how many rows it found. **Screenshot the red DAG.** Then fix it
(the dedupe in `stg_employees` should handle it) and re-run to green.

Be able to answer, out loud: *"what broke, how did I find it, how did I fix it,
and what would have happened if the test hadn't been there?"*

---

## 9. Shutting down

```powershell
docker compose down        # stop, keep the data
docker compose down -v     # stop and wipe the databases (fresh start)
```

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `port is already allocated` | Something else is on 8080 or 5433. Change the left-hand number in `ports:`. |
| Airflow container restarts forever | Metadata DB not ready. `docker compose down -v` then `up -d` again. |
| `dbt: command not found` | You called `dbt` not `/opt/dbt-venv/bin/dbt`. dbt is not on Airflow's PATH — that's deliberate. |
| dbt `Database Error: relation "raw.employees" does not exist` | You skipped step 3. Run the seed script. |
| DAG not visible in UI | Python syntax error in `dags/`. `docker compose exec airflow airflow dags list-import-errors` |
| Groq call fails with 401 | `.env` missing or key not picked up. `docker compose down && docker compose up -d` after editing `.env`. |
| Build is very slow | Normal on first run. Subsequent builds use the layer cache unless you edit a requirements file. |

---

## Version note

The pins in `requirements.txt` / `requirements-dbt.txt` are versions I believe
are compatible (Airflow 2.10.5 on Python 3.11 with dbt-core 1.9.x in an
isolated venv), but I can't verify them against a live index from here —
**if `docker compose build` fails on a version resolution error, read the error
and bump that one package rather than unpinning everything.** The isolation
between the two environments is the part that matters; the exact patch versions
are not load-bearing.
