"""Phase 2 deliverables #1-#7 over traces.sqlite. Read-only.

Run from sql-cost-opt/ :  python analyze_phase2.py

Defines ONE canonical predicate, all_local_green(task), reused by #3 and #4:
  (1) coverage: every LLM step (schema_retrieval/draft_sql/repair) in the
      baseline trajectory has exactly one shadow row;
  (2) all green: every such shadow row has matched=1 (NOT NULL).
A task FAILS if any label is 0, NULL, or missing (no vacuous pass).
"""
from __future__ import annotations

import math
import sys

import config


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% interval for a binomial proportion k/n."""
    if n == 0:
        return (0.0, 0.0)
    phat = k / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    half = (z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def _arm_map(conn, policy: str, arm: str | None) -> dict[str, dict]:
    """task_id -> {traj_id, final_success, total_cost} for one (policy, arm)."""
    if arm is None:
        where, params = "policy_label=? AND arm IS NULL", (policy,)
    else:
        where, params = "policy_label=? AND arm=?", (policy, arm)
    rows = conn.execute(
        f"SELECT task_id, trajectory_id, final_success, total_cost_usd "
        f"FROM trajectory WHERE {where}",
        params,
    ).fetchall()
    return {
        r[0]: {"traj_id": r[1], "final_success": r[2], "total_cost": r[3] or 0.0}
        for r in rows
    }


def all_local_green_task_ids(conn) -> set[str]:
    """task_ids whose baseline trajectory passes the canonical predicate."""
    rows = conn.execute(
        """
        WITH base AS (
            SELECT trajectory_id, task_id FROM trajectory
            WHERE policy_label='baseline_strong' AND arm IS NULL
        ),
        per_step AS (
            SELECT s.trajectory_id, s.step_id,
                   COUNT(sh.shadow_id) AS n_sh,
                   SUM(CASE WHEN sh.matched=1 THEN 1 ELSE 0 END) AS n_green
            FROM step s
            JOIN base b ON s.trajectory_id = b.trajectory_id
            LEFT JOIN shadow sh ON sh.step_id = s.step_id
            WHERE s.decision_type IN ('schema_retrieval','draft_sql','repair')
            GROUP BY s.step_id
        )
        SELECT b.task_id
        FROM base b
        WHERE EXISTS (SELECT 1 FROM per_step p WHERE p.trajectory_id=b.trajectory_id)
          AND NOT EXISTS (
              SELECT 1 FROM per_step p
              WHERE p.trajectory_id = b.trajectory_id
                AND (p.n_sh <> 1 OR p.n_green <> 1)   -- missing/dup OR not-green/null
          )
        """
    ).fetchall()
    return {r[0] for r in rows}


def invariant_check(conn) -> bool:
    """Production baseline number is provably unaffected by shadow rows / arms B,C."""
    ok = True
    # (a) baseline_strong trajectory totals == sum of their OWN step costs.
    row = conn.execute(
        """
        SELECT
          (SELECT COALESCE(SUM(total_cost_usd),0) FROM trajectory
             WHERE policy_label='baseline_strong' AND arm IS NULL),
          (SELECT COALESCE(SUM(s.cost_usd),0) FROM step s
             JOIN trajectory t ON s.trajectory_id=t.trajectory_id
             WHERE t.policy_label='baseline_strong' AND t.arm IS NULL)
        """
    ).fetchone()
    traj_total, step_total = row
    if abs(traj_total - step_total) > 1e-9:
        print(f"  INVARIANT FAIL: baseline traj total {traj_total} != step sum {step_total}")
        ok = False
    # (b) shadow cost is real but excluded from every trajectory total.
    shadow_total = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM shadow").fetchone()[0]
    # (c) cross-policy leakage check: baseline total must NOT include arm B/C or shadow.
    arms_total = conn.execute(
        "SELECT COALESCE(SUM(total_cost_usd),0) FROM trajectory WHERE policy_label='shadow'"
    ).fetchone()[0]
    print(f"  baseline_strong total   = ${traj_total:.6f}  (== own step sum ${step_total:.6f})")
    print(f"  shadow-table cost        = ${shadow_total:.6f}  (excluded from all traj totals)")
    print(f"  arms B+C trajectory cost = ${arms_total:.6f}  (separate policy='shadow')")
    print(f"  INVARIANT {'PASS' if ok else 'FAIL'}: baseline number isolated from shadow + arms")
    return ok


def analyze(db_path: str | None = None) -> None:
    import sqlite3

    path = db_path or str(config.PHASE2_DB_PATH)
    conn = sqlite3.connect(path)

    base = _arm_map(conn, "baseline_strong", None)
    ca = _arm_map(conn, "shadow", "cheap_all")
    cns = _arm_map(conn, "shadow", "cheap_no_schema")
    common = sorted(set(base) & set(ca) & set(cns), key=lambda x: int(x) if x.isdigit() else x)
    n = len(common)

    print("=" * 78)
    print("PHASE 2 REPORT")
    print("=" * 78)
    print(f"tasks with all 3 arms present: {n}  "
          f"(baseline={len(base)}, cheap_all={len(ca)}, cheap_no_schema={len(cns)})")

    print("\n--- INVARIANT: production baseline isolation ---")
    invariant_check(conn)

    green = all_local_green_task_ids(conn) & set(common)

    # ---- #1 myopic upper-bound saving %, by decision type (net) -----------
    print("\n--- #1 Myopic upper-bound saving %  [UPPER BOUND on strong-induced states] ---")
    denom = conn.execute(
        "SELECT COALESCE(SUM(total_cost_usd),0) FROM trajectory "
        "WHERE policy_label='baseline_strong' AND arm IS NULL"
    ).fetchone()[0]
    rows = conn.execute(
        """
        SELECT s.decision_type,
               COALESCE(SUM(s.cost_usd - sh.cost_usd),0) AS movable_net,
               COUNT(*) AS n_matched
        FROM step s
        JOIN trajectory t ON s.trajectory_id=t.trajectory_id
        JOIN shadow sh ON sh.step_id=s.step_id
        WHERE t.policy_label='baseline_strong' AND t.arm IS NULL AND sh.matched=1
        GROUP BY s.decision_type
        """
    ).fetchall()
    movable_by_type = {r[0]: (r[1], r[2]) for r in rows}
    total_movable = sum(v[0] for v in movable_by_type.values())
    for dt in ("schema_retrieval", "draft_sql", "repair"):
        net, k = movable_by_type.get(dt, (0.0, 0))
        pct = 100.0 * net / denom if denom else 0.0
        tag = "  <- PROXY label, most inflated" if dt == "schema_retrieval" else ""
        print(f"  {dt:16s}: {pct:5.1f}%  (net ${net:.5f}, matched steps={k}){tag}")
    print(f"  {'TOTAL':16s}: {100.0*total_movable/denom if denom else 0:5.1f}%  "
          f"(net ${total_movable:.5f} of baseline ${denom:.5f})")

    # ---- #2 realized exec-match + policy-scoped cost per arm --------------
    print("\n--- #2 Realized exec-match (vs gold) and policy-scoped cost per arm ---")
    for label, m in (("baseline_strong", base), ("cheap_all", ca), ("cheap_no_schema", cns)):
        sub = [m[t] for t in common]
        succ = sum(x["final_success"] or 0 for x in sub)
        cost = sum(x["total_cost"] for x in sub)
        lo, hi = wilson_ci(succ, n)
        print(f"  {label:16s}: exec-match {succ}/{n} = {100.0*succ/n if n else 0:5.1f}% "
              f"[{100*lo:.1f}, {100*hi:.1f}]   cost ${cost:.4f}")

    # ---- #3 cascade gap ---------------------------------------------------
    predicted_succ = sum(1 for t in common if (base[t]["final_success"] == 1 and t in green))
    realized_succ = sum(ca[t]["final_success"] or 0 for t in common)
    pred_rate = predicted_succ / n if n else 0.0
    real_rate = realized_succ / n if n else 0.0
    print("\n--- #3 Cascade gap (myopic-predicted cheap_all − realized cheap_all) ---")
    print(f"  myopic-predicted cheap_all success = {predicted_succ}/{n} = {100*pred_rate:.1f}%")
    print(f"  realized cheap_all exec-match       = {realized_succ}/{n} = {100*real_rate:.1f}%")
    print(f"  CASCADE GAP = {100*(pred_rate-real_rate):+.1f} pts")

    # ---- #4 HEADLINE: composition-attributed failures --------------------
    eligible = [t for t in common if base[t]["final_success"] == 1 and t in green]
    comp_fail = [t for t in eligible if ca[t]["final_success"] == 0]
    m = len(comp_fail)
    raw_lo, raw_hi = wilson_ci(m, n)
    cond_lo, cond_hi = wilson_ci(m, len(eligible))
    print("\n--- #4 HEADLINE: composition-attributed failures (sub-subset) ---")
    print("  def: baseline final_success=1 AND all_local_green AND cheap_all final_success=0")
    print(f"  raw rate over all tasks      = {m}/{n} = {100.0*m/n if n else 0:.2f}% "
          f"[{100*raw_lo:.2f}, {100*raw_hi:.2f}]  (Wilson 95%)")
    print(f"  conditional (among green+strong-correct) = {m}/{len(eligible)} = "
          f"{100.0*m/len(eligible) if eligible else 0:.1f}% [{100*cond_lo:.1f}, {100*cond_hi:.1f}]")
    print("  NOTE: sub-subset — denominator is itself conditioned on strong success + all-green.")

    # ---- #5 schema marginal cascade contribution -------------------------
    ca_succ = sum(ca[t]["final_success"] or 0 for t in common)
    cns_succ = sum(cns[t]["final_success"] or 0 for t in common)
    ca_cost = sum(ca[t]["total_cost"] for t in common)
    cns_cost = sum(cns[t]["total_cost"] for t in common)
    base_cost = sum(base[t]["total_cost"] for t in common)
    print("\n--- #5 Schema marginal cascade contribution ---")
    print(f"  cheap_all exec-match       = {100.0*ca_succ/n if n else 0:.1f}%  "
          f"(cost ${ca_cost:.4f}, saves ${base_cost-ca_cost:.4f} vs baseline)")
    print(f"  cheap_no_schema exec-match = {100.0*cns_succ/n if n else 0:.1f}%  "
          f"(cost ${cns_cost:.4f}, saves ${base_cost-cns_cost:.4f} vs baseline)")
    print(f"  SCHEMA MARGINAL = (cheap_all − cheap_no_schema) = "
          f"{100.0*(ca_succ-cns_succ)/n if n else 0:+.1f} pts")
    # cross-tab #4 failures vs cheap_no_schema recovery (schema proxy passed for all by def)
    recovered = sum(1 for t in comp_fail if cns[t]["final_success"] == 1)
    print(f"  cross-tab of the {m} composition failures (all had schema proxy GREEN by def):")
    print(f"    recovered by cheap_no_schema (strong schema) = {recovered}  -> schema-proxy artifact / schema mattered")
    print(f"    NOT recovered                                = {m-recovered}  -> real downstream draft/repair coupling")

    # ---- #6 schema_retrieval per-step match-rate + variance --------------
    print("\n--- #6 schema_retrieval shadow match-rate + variance across tasks/DBs ---")
    sr_rows = conn.execute(
        """
        SELECT t.db_id, sh.matched
        FROM shadow sh
        JOIN step s ON sh.step_id=s.step_id
        JOIN trajectory t ON s.trajectory_id=t.trajectory_id
        WHERE t.policy_label='baseline_strong' AND t.arm IS NULL
          AND s.decision_type='schema_retrieval'
        """
    ).fetchall()
    if sr_rows:
        overall = sum(r[1] or 0 for r in sr_rows) / len(sr_rows)
        per_db: dict[str, list[int]] = {}
        for db_id, mt in sr_rows:
            per_db.setdefault(db_id, []).append(mt or 0)
        db_means = [sum(v) / len(v) for v in per_db.values()]
        var = (sum((x - overall) ** 2 for x in db_means) / len(db_means)) if db_means else 0.0
        print(f"  overall match-rate = {100*overall:.1f}%  (n={len(sr_rows)} schema shadows)")
        print(f"  across {len(per_db)} DBs: mean-of-db-means {100*(sum(db_means)/len(db_means)):.1f}%, "
              f"variance {var:.4f}, range [{100*min(db_means):.0f}, {100*max(db_means):.0f}]%")
    else:
        print("  (no schema_retrieval shadow rows)")

    # ---- #7 repair-chain coverage from live arms B+C ---------------------
    print("\n--- #7 Repair-chain coverage (first real-data draft<-repair links) ---")
    rep_rows = conn.execute(
        """
        SELECT t.arm, COUNT(*)
        FROM step s JOIN trajectory t ON s.trajectory_id=t.trajectory_id
        WHERE t.policy_label='shadow' AND s.decision_type='repair'
          AND s.retry_of_step_id IS NOT NULL
        GROUP BY t.arm
        """
    ).fetchall()
    total_rep = sum(r[1] for r in rep_rows)
    for arm, cnt in rep_rows:
        print(f"  arm={arm}: {cnt} repair steps with retry_of_step_id")
    print(f"  TOTAL repair-chain links from arms B+C = {total_rep}")

    conn.close()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    analyze()
