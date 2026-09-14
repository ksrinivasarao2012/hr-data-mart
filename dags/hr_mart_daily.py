"""
Airflow DAG for the HR data mart.

HONESTY NOTE — read this before claiming Airflow experience from this repo.

This DAG mirrors run_pipeline.py exactly: same task names, same dependency
order, same failure semantics. It is written to be correct and it parses
cleanly, but it has NOT been executed on a live Airflow scheduler in this
project, because Airflow does not run natively on Windows and Docker was not
available on the build machine.

What you can honestly say about this:
    "I wrote the DAG and I understand the execution model — task dependencies,
     trigger rules, on_failure_callback, why the quality gate sits upstream of
     the publish step. I've orchestrated this pipeline in practice with a
     Python runner; I haven't operated an Airflow scheduler in production."

That answer is credible and it costs you nothing. Claiming production Airflow
experience and then fumbling a question about backfills or scheduler behaviour
costs you the interview.

To actually run it: docker compose up with the Dockerfile in this repo (needs
Docker Desktop + WSL2 on Windows).
"""

from __future__ import annotations

import pendulum
from airflow.models.dag import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator

DBT_BIN = "/opt/dbt-venv/bin/dbt"      # dbt lives in its own venv — see Dockerfile
DBT_DIR = "/opt/airflow/dbt"
SCRIPTS = "/opt/airflow/scripts"

DBT_FLAGS = f"--project-dir {DBT_DIR} --profiles-dir {DBT_DIR}"


def alert_on_failure(context):
    """
    on_failure_callback — what gets you paged.

    In production this posts to Slack / PagerDuty. The important property is
    that it names the task and the run, so the person woken up at 6am knows
    where to look before they open a laptop.
    """
    ti = context["task_instance"]
    print(
        f"ALERT: task {ti.task_id} failed in dag {ti.dag_id} "
        f"(run {context['run_id']}). Scorecard NOT published. "
        f"Logs: {ti.log_url}"
    )


default_args = {
    "owner": "data-platform",
    "retries": 1,
    "retry_delay": pendulum.duration(minutes=5),
    "on_failure_callback": alert_on_failure,
}

with DAG(
    dag_id="hr_mart_daily",
    description="Build and quality-gate the HR attrition scorecard",
    default_args=default_args,
    start_date=pendulum.datetime(2026, 1, 1, tz="Asia/Kolkata"),
    schedule="0 6 * * *",          # 06:00 IST daily
    catchup=False,                 # no backfill — this is a snapshot report
    max_active_runs=1,             # never let two runs write the mart at once
    tags=["hr", "dbt", "llm"],
) as dag:

    dbt_deps = BashOperator(
        task_id="dbt_deps",
        bash_command=f"{DBT_BIN} deps {DBT_FLAGS}",
    )

    seed_raw_data = BashOperator(
        task_id="seed_raw_data",
        bash_command=f"python {SCRIPTS}/seed_data.py",
    )

    dbt_run = BashOperator(
        task_id="dbt_run",
        bash_command=f"{DBT_BIN} run {DBT_FLAGS}",
    )

    # The LLM step is allowed to fail without killing the run: it enriches the
    # report, it does not produce it. If the provider is down, headcount and
    # attrition still publish — only the theme breakdown is missing.
    llm_enrich_surveys = BashOperator(
        task_id="llm_enrich_surveys",
        bash_command=f"python {SCRIPTS}/enrich_surveys.py",
        trigger_rule="all_success",
        on_failure_callback=alert_on_failure,
    )

    # THE QUALITY GATE. Everything upstream computes numbers; this decides
    # whether anyone is allowed to see them.
    dbt_test = BashOperator(
        task_id="dbt_test",
        bash_command=f"{DBT_BIN} test {DBT_FLAGS}",
        trigger_rule="all_done",   # run the tests even if the LLM step failed
    )

    # Only runs when dbt_test passed. This ordering IS the project.
    publish_docs = BashOperator(
        task_id="publish_docs",
        bash_command=f"{DBT_BIN} docs generate {DBT_FLAGS}",
        trigger_rule="all_success",
    )

    dbt_deps >> seed_raw_data >> dbt_run >> llm_enrich_surveys >> dbt_test >> publish_docs
