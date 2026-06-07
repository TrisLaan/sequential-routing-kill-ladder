"""Arm A: per-step cheap shadow over a completed baseline_strong trajectory.

POST-HOC RECONSTRUCTION (confirmed design): we do NOT touch agent/baseline.py.
Each strong step's exact LLM input is a pure function of fields baseline.py
already stored, so we rebuild it and fire gpt-4.1-mini on the identical input.
Input purity, by baseline.py line:
  * schema_retrieval input = schema_link_messages(question, evidence, full_ddl);
    full_ddl is deterministic (baseline.py:75, full_schema_ddl).
  * linked_schema fed to draft/repair = the schema_retrieval step's output
    (baseline.py:97  `linked_schema = sr.text`), stored as step.output (:95).
  * draft input = draft_messages(question, evidence, linked_schema)
    (baseline.py:102-106); its output stored as step.output (:119).
  * repair input = repair_messages(..., prev_sql, error) where prev_sql is the
    prior step's output and error its exec_error (baseline.py:129-137, :152).
So reconstruction reproduces strong's inputs byte-for-byte; these shadow calls
cannot affect what strong did or saw (strong already finished).

Labels (Q1/Q2 confirmed):
  draft_sql / repair  -> matched = cheap_matches_strong (primary), plus
                         cheap_matches_gold, strong_matches_gold;  set_equality.
  schema_retrieval    -> matched = schema_superset_of_strong_draft (primary),
                         plus schema_superset_of_gold;  schema_superset_proxy.

Repair shadows on arm A are an EXPECTED empty set (baseline's first draft has
always executed, so there are no repair steps). Real draft<-repair shadow/branch
coverage comes from the live arms B/C, not here.
"""
from __future__ import annotations

import time

from agent import prompts, schema_proxy
from agent.baseline import _extract_sql, full_schema_ddl
from data import evaluate, executor

CHEAP_MODEL = "gpt-4.1-mini"


def shadow_baseline(task, trajectory_id: int, client, logger) -> int:
    """Attach cheap shadow rows to one finished baseline trajectory.

    Returns the number of shadow rows written.
    """
    conn = logger.conn
    steps = conn.execute(
        "SELECT step_id, step_index, decision_type, output, exec_error "
        "FROM step WHERE trajectory_id = ? ORDER BY step_index",
        (trajectory_id,),
    ).fetchall()

    by_index = {s[1]: s for s in steps}
    linked_schema = next((s[3] for s in steps if s[2] == "schema_retrieval"), None)
    strong_draft_sql = next((s[3] for s in steps if s[2] == "draft_sql"), None)
    # Baseline always emits exactly one schema_retrieval and one draft_sql step.
    assert linked_schema is not None, "baseline trajectory missing schema_retrieval"
    assert strong_draft_sql is not None, "baseline trajectory missing draft_sql"

    full_ddl, _ = full_schema_ddl(task.db_path)
    tvocab, cvocab = schema_proxy.schema_vocab(task.db_path)
    gold_rows, _ = executor.run_sql(task.db_path, task.gold_sql)

    n_written = 0
    for step_id, idx, dtype, out, _err in steps:
        if dtype == "schema_retrieval":
            msgs = prompts.schema_link_messages(task.question, task.evidence, full_ddl)
            t0 = time.perf_counter()
            resp = client.complete(CHEAP_MODEL, msgs)  # cheap: temperature path
            latency = int((time.perf_counter() - t0) * 1000)
            sup_gold = schema_proxy.is_superset(resp.text, task.gold_sql, tvocab, cvocab)
            sup_strong = schema_proxy.is_superset(
                resp.text, strong_draft_sql, tvocab, cvocab
            )
            logger.log_shadow(
                step_id=step_id,
                shadow_model=CHEAP_MODEL,
                prompt_tokens=resp.prompt_tokens,
                completion_tokens=resp.completion_tokens,
                shadow_output=resp.text,
                latency_ms=latency,
                matched=int(sup_strong),  # Q1: primary = superset_of_strong_draft
                match_method="schema_superset_proxy",
                schema_superset_of_gold=int(sup_gold),
                schema_superset_of_strong_draft=int(sup_strong),
            )
            n_written += 1

        elif dtype in ("draft_sql", "repair"):
            if dtype == "draft_sql":
                msgs = prompts.draft_messages(
                    task.question, task.evidence, linked_schema
                )
            else:
                prev = by_index[idx - 1]
                msgs = prompts.repair_messages(
                    task.question, task.evidence, linked_schema,
                    prev_sql=prev[3], error=prev[4],
                )
            t0 = time.perf_counter()
            resp = client.complete(CHEAP_MODEL, msgs)
            latency = int((time.perf_counter() - t0) * 1000)
            cheap_sql = _extract_sql(resp.text)
            strong_sql = out

            cheap_rows, _ = executor.run_sql(task.db_path, cheap_sql)
            strong_rows, _ = executor.run_sql(task.db_path, strong_sql)
            # REUSE evaluate.execution_match (no reimplementation).
            cms = evaluate.execution_match(strong_rows, cheap_rows, task.gold_sql, task.benchmark)
            cmg = evaluate.execution_match(gold_rows, cheap_rows, task.gold_sql, task.benchmark)
            smg = evaluate.execution_match(gold_rows, strong_rows, task.gold_sql, task.benchmark)
            logger.log_shadow(
                step_id=step_id,
                shadow_model=CHEAP_MODEL,
                prompt_tokens=resp.prompt_tokens,
                completion_tokens=resp.completion_tokens,
                shadow_output=cheap_sql,
                latency_ms=latency,
                matched=int(cms),  # primary = cheap_matches_strong
                match_method="set_equality",
                cheap_matches_gold=int(cmg),
                strong_matches_gold=int(smg),
            )
            n_written += 1
        # finalize steps never exist (baseline writes no finalize row); ignore.

    return n_written
