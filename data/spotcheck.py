"""Spot-check / acceptance CLI for the success signal.

Run from the sql-cost-opt/ directory:
    python -m data.spotcheck --benchmark bird --audit 50 --show 5 --seed 0

--audit N : sample N tasks, run each GOLD query twice through the executor and
            compare the two result sets with evaluate.execution_match. Gold-vs-
            gold must score ~100%; anything lower is a harness bug or a corrupt
            task, and the offending task_ids are printed.
--show K  : dump K of the sampled tasks side-by-side (question / evidence /
            gold_sql / returned rows) for manual annotation-noise auditing.
"""
from __future__ import annotations

import argparse
import random
import sys

from data import adapter, evaluate, executor

_ROW_PREVIEW = 10


def _sample(tasks: list, n: int, seed: int) -> list:
    if n >= len(tasks):
        return list(tasks)
    return random.Random(seed).sample(tasks, n)


def audit(tasks: list, benchmark: str):
    """Run each gold query twice and compare. Returns (passed, failures)."""
    passed = 0
    failures = []  # (task_id, db_id, reason)
    for t in tasks:
        rows1, err1 = executor.run_sql(t.db_path, t.gold_sql)
        rows2, err2 = executor.run_sql(t.db_path, t.gold_sql)
        err = err1 or err2
        if err is not None:
            failures.append((t.task_id, t.db_id, f"exec error: {err}"))
        elif evaluate.execution_match(rows1, rows2, t.gold_sql, benchmark):
            passed += 1
        else:
            failures.append(
                (t.task_id, t.db_id, "gold-vs-gold mismatch (nondeterministic order?)")
            )
    return passed, failures


def show(tasks: list) -> None:
    for t in tasks:
        rows, err = executor.run_sql(t.db_path, t.gold_sql)
        print("=" * 88)
        print(f"[{t.benchmark}] task_id={t.task_id}  db_id={t.db_id}  difficulty={t.difficulty}")
        print(f"Q        : {t.question}")
        print(f"evidence : {t.evidence}")
        print(f"gold_sql : {t.gold_sql}")
        if err is not None:
            print(f"ERROR    : {err}")
        else:
            print(f"rows     : {len(rows)} returned")
            for r in rows[:_ROW_PREVIEW]:
                print(f"   {r}")
            if len(rows) > _ROW_PREVIEW:
                print(f"   ... (+{len(rows) - _ROW_PREVIEW} more)")
    print("=" * 88)


def main() -> None:
    # BIRD has accented text (player names, etc.); keep the dump from dying on
    # the Windows console's default codepage.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="Success-signal spot-check / acceptance.")
    ap.add_argument("--benchmark", default="bird", choices=adapter.BENCHMARKS)
    ap.add_argument("--audit", type=int, default=50, metavar="N",
                    help="number of tasks to gold-vs-gold audit")
    ap.add_argument("--show", type=int, default=5, metavar="K",
                    help="number of sampled tasks to dump side-by-side")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    tasks = list(adapter.load_tasks(args.benchmark))
    print(f"loaded {len(tasks)} {args.benchmark} tasks from disk")

    sample = _sample(tasks, args.audit, args.seed)
    n = len(sample)
    passed, failures = audit(sample, args.benchmark)

    print()
    print(f"=== AUDIT: gold-vs-gold on {n} sampled tasks (seed={args.seed}) ===")
    pct = 100.0 * passed / n if n else 0.0
    print(f"passed: {passed}/{n}  ({pct:.1f}%)")
    if failures:
        print(f"FLAGGED {len(failures)} task(s):")
        for tid, db_id, reason in failures:
            print(f"   task_id={tid}  db_id={db_id}: {reason}")
    else:
        print("no failures -- harness clean on this sample.")

    print()
    print(f"=== SPOT-CHECK: {min(args.show, n)} sampled tasks side-by-side ===")
    show(sample[: args.show])


if __name__ == "__main__":
    main()
