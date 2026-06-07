"""Phase 3 — Stage 2 orchestrator (code-agent thin smoke test).

Subcommands:
  mock    $0 MockClient dry-run of the full agent->bash->tracer wiring (no network).
  cheap   cheap-only (gpt-4.1-mini) on all chosen tasks. Near-free. (after H1 OK)
  strong  expensive-only (gpt-5.4) on the SAME tasks; per-task $1.50 hard cap. (after H2 OK)
  report  recompute + print deliverables from saved results.

Budget guards (persisted in phase3_spend.json so they survive across the H1/H2 halts):
  * $1.50 per-task hard cap on the strong arm (abort trajectory -> exit_status=cost_capped).
  * $25 cumulative CIRCUIT BREAKER: if crossed mid-run, stop immediately and report.
  * cumulative spend printed after EVERY task.
Single arm at a time => realized cost is clean by construction (segregation invariant).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import config
import phase3_repo as R
from agent.client import OpenAIClient
from phase3_agent import CodeAgent
from phase3_grade import grade_submission
from phase3_tasks import load_manifest
from tracer import TraceLogger

SPEND_FILE = config.ROOT / "phase3_spend.json"
RESULTS = {"cheap": config.ROOT / "phase3_results_cheap.json",
           "strong": config.ROOT / "phase3_results_strong.json"}
CIRCUIT_BREAKER_USD = 35.0   # Stage 4 (full split); $40 hard ceiling stands
PER_TASK_CAP_USD = 1.50
STRONG_MODEL = "gpt-5.4"
CHEAP_MODEL = "gpt-4.1-mini"
STEP_LIMIT = 30
WALL_LIMIT_S = 900        # 15 min/task wall cap (bounds runtime on big repos)
CMD_TIMEOUT_S = 120


class CircuitBreaker(Exception):
    pass


class Spend:
    """Cumulative spend, persisted so the $25 breaker spans invocations."""

    def __init__(self):
        self.total = 0.0
        if SPEND_FILE.exists():
            self.total = json.loads(SPEND_FILE.read_text()).get("total_usd", 0.0)

    def add(self, delta: float):
        self.total += delta
        SPEND_FILE.write_text(json.dumps({"total_usd": self.total}, indent=2))
        if self.total > CIRCUIT_BREAKER_USD:
            raise CircuitBreaker(f"cumulative spend ${self.total:.4f} > ${CIRCUIT_BREAKER_USD}")


def _venv_scripts(iid: str) -> str:
    return str((R.WORK_ROOT / iid / "venv" / "Scripts"))


def _diff_files(patch: str) -> list[str]:
    """Files touched by a unified diff (the `+++ b/<path>` targets)."""
    import re
    if not patch:
        return []
    return sorted(set(re.findall(r'^\+\+\+ b/(.+?)\s*$', patch, re.M)))


def _run_arm(arm: str, model: str, reasoning_effort: str | None,
             per_task_cap: float | None, *, manifest_path=None,
             results_path: Path | None = None, step_limit: int = STEP_LIMIT,
             wall_limit: int = WALL_LIMIT_S):
    from phase3_tasks import MANIFEST
    manifest = load_manifest(manifest_path or MANIFEST)
    tasks = manifest["chosen"]
    results_path = results_path or RESULTS[arm]
    if not tasks:
        print("no chosen tasks in manifest; run phase3_tasks.py select[_light] first")
        return
    spend = Spend()
    db = TraceLogger(str(config.PHASE3_DB_PATH))
    policy = "cheap_only" if arm == "cheap" else "strong_only"
    print(f"=== {arm} arm: model={model} effort={reasoning_effort} cap={per_task_cap} "
          f"step_limit={step_limit} | starting cumulative ${spend.total:.4f} ===")
    results = []
    try:
        for i, t in enumerate(tasks, 1):
            iid = t["instance_id"]
            R.git_clean_reset(iid, t["base_commit"])         # clean start
            agent = CodeAgent(
                client=OpenAIClient(),
                model=model, reasoning_effort=reasoning_effort, tracer=db,
                policy_label=policy, step_limit=step_limit,
                per_task_cost_cap=per_task_cap, cmd_timeout=CMD_TIMEOUT_S,
                wall_limit_s=wall_limit, on_spend=spend.add)
            rr = agent.run(instance_id=iid, repo=t["repo"],
                           problem_statement=t["problem_statement"],
                           cwd=str(R.testbed_dir(iid)), venv_scripts=_venv_scripts(iid))
            agent_diff = R.git_diff(iid)
            g = grade_submission(t, agent_diff)
            # write resolution back onto the trajectory row
            db.conn.execute("UPDATE trajectory SET final_success=?, success_method=? "
                            "WHERE trajectory_id=?", (g.resolved, "f2p_all_pass", rr.trajectory_id))
            db.conn.commit()
            gold_f = _diff_files(t["patch"])
            agent_f = _diff_files(agent_diff)
            res = {"instance_id": iid, "repo": t["repo"], "model": model,
                   "resolved": g.resolved, "exit_status": rr.exit_status,
                   "submitted": rr.exit_status == "submitted",
                   "num_steps": rr.num_steps, "cost_usd": rr.total_cost_usd,
                   "n_fail_steps": sum(1 for s in rr.steps if s.check == "fail"),
                   "n_test_steps": sum(1 for s in rr.steps if s.is_test_run),
                   "gold_files": gold_f, "agent_files": agent_f,
                   "edited_right_files": bool(set(agent_f) & set(gold_f)),
                   "grade_detail": g.detail[:200],
                   "step_checks": [{"i": s.index, "rc": s.returncode, "check": s.check,
                                    "is_test": s.is_test_run, "cost": s.cost_usd}
                                   for s in rr.steps]}
            results.append(res)
            print(f"[{arm} {i}/{len(tasks)}] {iid}: resolved={g.resolved} "
                  f"exit={rr.exit_status} steps={rr.num_steps} "
                  f"task_cost=${rr.total_cost_usd:.4f}  ||  CUMULATIVE=${spend.total:.4f}")
    except CircuitBreaker as e:
        print(f"\n*** CIRCUIT BREAKER TRIPPED: {e} — STOPPING. ***")
        results_path.write_text(json.dumps(results, indent=2))
        db.close()
        sys.exit(2)
    results_path.write_text(json.dumps(results, indent=2))
    db.close()
    _print_arm_summary(arm, results, spend.total)


def _print_arm_summary(arm: str, results: list[dict], cumulative: float):
    n = len(results)
    if not n:
        return
    resolved = sum(r["resolved"] or 0 for r in results)
    steps = [r["num_steps"] for r in results]
    costs = [r["cost_usd"] for r in results]
    import statistics as st
    print(f"\n=== {arm} arm summary (n={n}) ===")
    print(f"resolve: {resolved}/{n}")
    print(f"horizon (steps) per task: {dict((r['instance_id'], r['num_steps']) for r in results)}")
    print(f"horizon distribution: min={min(steps)} med={st.median(steps)} max={max(steps)}")
    print(f"cost/task mean=${st.mean(costs):.4f} "
          f"sd=${(st.stdev(costs) if n>1 else 0):.4f}")
    capped = [r['instance_id'] for r in results if r['exit_status'] == 'cost_capped']
    if capped:
        print(f"CAPPED tasks (hit ${PER_TASK_CAP_USD}/task): {capped}")
    # contamination flag: a resolve in a very short trajectory may be a memorized
    # ~1-step fix (gpt-5.4 may have 2025-03 in its training window).
    sus = [r['instance_id'] for r in results if r['resolved'] and r['num_steps'] <= 4]
    if sus:
        print(f"FLAG suspicious short-trajectory resolves (<=4 steps, read with "
              f"contamination caution): {sus}")
    # GUI/hang flag: wall cap hit with few steps => likely an interactive-plot block,
    # not real horizon.
    hang = [r['instance_id'] for r in results
            if r['exit_status'] == 'wall_limit' and r['num_steps'] < 10]
    if hang:
        print(f"FLAG wall-cap-with-few-steps (possible GUI/plt hang, not real horizon): {hang}")
    print(f"cumulative spend now: ${cumulative:.4f}")
    _classify_failures(arm, results)


def _classify_failures(arm: str, results: list[dict]):
    """Failure-mode breakdown per arm — the cut that answers the open concern:
      (A) EXECUTION/TEST-FAIL = never produced a clean-running SUBMISSION (cost/step/
          wall-limit, loops on failing checks). escalate-on-failure / routing has a
          sequential target here.
      (B) SEMANTIC near-miss  = submitted a complete running patch (and, as evidence,
          targeted the right gold file(s)) but the fix is wrong. Routing helps only if
          the OTHER model gets the semantics right -> a FLAT "use the capable model",
          not richly sequential.
    Falls back to (A) for a submission that touched the WRONG files (not a clean
    right-place near-miss).
    """
    print(f"\n--- {arm} failure-mode breakdown (unresolved tasks) ---")
    A, B = [], []
    for r in results:
        if r["resolved"]:
            continue
        semantic = bool(r.get("submitted")) and bool(r.get("edited_right_files"))
        (B if semantic else A).append(r["instance_id"])
        print(f"  {r['instance_id']}: {'(B) SEMANTIC near-miss' if semantic else '(A) EXECUTION/TEST-FAIL'} "
              f"(exit={r['exit_status']}, submitted={r.get('submitted')}, "
              f"right_files={r.get('edited_right_files')}, fail_steps={r['n_fail_steps']}, "
              f"steps={r['num_steps']})")
    n_trigger = sum(1 for r in results if not r["resolved"] and r["n_fail_steps"] > 0)
    nfail = sum(1 for r in results if not r["resolved"])
    print(f"  => (A) EXECUTION/TEST-FAIL: {len(A)}  |  (B) SEMANTIC near-miss: {len(B)}  "
          f"(of {nfail} unresolved)")
    print(f"  => escalate-on-failure would fire on {n_trigger}/{nfail} unresolved "
          f"(>=1 failing check) — simulatable from step_checks logs.")


# ===================== OUT-OF-FAMILY MODEL-DIVERSITY PROBE =====================
# Separate provider (OpenRouter) => separate budget ledger. We do NOT touch
# phase3_spend.json (the OpenAI $40 ceiling). This stage's ceiling is $15 with a
# $12 soft-stop, and the OpenRouter key itself is capped at $15 as a final backstop.
OOF_SPEND_FILE = config.ROOT / "phase3_oof_spend.json"
# NOTE: these thresholds are in ESTIMATE space (config.PRICES list rates), which
# runs ~1.6x hot vs OpenRouter's real charges (confirmed against the live key
# balance). The TRUE hard backstop is the $15 cap set on the OpenRouter key
# itself (provider-enforced: calls are rejected once real spend hits $15). So we
# set the estimate-space limits high enough not to truncate a full 3-arm run
# prematurely; the real cap, not these numbers, is what actually bounds spend.
OOF_BREAKER_USD = 25.0
OOF_SOFT_STOP_USD = 22.0
OOF_MODELS = [  # (model_id, policy_label)
    ("qwen/qwen3-coder", "qwen3coder"),
    ("deepseek/deepseek-v3.2", "deepseek_v32"),
    ("z-ai/glm-4.6", "glm46"),
]
OOF_STEP_LIMIT = 50
OOF_PER_TASK_CAP = 2.0


class OOFSpend:
    """Cumulative OpenRouter spend, persisted to its OWN file; $15 hard breaker."""

    def __init__(self):
        self.total = 0.0
        if OOF_SPEND_FILE.exists():
            self.total = json.loads(OOF_SPEND_FILE.read_text()).get("total_usd", 0.0)

    def add(self, delta: float):
        self.total += delta
        OOF_SPEND_FILE.write_text(json.dumps({"total_usd": self.total}, indent=2))
        if self.total > OOF_BREAKER_USD:
            raise CircuitBreaker(
                f"OpenRouter cumulative ${self.total:.4f} > ${OOF_BREAKER_USD}")


def _union_tasks():
    """All 35 distinct tasks = Stage2 + Stage3 + Stage4 manifests, deduped, ordered."""
    from phase3_tasks import MANIFEST, MANIFEST_LIGHT, MANIFEST_FULL
    seen, tasks = set(), []
    for p in (MANIFEST, MANIFEST_LIGHT, MANIFEST_FULL):
        for t in load_manifest(p)["chosen"]:
            if t["instance_id"] not in seen:
                seen.add(t["instance_id"])
                tasks.append(t)
    return tasks


def run_oof():
    """Run the 3 out-of-family arms on all 35 tasks via OpenRouter. Spends real $."""
    tasks = _union_tasks()
    spend = OOFSpend()
    db = TraceLogger(str(config.PHASE3_DB_PATH))
    print(f"=== OUT-OF-FAMILY PROBE: {len(tasks)} tasks x {len(OOF_MODELS)} models "
          f"| step_limit={OOF_STEP_LIMIT} per_task_cap=${OOF_PER_TASK_CAP} "
          f"| soft-stop ${OOF_SOFT_STOP_USD} hard ${OOF_BREAKER_USD} "
          f"| starting cumulative ${spend.total:.4f} ===")
    try:
        for model, label in OOF_MODELS:
            # Skip a model already fully logged (idempotent re-entry after a stop).
            done = {r[0] for r in db.conn.execute(
                "SELECT task_id FROM trajectory WHERE policy_label=?", (label,)).fetchall()}
            results = []
            print(f"\n#### arm {label} (model={model}) — {len(done)} already logged ####")
            for i, t in enumerate(tasks, 1):
                iid = t["instance_id"]
                if iid in done:
                    print(f"[{label} {i}/{len(tasks)}] {iid}: already logged, skip")
                    continue
                if spend.total >= OOF_SOFT_STOP_USD:
                    print(f"\n*** SOFT-STOP: cumulative ${spend.total:.4f} >= "
                          f"${OOF_SOFT_STOP_USD}. Stopping before {iid}. ***")
                    raise CircuitBreaker("soft-stop")
                R.git_clean_reset(iid, t["base_commit"])
                agent = CodeAgent(
                    client=OpenAIClient.for_openrouter(),
                    model=model, reasoning_effort=None, tracer=db,
                    policy_label=label, step_limit=OOF_STEP_LIMIT,
                    per_task_cost_cap=OOF_PER_TASK_CAP, cmd_timeout=CMD_TIMEOUT_S,
                    wall_limit_s=WALL_LIMIT_S, on_spend=spend.add)
                rr = agent.run(instance_id=iid, repo=t["repo"],
                               problem_statement=t["problem_statement"],
                               cwd=str(R.testbed_dir(iid)), venv_scripts=_venv_scripts(iid))
                agent_diff = R.git_diff(iid)
                g = grade_submission(t, agent_diff)
                db.conn.execute("UPDATE trajectory SET final_success=?, success_method=? "
                                "WHERE trajectory_id=?",
                                (g.resolved, "f2p_all_pass", rr.trajectory_id))
                db.conn.commit()
                gold_f = _diff_files(t["patch"]); agent_f = _diff_files(agent_diff)
                results.append({"instance_id": iid, "model": model, "resolved": g.resolved,
                                "exit_status": rr.exit_status,
                                "submitted": rr.exit_status == "submitted",
                                "num_steps": rr.num_steps, "cost_usd": rr.total_cost_usd,
                                "edited_right_files": bool(set(agent_f) & set(gold_f)),
                                "grade_detail": g.detail[:200]})
                print(f"[{label} {i}/{len(tasks)}] {iid}: resolved={g.resolved} "
                      f"exit={rr.exit_status} steps={rr.num_steps} "
                      f"task_cost=${rr.total_cost_usd:.4f}  ||  CUMULATIVE=${spend.total:.4f}")
            outp = config.ROOT / f"phase3_results_oof_{label}.json"
            outp.write_text(json.dumps(results, indent=2))
            print(f"  wrote {outp.name} ({len(results)} new rows)")
    except CircuitBreaker as e:
        print(f"\n*** STOPPED: {e} — cumulative ${spend.total:.4f}. "
              f"Partial results saved; re-run 'oof' to resume (idempotent). ***")
        db.close()
        sys.exit(2)
    db.close()
    print(f"\n=== OUT-OF-FAMILY PROBE COMPLETE. cumulative ${spend.total:.4f} ===")


def mock_dryrun():
    """$0: drive the full loop with a scripted MockClient against task #1's testbed."""
    from agent.client import MockClient
    manifest = load_manifest()
    if not manifest["chosen"]:
        print("need a manifest with >=1 chosen task; run phase3_tasks.py select first")
        return
    t = manifest["chosen"][0]
    iid = t["instance_id"]
    R.git_clean_reset(iid, t["base_commit"])
    # scripted assistant turns: a bad-format turn, a real ls, a real python check,
    # then the two-step submission. (text, prompt_tokens, completion_tokens)
    block = "```mswea_bash_command\n{}\n```"
    script = [
        ("THOUGHT: malformed on purpose (no block).", 100, 10),                 # format error
        ("THOUGHT: list files.\n" + block.format("ls | head -5"), 120, 12),     # rc0
        ("THOUGHT: sanity python.\n" + block.format("python -c \"print(1+1)\""), 130, 14),
        ("THOUGHT: make patch.\n" + block.format("git diff > patch.txt"), 90, 8),
        # cd-PREFIXED submit — the exact pattern the old gate missed; must now submit.
        ("THOUGHT: submit.\n" + block.format(
            "cd . && echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat patch.txt"), 80, 9),
    ]
    db = TraceLogger(":memory:")     # mock => never touch the real phase3 DB
    agent = CodeAgent(client=MockClient(script), model=CHEAP_MODEL,
                      reasoning_effort=None, tracer=db, policy_label="cheap_only",
                      step_limit=10, per_task_cost_cap=None)
    rr = agent.run(instance_id=iid, repo=t["repo"],
                   problem_statement=t["problem_statement"],
                   cwd=str(R.testbed_dir(iid)), venv_scripts=_venv_scripts(iid))
    g = grade_submission(t, R.git_diff(iid))
    # introspect the tracer
    rows = db.conn.execute(
        "SELECT step_index, decision_type, action_model, cost_usd, exec_error, state_features "
        "FROM step WHERE trajectory_id=? ORDER BY step_index", (rr.trajectory_id,)).fetchall()
    traj = db.conn.execute(
        "SELECT num_steps, total_cost_usd, final_success, success_method FROM trajectory "
        "WHERE trajectory_id=?", (rr.trajectory_id,)).fetchone()
    print("=== MOCK DRY-RUN ($0, no network) ===")
    print(f"exit_status={rr.exit_status} num_steps={rr.num_steps} "
          f"submitted_patch_len={len(rr.submission)} total_cost=${rr.total_cost_usd:.6f}")
    print(f"trajectory row: num_steps={traj[0]} total_cost_usd={traj[1]} "
          f"final_success={traj[2]} method={traj[3]}")
    print("steps logged to tracer:")
    for r in rows:
        print(f"  idx={r[0]} type={r[1]} model={r[2]} cost={r[3]} "
              f"exec_error={r[4]} state={r[5]}")
    print(f"grade_submission resolved={g.resolved} ({g.detail[:120]})")
    # $0 = no API calls (MockClient). The tracer still COMPUTES a cost from the
    # scripted token counts via config.cost_usd — that's the pricing-wiring check,
    # not real spend. So we assert it computed a positive cost, and that REAL spend
    # is zero by construction (no network).
    assert traj[1] > 0.0, "expected nonzero tracer-computed cost from scripted tokens"
    assert rr.exit_status == "submitted", f"expected submit, got {rr.exit_status}"
    print(f"\nWIRING OK: real $ spent = $0 (MockClient, no API calls); "
          f"tracer-computed cost on scripted tokens = ${traj[1]:.6f}; "
          f"steps logged with per-step pass/fail in state_features; submit detected; grading ran.")
    db.close()


