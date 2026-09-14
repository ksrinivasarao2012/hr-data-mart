# ---------------------------------------------------------------------------
# Airflow image + a SEPARATE virtualenv for dbt.
#
# Why the separate venv: dbt-core and Airflow pin incompatible versions of
# jinja2 (and a few others). Installing both into the same environment either
# fails outright or produces an Airflow that won't start. Keeping dbt in
# /opt/dbt-venv means the two never see each other's packages, and the DAG
# just calls /opt/dbt-venv/bin/dbt by absolute path.
# ---------------------------------------------------------------------------

FROM apache/airflow:2.10.5-python3.11

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

USER airflow

# 1) Airflow-side extras, pinned against the official constraints file so we
#    can't accidentally drag Airflow's own dependencies to a broken version.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-2.10.5/constraints-3.11.txt"

# 2) dbt in its own isolated venv.
COPY requirements-dbt.txt /tmp/requirements-dbt.txt
RUN python -m venv /opt/dbt-venv \
    && /opt/dbt-venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/dbt-venv/bin/pip install --no-cache-dir -r /tmp/requirements-dbt.txt

ENV DBT_BIN=/opt/dbt-venv/bin/dbt \
    DBT_PROFILES_DIR=/opt/airflow/dbt \
    DBT_PROJECT_DIR=/opt/airflow/dbt
