"""Live counterfactual arms (policy='shadow'): full trajectories, not shadows.

Mirrors the baseline_strong loop in agent/baseline.py exactly (schema_retrieval
-> draft_sql -> repair x<=3 -> deterministic finalize), but selects the model per
step so we can run cheaper configurations end to end and see the real cascade.

  arm='cheap_all'        : gpt-4.1-mini at schema_retrieval, draft_sql, repair.
  arm='cheap_no_schema'  : gpt-5.4 (reasoning, medium) at schema_retrieval;
                           gpt-4.1-mini at draft_sql + repair.

These are real trajectories: trajectory + step rows logged normally, repairs
linked via retry_of_step_id. No shadow rows (shadow is arm A only). Costs roll up
into trajectory totals scoped by (policy='shadow', arm).
"""
from __future__ import annotations

import time

from agent import prompts
from agent.baseline import MAX_REPAIRS, _extract_sql, full_schema_ddl
from data import evaluate, executor

STRONG = "gpt-5.4"
CHEAP = "gpt-4.1-mini"
MEDIUM = "medium"

# (model, effort) per step group. effort=None -> cheap/ordinary temperature path.
ARM_CONFIG: dict[str, dict[str, tuple[str, str | None]]] = {
    "cheap_all": {
        "schema": (CHEAP, None),
        "body": (CHEAP, None),
    },
    "cheap_no_schema": {
        "schema": (STRONG, MEDIUM),
        "body": (CHEAP, None),
    },
}


def run_arm(task, client, logger, *, arm: str, max_repairs: int = MAX_REPAIRS, notes: str | None = None):
    """Run one counterfactual arm on a task. Returns (trajectory_id, success)."""
    cfg = ARM_CONFIG[arm]
    schema_model, schema_effort = cfg["schema"]
    body_model, body_effort = cfg["body"]

    traj_id = logger.start_trajectory(
        task_id=task.task_id,
        db_id=task.db_id,
        benchmark=task.benchmark,
        policy_label="shadow",
        arm=arm,
        gold_sql=task.gold_sql,
        notes=notes or f"phase2 {arm}",
    )

    full_ddl, n_tables = full_schema_ddl(task.db_path)
    step_index = 0

    # --- schema_retrieval -------------------------------------------------
    t0 = time.perf_counter()
    sr = client.complete(
        schema_model,
        prompts.schema_link_messages(task.question, task.evidence, full_ddl),
        reasoning_effort=schema_effort,
    )
    logger.log_step(
        trajectory_id=traj_id,
        step_index=step_index,
        decision_type="schema_retrieval",
        action_model=schema_model,
        action_effort=schema_effort,
        prompt_tokens=sr.prompt_tokens,
        completion_tokens=sr.completion_tokens,
        state_features={"n_tables": n_tables, "has_evidence": bool(task.evidence)},
        latency_ms=int((time.perf_counter() - t0) * 1000),
        output=sr.text,
    )
    linked_schema = sr.text
    step_index += 1

    # --- draft_sql --------------------------------------------------------
    t0 = time.perf_counter()
    draft = client.complete(
        body_model,
        prompts.draft_messages(task.question, task.evidence, linked_schema),
        reasoning_effort=body_effort,
    )
    sql = _extract_sql(draft.text)
    _, err = executor.run_sql(task.db_path, sql)
    last_step_id = logger.log_step(
        trajectory_id=traj_id,
        step_index=step_index,
        decision_type="draft_sql",
        action_model=body_model,
        action_effort=body_effort,
        prompt_tokens=draft.prompt_tokens,
        completion_tokens=draft.completion_tokens,
        state_features={"linked_schema_chars": len(linked_schema)},
        latency_ms=int((time.perf_counter() - t0) * 1000),
        output=sql,
        exec_error=err,
    )
    step_index += 1
    last_clean_sql = sql if err is None else None

    # --- repair loop ------------------------------------------------------
    attempts = 0
    while err is not None and attempts < max_repairs:
        prev_sql = sql
        t0 = time.perf_counter()
        rep = client.complete(
            body_model,
            prompts.repair_messages(
                task.question, task.evidence, linked_schema, prev_sql, err
            ),
            reasoning_effort=body_effort,
        )
        sql = _extract_sql(rep.text)
        _, err = executor.run_sql(task.db_path, sql)
        last_step_id = logger.log_step(
            trajectory_id=traj_id,
            step_index=step_index,
            decision_type="repair",
            action_model=body_model,
            action_effort=body_effort,
            prompt_tokens=rep.prompt_tokens,
            completion_tokens=rep.completion_tokens,
            state_features={"attempt": attempts + 1, "prev_error": err is not None},
            latency_ms=int((time.perf_counter() - t0) * 1000),
            output=sql,
            exec_error=err,
            retry_of_step_id=last_step_id,
        )
        step_index += 1
        attempts += 1
        if err is None:
            last_clean_sql = sql

    # --- finalize (deterministic, no step row) ----------------------------
    final_sql = last_clean_sql if last_clean_sql is not None else sql
    gold_rows, _ = executor.run_sql(task.db_path, task.gold_sql)
    pred_rows, _ = executor.run_sql(task.db_path, final_sql)
    success = evaluate.execution_match(gold_rows, pred_rows, task.gold_sql, task.benchmark)

    logger.finish_trajectory(
        traj_id,
        final_pred_sql=final_sql,
        final_success=1 if success else 0,
        success_method="exec_match" if success else None,
    )
    return traj_id, success
