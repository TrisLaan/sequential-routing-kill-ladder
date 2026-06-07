"""Real baseline_strong run over the first 20 BIRD dev tasks (dataset order).

Run from sql-cost-opt/ :
    python run_baseline.py

Spends real money on gpt-5.4 — only run once OPENAI_API_KEY is confirmed live.
Writes trajectories/steps to config.TRACE_DB_PATH (traces.sqlite).
"""
from __future__ import annotations

import itertools
import sys

import config
from agent.baseline import run_baseline
from agent.client import OpenAIClient
from data import adapter
from tracer import TraceLogger

N_TASKS = 20


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    tasks = list(itertools.islice(adapter.load_tasks("bird"), N_TASKS))
    print(f"loaded first {len(tasks)} BIRD tasks (dataset order)")

    client = OpenAIClient()
    logger = TraceLogger(str(config.TRACE_DB_PATH))

    n_success = 0
    for i, task in enumerate(tasks):
        traj_id, success = run_baseline(task, client, logger)
        n_success += int(success)
        flag = "OK " if success else "MISS"
        print(f"[{i + 1:2d}/{len(tasks)}] {flag} task_id={task.task_id} db_id={task.db_id}")

    total_cost = logger.conn.execute(
        "SELECT COALESCE(SUM(total_cost_usd),0.0) FROM trajectory"
    ).fetchone()[0]

    print()
    print(f"=== baseline_strong: {n_success}/{len(tasks)} exec-match "
          f"({100.0 * n_success / len(tasks):.1f}%) ===")
    print(f"total cost: ${total_cost:.4f}")
    print(f"traces written to: {config.TRACE_DB_PATH}")
    logger.close()


if __name__ == "__main__":
    main()