if __name__ == "__main__":
    from phase3_tasks import MANIFEST_LIGHT, MANIFEST_FULL
    LIGHT_RESULTS = {"cheap": config.ROOT / "phase3_results_cheap_light.json",
                     "strong": config.ROOT / "phase3_results_strong_light.json"}
    FULL_RESULTS = {"cheap": config.ROOT / "phase3_results_cheap_full.json",
                    "strong": config.ROOT / "phase3_results_strong_full.json"}
    LIGHT_STEP_LIMIT, LIGHT_STRONG_CAP = 50, 6.0
    cmd = sys.argv[1] if len(sys.argv) > 1 else "mock"
    if cmd == "mock":
        mock_dryrun()
    elif cmd == "oof":
        run_oof()
    elif cmd == "cheap":
        _run_arm("cheap", CHEAP_MODEL, None, None)
    elif cmd == "strong":
        _run_arm("strong", STRONG_MODEL, "medium", PER_TASK_CAP_USD)
    elif cmd == "cheap_light":
        _run_arm("cheap", CHEAP_MODEL, None, None, manifest_path=MANIFEST_LIGHT,
                 results_path=LIGHT_RESULTS["cheap"], step_limit=LIGHT_STEP_LIMIT)
    elif cmd == "strong_light":
        _run_arm("strong", STRONG_MODEL, "medium", LIGHT_STRONG_CAP,
                 manifest_path=MANIFEST_LIGHT, results_path=LIGHT_RESULTS["strong"],
                 step_limit=LIGHT_STEP_LIMIT)
    elif cmd == "cheap_full":
        _run_arm("cheap", CHEAP_MODEL, None, None, manifest_path=MANIFEST_FULL,
                 results_path=FULL_RESULTS["cheap"], step_limit=LIGHT_STEP_LIMIT)
    elif cmd == "strong_full":
        # cap lowered 6.0 -> 2.5 to cover all 22 tasks within the $35 breaker; strong
        # RESOLVES occur early/cheap (<$0.7 in Stage 3), so this preserves the spread
        # signal and only cuts long non-resolving thrash.
        _run_arm("strong", STRONG_MODEL, "medium", 2.5,
                 manifest_path=MANIFEST_FULL, results_path=FULL_RESULTS["strong"],
                 step_limit=LIGHT_STEP_LIMIT)
    elif cmd == "report":
        cum = (json.loads(SPEND_FILE.read_text()).get("total_usd", 0.0)
               if SPEND_FILE.exists() else 0.0)
        for label, table in (("stage2", RESULTS), ("stage3-light", LIGHT_RESULTS)):
            for arm in ("cheap", "strong"):
                if table[arm].exists():
                    print(f"\n######## {label} :: {arm} ########")
                    _print_arm_summary(arm, json.loads(table[arm].read_text()), cum)
    else:
        print(f"unknown command {cmd!r}")
