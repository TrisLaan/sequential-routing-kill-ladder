"""$0 validation of the baseline_strong trace wiring against the MockClient.

No network, no API key. Builds a throwaway SQLite DB and a synthetic Task, then
drives the real run_baseline loop with a scripted MockClient whose sequence is:

    schema_retrieval -> draft_sql (FAILS: bad column) -> repair (SUCCEEDS)

and asserts the trace rows: three steps in order, the draft<-repair chain
(retry_of_step_id), the exec_error on the draft only, the cost/token rollups,
final_success, and that NO shadow rows were written (baseline has no shadow).

Run from sql-cost-opt/ :
    python validate_baseline.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import config
from agent.baseline import run_baseline
from agent.client import MockClient
from tracer import TraceLogger


@dataclass(frozen=True)
class _Task:
    task_id: str
    db_id: str
    question: str
    gold_sql: str
    evidence: str | None
    db_path: str
    benchmark: str
    difficulty: str | None = None


def _build_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE schools (id INTEGER PRIMARY KEY, county TEXT);
        INSERT INTO schools (id, county) VALUES (1,'Alameda'),(2,'Alameda'),(3,'Marin');
        """
    )
    conn.commit()
    conn.close()


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "mockdb.sqlite"
        _build_db(db_path)

        gold = "SELECT COUNT(*) FROM schools WHERE county = 'Alameda'"
        task = _Task(
            task_id="mock-001",
            db_id="mockdb",
            question="How many schools are in Alameda county?",
            gold_sql=gold,
            evidence=None,
            db_path=str(db_path),
            benchmark="bird",
        )

        # (text, prompt_tokens, completion_tokens) per call, in loop order.
        script = [
            ("Table: schools\nColumns: id, county", 900, 20),          # schema_retrieval
            ("SELECT COUNT(*) FROM schools WHERE district = 'Alameda'", 950, 18),  # draft: bad column
            (gold, 1000, 16),                                          # repair: clean + matches gold
        ]
        client = MockClient(script)

        logger = TraceLogger(":memory:")
        traj_id, success = run_baseline(task, client, logger)
        conn = logger.conn

        # --- assertions ---------------------------------------------------
        if len(client.calls) != 3:
            _fail(f"expected 3 LLM calls, got {len(client.calls)}")
        for c in client.calls:
            if c["model"] != "gpt-5.4":
                _fail(f"expected model gpt-5.4, got {c['model']!r}")
            if c["reasoning_effort"] != "medium":
                _fail(f"expected reasoning_effort 'medium', got {c['reasoning_effort']!r}")

        steps = conn.execute(
            "SELECT step_id, step_index, decision_type, action_model, action_effort, "
            "prompt_tokens, completion_tokens, cost_usd, exec_error, retry_of_step_id "
            "FROM step ORDER BY step_index"
        ).fetchall()
        if len(steps) != 3:
            _fail(f"expected 3 step rows, got {len(steps)}")

        kinds = [s[2] for s in steps]
        if kinds != ["schema_retrieval", "draft_sql", "repair"]:
            _fail(f"step order wrong: {kinds}")

        if any(s[3] != "gpt-5.4" or s[4] != "medium" for s in steps):
            _fail("every step must log action_model=gpt-5.4, action_effort=medium")

        sr, draft, repair = steps
        if sr[8] is not None:
            _fail("schema_retrieval should have no exec_error")
        if draft[8] is None:
            _fail("draft_sql should have recorded an exec_error (bad column)")
        if repair[8] is not None:
            _fail(f"repair should have NO exec_error, got {repair[8]!r}")

        # draft<-repair chain
        if repair[9] != draft[0]:
            _fail(f"repair.retry_of_step_id ({repair[9]}) should equal draft step_id ({draft[0]})")
        if draft[9] is not None:
            _fail("draft_sql should not be a retry of anything")

        # cost rollup = sum of step costs, and matches config pricing
        step_cost_sum = sum(s[7] for s in steps)
        expected_cost = sum(
            config.cost_usd("gpt-5.4", s[5], s[6]) for s in steps
        )
        if abs(step_cost_sum - expected_cost) > 1e-12:
            _fail(f"per-step cost mismatch vs config pricing: {step_cost_sum} vs {expected_cost}")

        traj = conn.execute(
            "SELECT num_steps, total_cost_usd, total_tok_in, total_tok_out, "
            "final_success, success_method, final_pred_sql FROM trajectory "
            "WHERE trajectory_id = ?",
            (traj_id,),
        ).fetchone()
        num_steps, total_cost, tok_in, tok_out, final_success, method, final_sql = traj

        if num_steps != 3:
            _fail(f"trajectory.num_steps should be 3, got {num_steps}")
        if abs(total_cost - step_cost_sum) > 1e-12:
            _fail(f"trajectory rollup cost {total_cost} != sum of steps {step_cost_sum}")
        if tok_in != sum(s[5] for s in steps) or tok_out != sum(s[6] for s in steps):
            _fail(f"trajectory token rollup wrong: in={tok_in} out={tok_out}")
        if total_cost <= 0:
            _fail("total_cost_usd should be > 0")
        if not success or final_success != 1 or method != "exec_match":
            _fail(f"expected success: success={success} final_success={final_success} method={method!r}")
        if final_sql.strip() != gold:
            _fail(f"final_pred_sql should be the clean repair SQL, got {final_sql!r}")

        # baseline has NO shadow rows
        n_shadow = conn.execute("SELECT COUNT(*) FROM shadow").fetchone()[0]
        if n_shadow != 0:
            _fail(f"baseline_strong must write 0 shadow rows, got {n_shadow}")

        logger.close()

    print("PASS: baseline_strong trace wiring validated against MockClient ($0)")
    print(f"  steps         : {kinds}")
    print(f"  draft step_id : {draft[0]}  <- repair.retry_of_step_id={repair[9]}")
    print(f"  rollup cost   : ${total_cost:.6f}  (tok_in={tok_in}, tok_out={tok_out})")
    print(f"  final_success : {final_success} ({method})")
    print(f"  shadow rows   : {n_shadow}")


if __name__ == "__main__":
    main()
