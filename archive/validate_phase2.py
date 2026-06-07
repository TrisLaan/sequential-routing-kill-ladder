"""$0 mock validation of the WHOLE Phase 2 pipeline. No network, no API key.

Builds a throwaway DB + synthetic task and drives all three arms with scripted
MockClients (one cheap exec error in arm B so the repair chain + retry_of_step_id
are covered), then asserts: arm discriminator, the four shadow label columns per
decision type, repair-chain link, cost-scoping invariants (baseline_strong total
excludes shadow rows AND arms B/C), the canonical all_local_green predicate, and
that analyze_phase2 runs clean.

Run from sql-cost-opt/ :  python validate_phase2.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import analyze_phase2
import config
from agent.arms import run_arm
from agent.baseline import run_baseline
from agent.client import MockClient
from agent.shadow import shadow_baseline
from data.adapter import Task
from tracer import TraceLogger

GOLD = "SELECT COUNT(*) FROM schools WHERE county = 'Alameda'"


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
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "mockdb.sqlite"
        _build_db(db_path)
        traces = str(Path(tmp) / "traces.sqlite")

        task = Task(
            task_id="7",
            db_id="mockdb",
            question="How many schools are in Alameda county?",
            gold_sql=GOLD,
            evidence=None,
            db_path=str(db_path),
            benchmark="bird",
            difficulty="simple",
        )

        logger = TraceLogger(traces)
        conn = logger.conn

        # --- Arm A: baseline (strong), draft succeeds (= gold) ------------
        base_client = MockClient([
            ("Table: schools\nColumns: id, county", 900, 20),  # schema_retrieval
            (GOLD, 950, 18),                                    # draft_sql (clean, matches gold)
        ])
        base_traj, base_ok = run_baseline(task, base_client, logger)
        if not base_ok:
            _fail("baseline should succeed on the mock task")

        # --- Arm A: cheap shadow over the baseline trajectory -------------
        shadow_client = MockClient([
            ("Table: schools\nColumns: id, county", 880, 22),  # schema shadow (superset)
            (GOLD, 870, 15),                                    # draft shadow (= gold/strong)
        ])
        n_shadow = shadow_baseline(task, base_traj, shadow_client, logger)
        if n_shadow != 2:
            _fail(f"expected 2 shadow rows (schema+draft), got {n_shadow}")

        # --- Arm B: cheap_all; draft errors -> repair clean-but-wrong -----
        b_client = MockClient([
            ("Table: schools\nColumns: id, county", 800, 20),               # schema
            ("SELECT COUNT(*) FROM schools WHERE district = 'Alameda'", 810, 18),  # draft: bad column
            ("SELECT COUNT(*) FROM schools WHERE county = 'Marin'", 820, 16),      # repair: clean, wrong
        ])
        ca_traj, ca_ok = run_arm(task, b_client, logger, arm="cheap_all")
        if ca_ok:
            _fail("cheap_all was scripted to end on a wrong (but clean) query -> should MISS")

        # --- Arm C: cheap_no_schema; strong schema + cheap draft (= gold) -
        c_client = MockClient([
            ("Table: schools\nColumns: id, county", 900, 20),  # schema (strong)
            (GOLD, 700, 14),                                    # draft (cheap, = gold)
        ])
        cns_traj, cns_ok = run_arm(task, c_client, logger, arm="cheap_no_schema")
        if not cns_ok:
            _fail("cheap_no_schema scripted to match gold -> should succeed")

        # ================= ASSERTIONS =================
        # arm discriminator
        arms = dict(conn.execute("SELECT trajectory_id, arm FROM trajectory").fetchall())
        if arms[base_traj] is not None:
            _fail(f"baseline arm should be NULL, got {arms[base_traj]!r}")
        if arms[ca_traj] != "cheap_all" or arms[cns_traj] != "cheap_no_schema":
            _fail(f"arm labels wrong: {arms}")

        # shadow label columns per decision type
        sh = conn.execute(
            "SELECT s.decision_type, sh.matched, sh.match_method, sh.cheap_matches_gold, "
            "sh.strong_matches_gold, sh.schema_superset_of_gold, sh.schema_superset_of_strong_draft "
            "FROM shadow sh JOIN step s ON sh.step_id=s.step_id "
            "WHERE s.trajectory_id=? ORDER BY s.step_index",
            (base_traj,),
        ).fetchall()
        dt = {r[0]: r for r in sh}
        srow = dt["schema_retrieval"]
        if srow[2] != "schema_superset_proxy":
            _fail(f"schema shadow match_method wrong: {srow[2]}")
        if srow[1] != 1 or srow[6] != 1 or srow[5] != 1:
            _fail(f"schema shadow superset labels wrong: matched={srow[1]} of_gold={srow[5]} of_strong={srow[6]}")
        if srow[3] is not None or srow[4] is not None:
            _fail("schema shadow must leave cheap/strong_matches_gold NULL")
        drow = dt["draft_sql"]
        if drow[2] != "set_equality":
            _fail(f"draft shadow match_method wrong: {drow[2]}")
        if drow[1] != 1 or drow[3] != 1 or drow[4] != 1:
            _fail(f"draft shadow labels wrong: matched={drow[1]} cmg={drow[3]} smg={drow[4]}")
        if drow[5] is not None or drow[6] is not None:
            _fail("draft shadow must leave schema superset cols NULL")

        # repair-chain link in arm B
        rep = conn.execute(
            "SELECT step_id, retry_of_step_id, decision_type FROM step "
            "WHERE trajectory_id=? AND decision_type='repair'", (ca_traj,)
        ).fetchall()
        if len(rep) != 1 or rep[0][1] is None:
            _fail(f"arm B should have exactly one repair step with retry_of_step_id, got {rep}")
        draft_id = conn.execute(
            "SELECT step_id FROM step WHERE trajectory_id=? AND decision_type='draft_sql'", (ca_traj,)
        ).fetchone()[0]
        if rep[0][1] != draft_id:
            _fail(f"repair.retry_of_step_id ({rep[0][1]}) should link to draft ({draft_id})")

        # final_success non-null on all completed trajectories
        nulls = conn.execute("SELECT COUNT(*) FROM trajectory WHERE final_success IS NULL").fetchone()[0]
        if nulls:
            _fail(f"{nulls} completed trajectories have NULL final_success")

        # cost-scoping invariant
        if not analyze_phase2.invariant_check(conn):
            _fail("invariant_check failed: baseline total not isolated from shadow/arms")
        base_total = conn.execute(
            "SELECT total_cost_usd FROM trajectory WHERE trajectory_id=?", (base_traj,)
        ).fetchone()[0]
        base_steps = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM step WHERE trajectory_id=?", (base_traj,)
        ).fetchone()[0]
        shadow_on_base = conn.execute(
            "SELECT COALESCE(SUM(sh.cost_usd),0) FROM shadow sh JOIN step s ON sh.step_id=s.step_id "
            "WHERE s.trajectory_id=?", (base_traj,)
        ).fetchone()[0]
        if abs(base_total - base_steps) > 1e-12:
            _fail(f"baseline total {base_total} != its step sum {base_steps}")
        if shadow_on_base <= 0:
            _fail("expected nonzero shadow cost attached to baseline steps")
        if abs(base_total - (base_steps + shadow_on_base)) < 1e-12:
            _fail("baseline total must EXCLUDE shadow cost, but it appears included")

        # canonical predicate
        green = analyze_phase2.all_local_green_task_ids(conn)
        if task.task_id not in green:
            _fail(f"task {task.task_id} should be all_local_green (all shadows matched=1)")

        logger.close()

        # analyze runs clean end-to-end on the mock DB
        print("\n----- analyze_phase2 on mock DB (smoke) -----")
        analyze_phase2.analyze(traces)

    print("\nPASS: Phase 2 pipeline validated against MockClient ($0)")
    print(f"  arms: baseline(NULL)+shadow, cheap_all(MISS, 1 repair link), cheap_no_schema(OK)")
    print(f"  shadow rows on baseline: {n_shadow} with per-decision label columns")
    print(f"  composition failure present (base OK + green + cheap_all MISS); cheap_no_schema recovers")


if __name__ == "__main__":
    main()
