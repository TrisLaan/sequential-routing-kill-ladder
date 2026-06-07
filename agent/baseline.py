"""Phase 1: the baseline_strong policy.

gpt-5.4 on every step, reasoning_effort="medium", no shadow. Flow:

    schema_retrieval -> draft_sql -> [repair]*  -> finalize

draft_sql and each repair execute their SQL against the task DB. The loop stops
when a query runs with no error OR the 3-repair cap is hit; then `finalize`
(deterministic, no step row) picks the last error-free SQL, or the last attempt
if none ran clean. Each repair links to its predecessor via retry_of_step_id,
forming the draft<-repair chain.

Only schema_retrieval / draft_sql / repair write `step` rows. finalize does not.
"""
from __future__ import annotations

import time
from typing import Protocol

import config  # noqa: F401  (kept for parity; cost is computed inside tracer)
from data import evaluate, executor
from agent import prompts
from agent.client import LLMResponse

MODEL = "gpt-5.4"
EFFORT = "medium"
MAX_REPAIRS = 3


class _Client(Protocol):
    def complete(
        self, model: str, messages: list[dict[str, str]], *, reasoning_effort: str | None = ...
    ) -> LLMResponse: ...


def full_schema_ddl(db_path: str) -> tuple[str, int]:
    """Return (joined CREATE TABLE DDL, n_tables) read read-only via executor."""
    rows, err = executor.run_sql(
        db_path,
        "SELECT sql FROM sqlite_master "
        "WHERE type='table' AND sql IS NOT NULL ORDER BY name",
    )
    if err is not None:
        raise RuntimeError(f"failed to read schema DDL from {db_path}: {err}")
    ddls = [r[0] for r in rows]
    return "\n\n".join(ddls), len(ddls)


def _extract_sql(text: str) -> str:
    """Strip markdown fences/labels the model may add despite instructions."""
    s = text.strip()
    if s.startswith("```"):
        s = s[3:]
        # drop an optional leading language tag like "sql\n"
        if "\n" in s:
            first, rest = s.split("\n", 1)
            if first.strip().lower() in ("sql", "sqlite", ""):
                s = rest
        if "```" in s:
            s = s[: s.index("```")]
    return s.strip()


def run_baseline(task, client: _Client, logger, *, max_repairs: int = MAX_REPAIRS):
    """Run baseline_strong on one Task. Returns (trajectory_id, success: bool)."""
    traj_id = logger.start_trajectory(
        task_id=task.task_id,
        db_id=task.db_id,
        benchmark=task.benchmark,
        policy_label="baseline_strong",
        gold_sql=task.gold_sql,
        notes="phase1 baseline_strong",
    )

    full_ddl, n_tables = full_schema_ddl(task.db_path)
    step_index = 0

    # --- schema_retrieval -------------------------------------------------
    t0 = time.perf_counter()
    sr = client.complete(
        MODEL,
        prompts.schema_link_messages(task.question, task.evidence, full_ddl),
        reasoning_effort=EFFORT,
    )
    logger.log_step(
        trajectory_id=traj_id,
        step_index=step_index,
        decision_type="schema_retrieval",
        action_model=MODEL,
        action_effort=EFFORT,
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
        MODEL,
        prompts.draft_messages(task.question, task.evidence, linked_schema),
        reasoning_effort=EFFORT,
    )
    sql = _extract_sql(draft.text)
    _, err = executor.run_sql(task.db_path, sql)
    last_step_id = logger.log_step(
        trajectory_id=traj_id,
        step_index=step_index,
        decision_type="draft_sql",
        action_model=MODEL,
        action_effort=EFFORT,
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
            MODEL,
            prompts.repair_messages(
                task.question, task.evidence, linked_schema, prev_sql, err
            ),
            reasoning_effort=EFFORT,
        )
        sql = _extract_sql(rep.text)
        _, err = executor.run_sql(task.db_path, sql)
        last_step_id = logger.log_step(
            trajectory_id=traj_id,
            step_index=step_index,
            decision_type="repair",
            action_model=MODEL,
            action_effort=EFFORT,
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

    # Evaluate final SQL vs gold via execution match. Re-execute both so the
    # comparison never depends on loop-local row state.
    gold_rows, _ = executor.run_sql(task.db_path, task.gold_sql)
    pred_rows, _ = executor.run_sql(task.db_path, final_sql)
    success = evaluate.execution_match(
        gold_rows, pred_rows, task.gold_sql, task.benchmark
    )

    logger.finish_trajectory(
        traj_id,
        final_pred_sql=final_sql,
        final_success=1 if success else 0,
        success_method="exec_match" if success else None,
    )
    return traj_id, success
