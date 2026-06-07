"""Paid Phase 2 run: 3 arms per task over a seeded N=500 BIRD sample.

Run from sql-cost-opt/ (only after validate_phase2.py passes and the key is live):
    python run_phase2.py

Per task: baseline_strong (arm A) + its cheap per-step shadow, then the two live
counterfactual arms cheap_all / cheap_no_schema. Sampling is reproducible: BIRD
tasks are sorted by question_id BEFORE the seeded sample. Seed + sampled task_ids
are recorded to phase2_sample.json; the seed is also stamped into each
trajectory's notes.
"""
from __future__ import annotations

import itertools  # noqa: F401  (kept for parity with run_baseline)
import json
import random
import sys

import config
from agent.arms import run_arm
from agent.baseline import run_baseline
from agent.client import OpenAIClient
from agent.shadow import shadow_baseline
from data import adapter
from tracer import TraceLogger

SEED = 20240627
N_TASKS = 500


def _resume_prepare(conn) -> set[str]:
    """Make the run idempotent/resumable on a dedicated Phase-2 DB.

    A task is COMPLETE iff it has a cheap_no_schema trajectory — that arm is
    written last per task, so its presence implies baseline+shadow+cheap_all all
    landed. Any task with rows but no cheap_no_schema is INCOMPLETE (a prior run
    died mid-task); delete its partial trajectories so the re-run rebuilds it
    cleanly instead of duplicating. Returns the set of completed task_ids to skip.
    """
    done = {r[0] for r in conn.execute(
        "SELECT DISTINCT task_id FROM trajectory "
        "WHERE policy_label='shadow' AND arm='cheap_no_schema'"
    )}
    all_ids = {r[0] for r in conn.execute("SELECT DISTINCT task_id FROM trajectory")}
    incomplete = all_ids - done
    for tid in incomplete:
        trajs = [r[0] for r in conn.execute(
            "SELECT trajectory_id FROM trajectory WHERE task_id=?", (tid,))]
        for trj in trajs:
            conn.execute(
                "DELETE FROM shadow WHERE step_id IN "
                "(SELECT step_id FROM step WHERE trajectory_id=?)", (trj,))
            conn.execute("DELETE FROM step WHERE trajectory_id=?", (trj,))
            conn.execute("DELETE FROM trajectory WHERE trajectory_id=?", (trj,))
    conn.commit()
    if incomplete:
        print(f"resume: cleaned {len(incomplete)} incomplete task(s); "
              f"skipping {len(done)} already complete")
    return done


def _arm_costs(conn) -> dict[str, float]:
    rows = conn.execute(
        """
        SELECT CASE WHEN policy_label='baseline_strong' THEN 'baseline_strong'
                    ELSE 'shadow:'||COALESCE(arm,'?') END AS k,
               COALESCE(SUM(total_cost_usd),0)
        FROM trajectory GROUP BY k
        """
    ).fetchall()
    out = {r[0]: r[1] for r in rows}
    out["shadow_table"] = conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0) FROM shadow"
    ).fetchone()[0]
    return out


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    all_tasks = list(adapter.load_tasks("bird"))
    # Freeze ordering by question_id so the seed reproduces the same sample.
    all_tasks.sort(key=lambda t: int(t.task_id))
    if N_TASKS > len(all_tasks):
        raise SystemExit(f"N_TASKS={N_TASKS} exceeds dev set size {len(all_tasks)}")
    sample = random.Random(SEED).sample(all_tasks, N_TASKS)

    sampled_ids = [t.task_id for t in sample]
    first20 = {str(i) for i in range(20)}
    overlap = sorted([tid for tid in sampled_ids if tid in first20], key=int)
    sidecar = config.ROOT / "phase2_sample.json"
    sidecar.write_text(json.dumps(
        {"seed": SEED, "n": N_TASKS, "benchmark": "bird",
         "sampled_task_ids": sorted(sampled_ids, key=int),
         "phase1_first20_in_sample": overlap},
        indent=2,
    ), encoding="utf-8")
    print(f"seed={SEED}  sampled {len(sampled_ids)} BIRD tasks  "
          f"(Phase-1 first-20 in sample: {len(overlap)})  -> {sidecar.name}")

    client = OpenAIClient()
    logger = TraceLogger(str(config.PHASE2_DB_PATH))
    done = _resume_prepare(logger.conn)
    note = f"phase2 seed={SEED}"

    succ = {"baseline_strong": 0, "cheap_all": 0, "cheap_no_schema": 0}
    for i, task in enumerate(sample, 1):
        if task.task_id in done:
            continue  # already complete from a prior run (counts come from analyze)
        # Arm A: baseline (unchanged) + cheap shadow.
        bt, b_ok = run_baseline(task, client, logger)
        logger.conn.execute(
            "UPDATE trajectory SET notes=? WHERE trajectory_id=?",
            (f"{note} baseline_strong", bt),
        )
        logger.conn.commit()
        shadow_baseline(task, bt, client, logger)
        succ["baseline_strong"] += int(b_ok)
        # Arms B, C.
        _, ca_ok = run_arm(task, client, logger, arm="cheap_all", notes=f"{note} cheap_all")
        _, cns_ok = run_arm(task, client, logger, arm="cheap_no_schema", notes=f"{note} cheap_no_schema")
        succ["cheap_all"] += int(ca_ok)
        succ["cheap_no_schema"] += int(cns_ok)

        if i % 25 == 0 or i == len(sample):
            c = _arm_costs(logger.conn)
            print(f"[{i:3d}/{len(sample)}] "
                  f"strong={succ['baseline_strong']} ca={succ['cheap_all']} "
                  f"cns={succ['cheap_no_schema']} | "
                  f"$ strong={c.get('baseline_strong',0):.3f} "
                  f"ca={c.get('shadow:cheap_all',0):.3f} "
                  f"cns={c.get('shadow:cheap_no_schema',0):.3f} "
                  f"shadow={c['shadow_table']:.3f}")

    c = _arm_costs(logger.conn)
    print("\n=== Phase 2 run complete ===")
    for k in ("baseline_strong", "cheap_all", "cheap_no_schema"):
        key = k if k == "baseline_strong" else f"shadow:{k}"
        print(f"  {k:16s}: {succ[k]}/{len(sample)} exec-match  cost ${c.get(key,0):.4f}")
    print(f"  shadow-table cost (excluded from totals): ${c['shadow_table']:.4f}")
    print(f"  traces: {config.PHASE2_DB_PATH}")
    print("Run `python analyze_phase2.py` for deliverables #1-#7.")
    logger.close()


if __name__ == "__main__":
    main()
