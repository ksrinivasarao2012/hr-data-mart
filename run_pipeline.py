"""
Pipeline runner.

This does the job Airflow would do — run steps in order, stop on failure, log
what happened — without requiring Docker or WSL, so it runs on plain Windows.

It is deliberately NOT pretending to be Airflow. The equivalent Airflow DAG is
in dags/hr_mart_daily.py with the same task names and the same dependency
order, so the two are directly comparable. See the README for an honest note on
which of the two has actually been executed.

THE RULE THIS FILE EXISTS TO ENFORCE:
    if the tests fail, the report is NOT published.

That single behaviour is the entire point of the project. A pipeline that
publishes whatever it computed is just a script; a pipeline that refuses to
publish a number it cannot vouch for is a data platform.

Usage:
    python run_pipeline.py              # full run
    python run_pipeline.py --no-llm     # skip the LLM step (no API key needed)
    python run_pipeline.py --no-seed    # keep existing data, just rebuild
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent
DBT_DIR = ROOT / "dbt"
PY = sys.executable


class TaskFailed(Exception):
    pass


def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {level:<7} {msg}", flush=True)


def run_task(name: str, cmd: list[str], allow_fail: bool = False) -> int:
    """Run one task. Raise TaskFailed on non-zero exit unless allow_fail."""
    log(f"START  {name}")
    log(f"        $ {' '.join(str(c) for c in cmd)}")
    started = time.time()

    result = subprocess.run(cmd, cwd=ROOT)
    elapsed = time.time() - started

    if result.returncode != 0:
        if allow_fail:
            log(f"FAILED {name}  (exit {result.returncode}, {elapsed:.1f}s) — continuing", "WARN")
            return result.returncode
        log(f"FAILED {name}  (exit {result.returncode}, {elapsed:.1f}s)", "ERROR")
        raise TaskFailed(name)

    log(f"OK     {name}  ({elapsed:.1f}s)\n")
    return 0


def dbt(*args: str) -> list[str]:
    return ["dbt", *args, "--project-dir", str(DBT_DIR), "--profiles-dir", str(DBT_DIR)]


def alert(failed_task: str):
    """
    Stand-in for the alerting Airflow would do (email / Slack / PagerDuty).

    Prints loudly and writes a marker file. In a real deployment this is the
    on_failure_callback. Keeping it as an explicit, named step rather than a
    bare `except: pass` is the difference between a pipeline that fails and a
    pipeline that fails LOUDLY.
    """
    banner = "!" * 72
    print(f"\n{banner}")
    print(f"  PIPELINE HALTED — task `{failed_task}` failed")
    print(f"  The attrition scorecard was NOT published.")
    print(f"  Downstream consumers keep yesterday's numbers rather than")
    print(f"  receiving numbers we cannot vouch for.")
    print(f"{banner}\n")

    marker = ROOT / "PIPELINE_FAILED.txt"
    marker.write_text(
        f"Failed task : {failed_task}\n"
        f"Failed at   : {datetime.now().isoformat()}\n"
        f"Action      : inspect the dbt output above, fix the source data or\n"
        f"              the model, then re-run `python run_pipeline.py`.\n",
        encoding="utf-8",
    )
    log(f"wrote {marker.name}", "WARN")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-seed", action="store_true", help="skip data generation")
    ap.add_argument("--no-llm", action="store_true", help="skip the LLM enrichment step")
    args = ap.parse_args()

    marker = ROOT / "PIPELINE_FAILED.txt"
    if marker.exists():
        marker.unlink()

    started = datetime.now()
    print("=" * 72)
    print(f"  hr_mart_daily — run started {started:%Y-%m-%d %H:%M:%S}")
    print("=" * 72 + "\n")

    try:
        # ---- 1. dependencies -------------------------------------------------
        run_task("dbt_deps", dbt("deps"))

        # ---- 2. extract / load ----------------------------------------------
        if not args.no_seed:
            run_task("seed_raw_data", [PY, str(ROOT / "scripts" / "seed_data.py")])
        else:
            log("SKIP   seed_raw_data (--no-seed)\n")

        # ---- 3. transform ----------------------------------------------------
        run_task("dbt_run", dbt("run"))

        # ---- 4. LLM enrichment ----------------------------------------------
        # allow_fail=True on purpose: the LLM is an ENRICHMENT, not the report.
        # If Groq is down at 6am, the attrition scorecard should still publish
        # — it just won't have theme breakdowns. Deciding which steps are
        # allowed to fail is a design decision worth being able to defend.
        if not args.no_llm and os.getenv("GROQ_API_KEY"):
            run_task(
                "llm_enrich_surveys",
                [PY, str(ROOT / "scripts" / "enrich_surveys.py")],
                allow_fail=True,
            )
        else:
            log("SKIP   llm_enrich_surveys (no key or --no-llm)\n")

        # ---- 5. QUALITY GATE -------------------------------------------------
        # Everything above this line builds numbers. This line decides whether
        # anyone is allowed to see them.
        run_task("dbt_test", dbt("test"))

        # ---- 6. publish ------------------------------------------------------
        # Only reached when every test passed.
        run_task("dbt_docs_generate", dbt("docs", "generate"))
        log("PUBLISH  scorecard passed all quality gates and is live")

    except TaskFailed as exc:
        alert(str(exc))
        elapsed = (datetime.now() - started).total_seconds()
        print(f"Run FAILED after {elapsed:.1f}s")
        sys.exit(1)

    elapsed = (datetime.now() - started).total_seconds()
    print("\n" + "=" * 72)
    print(f"  hr_mart_daily — SUCCESS in {elapsed:.1f}s")
    print("=" * 72)
    print("\n  Inspect the report:")
    print('    psql -d hr_mart -c "select month_label, headcount_eom, joiners, '
          'leavers, attrition_rate_pct, attrition_rate_3mo_rolling '
          'from analytics_marts.mart_attrition_scorecard order by month_start desc limit 12;"')


if __name__ == "__main__":
    main()
