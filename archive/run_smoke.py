"""Smoke test for the trace logger. In-memory DB, one trajectory, three writes."""
from __future__ import annotations

from tracer import TraceLogger


def main() -> None:
    logger = TraceLogger(":memory:")

    traj_id = logger.start_trajectory(
        task_id="smoke-001",
        db_id="california_schools",
        benchmark="bird",
        policy_label="baseline_strong",
        gold_sql="SELECT COUNT(*) FROM schools WHERE County = 'Alameda';",
        notes="smoke test",
    )

    draft_step_id = logger.log_step(
        trajectory_id=traj_id,
        step_index=0,
        decision_type="draft_sql",
        action_model="gpt-5.4",
        action_effort="medium",
        prompt_tokens=1200,
        completion_tokens=80,
        state_features={"schema_tokens": 850, "n_tables": 3},
        latency_ms=2400,
        output="SELECT COUNT(*) FROM school WHERE County = 'Alameda';",
        exec_error="no such table: school",
    )

    logger.log_shadow(
        step_id=draft_step_id,
        shadow_model="gpt-4.1-mini",
        prompt_tokens=1200,
        completion_tokens=75,
        shadow_output="SELECT COUNT(*) FROM schools WHERE County = 'Alameda';",
        latency_ms=900,
        matched=0,
        match_method="string",
    )

    logger.log_step(
        trajectory_id=traj_id,
        step_index=1,
        decision_type="repair",
        action_model="gpt-5.4",
        action_effort="medium",
        prompt_tokens=1350,
        completion_tokens=90,
        state_features={"prev_error": "no such table: school"},
        latency_ms=2100,
        output="SELECT COUNT(*) FROM schools WHERE County = 'Alameda';",
        exec_error=None,
        retry_of_step_id=draft_step_id,
    )

    logger.finish_trajectory(
        traj_id,
        final_pred_sql="SELECT COUNT(*) FROM schools WHERE County = 'Alameda';",
        final_success=1,
        success_method="exec_match",
    )

    row = logger.conn.execute(
        """
        SELECT trajectory_id, num_steps, total_cost_usd,
               total_tok_in, total_tok_out, final_success, success_method
        FROM trajectory WHERE trajectory_id = ?
        """,
        (traj_id,),
    ).fetchone()

    shadow_cost = logger.conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0.0) FROM shadow"
    ).fetchone()[0]

    retry_link = logger.conn.execute(
        "SELECT step_id, retry_of_step_id FROM step WHERE retry_of_step_id IS NOT NULL"
    ).fetchone()

    print("trajectory rollup:")
    print(f"  trajectory_id   = {row[0]}")
    print(f"  num_steps       = {row[1]}")
    print(f"  total_cost_usd  = ${row[2]:.6f}")
    print(f"  total_tok_in    = {row[3]}")
    print(f"  total_tok_out   = {row[4]}")
    print(f"  final_success   = {row[5]} ({row[6]})")
    print(f"  shadow_cost_usd = ${shadow_cost:.6f}  (segregated, not in total)")
    print(f"  repair step {retry_link[0]} -> retry_of_step_id={retry_link[1]}")

    logger.close()


if __name__ == "__main__":
    main()
