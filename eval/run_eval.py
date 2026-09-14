"""
Measure how good the LLM classifier actually is.

This is the part the JD calls out explicitly: "building evals so we know when
the model is confidently wrong."

40 hand-labelled responses in gold_labels.csv, including 5 deliberately vague
ones labelled ABSTAIN. Those 5 matter most: they measure whether the model
knows when NOT to answer. A classifier that scores 95% on clear cases and
confidently mislabels every vague one is worse in production than a classifier
that scores 85% and abstains correctly — because the confident wrong answers
are the ones that reach a leadership deck.

Metrics reported:
  exact_match      — predicted theme set == gold theme set
  partial_match    — at least one predicted theme is correct (multi-label reality)
  abstain_recall   — of the rows that SHOULD be abstained, how many were
  false_confidence — vague rows the model confidently labelled anyway  <- the
                     single number to care about
  per-theme precision / recall / F1

Run:  python eval/run_eval.py
"""

from __future__ import annotations

import csv
import os
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq
from tabulate import tabulate

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.enrich_surveys import ALLOWED_THEMES, MODEL, classify  # noqa: E402

load_dotenv()

GOLD_PATH = Path(__file__).parent / "gold_labels.csv"


def load_gold():
    rows = []
    with open(GOLD_PATH, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            gold = r["gold_themes"].strip()
            rows.append(
                {
                    "text": r["response_text"],
                    "gold": set() if gold == "ABSTAIN" else set(gold.split("|")),
                    "should_abstain": gold == "ABSTAIN",
                }
            )
    return rows


def main():
    if not os.getenv("GROQ_API_KEY"):
        sys.exit("GROQ_API_KEY not set. Put it in .env and re-run.")

    client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    gold_rows = load_gold()
    print(f"Evaluating {MODEL} on {len(gold_rows)} hand-labelled responses...\n")

    exact = partial = 0
    abstain_should = abstain_did = 0
    false_confidence = 0
    rejected = 0

    tp = defaultdict(int)
    fp = defaultdict(int)
    fn = defaultdict(int)

    failures = []

    for row in gold_rows:
        parsed, raw, reason = classify(client, row["text"])

        # Treat any guardrail rejection (abstain, low confidence, schema fail)
        # as "the system declined to label this" — which is exactly how the
        # pipeline treats it too. The eval must mirror production behaviour.
        predicted = set()
        declined = reason is not None
        if reason is not None and not reason.startswith(("model_abstained", "low_confidence")):
            rejected += 1
        if not declined and parsed is not None:
            predicted = set(parsed.themes)

        if row["should_abstain"]:
            abstain_should += 1
            if declined:
                abstain_did += 1
            else:
                # The dangerous case: a vague response given a confident label.
                false_confidence += 1
                failures.append((row["text"][:55], "ABSTAIN", "|".join(sorted(predicted))))
        else:
            if predicted == row["gold"]:
                exact += 1
            elif predicted & row["gold"]:
                partial += 1
                failures.append((row["text"][:55], "|".join(sorted(row["gold"])), "|".join(sorted(predicted))))
            else:
                failures.append((row["text"][:55], "|".join(sorted(row["gold"])), "|".join(sorted(predicted)) or "(declined)"))

            for t in ALLOWED_THEMES:
                if t in predicted and t in row["gold"]:
                    tp[t] += 1
                elif t in predicted:
                    fp[t] += 1
                elif t in row["gold"]:
                    fn[t] += 1

    n_labelled = len(gold_rows) - abstain_should

    print("=== Headline ===")
    print(tabulate(
        [
            ["exact match (labelled rows)", f"{exact}/{n_labelled}", f"{100*exact/n_labelled:.1f}%"],
            ["partial match (>=1 correct)", f"{exact+partial}/{n_labelled}", f"{100*(exact+partial)/n_labelled:.1f}%"],
            ["abstain recall (vague rows)", f"{abstain_did}/{abstain_should}", f"{100*abstain_did/abstain_should:.1f}%"],
            ["FALSE CONFIDENCE", f"{false_confidence}/{abstain_should}", f"{100*false_confidence/abstain_should:.1f}%"],
            ["guardrail rejections", rejected, ""],
        ],
        headers=["metric", "count", "rate"],
        tablefmt="github",
    ))

    print("\n=== Per-theme ===")
    table = []
    for t in ALLOWED_THEMES:
        prec = tp[t] / (tp[t] + fp[t]) if (tp[t] + fp[t]) else 0.0
        rec = tp[t] / (tp[t] + fn[t]) if (tp[t] + fn[t]) else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        table.append([t, tp[t], fp[t], fn[t], f"{prec:.2f}", f"{rec:.2f}", f"{f1:.2f}"])
    print(tabulate(table, headers=["theme", "TP", "FP", "FN", "prec", "rec", "F1"], tablefmt="github"))

    if failures:
        print("\n=== Disagreements (read these — they tell you what to fix) ===")
        print(tabulate(failures, headers=["response (truncated)", "gold", "predicted"], tablefmt="github"))

    print(
        "\nPut the FALSE CONFIDENCE number in your README. It is the number that"
        "\ndecides whether this pipeline can be trusted to run unattended."
    )


if __name__ == "__main__":
    main()
