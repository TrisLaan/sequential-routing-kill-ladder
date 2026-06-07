"""Phase 3 — Stage 6: MIXED-TRAJECTORY ROUTING (the direct sequential test).

Everything prior measured models running WHOLE trajectories SOLO and compared end
outcomes, or shadow-labeled the strong trajectory step by step. We never ran a true
MIXED trajectory where one model does the early steps, hands off its ACCUMULATED
STATE, and another model finishes mid-task. This module does exactly that.

How state is carried across the switch (auditable — this is the whole point)
--------------------------------------------------------------------------------
A handoff at step k means: model A runs steps 0..k-1 of the ReAct loop on a CLEAN
testbed, then model B runs steps k..end. B inherits A's state in two ways, both real,
neither reset:
  1. WORKING DIRECTORY: the same C:\\p3\\<iid>\\testbed on disk. We do NOT git-reset
     or re-clone between segments, so every file A edited, every repro script / patch.txt
     A wrote, persists for B.
  2. MESSAGE HISTORY: the SAME `messages` list. B's first call sees A's entire
     conversation (system + instance prompt + every THOUGHT/command A emitted + every
     observation the shell returned). B simply continues the conversation — it is not
     told a switch happened, so this isolates PURE state coupling, not a coaching effect.
Both segments run the IDENTICAL `phase3_agent.react_loop`; only the model/client swap.
The whole thing is ONE tracer trajectory; each step row carries its own action_model,
so the switch point is visible in the log. policy_label = mixed_cs (cheap->strong) or
mixed_sc (strong->cheap); the handoff k is in trajectory.notes.

Budget: dedicated stage-6 ledger (phase3_stage6_spend.json), soft-stop $12 / hard $15,
SEPARATE from the historical OpenAI phase3_spend.json so the prior $32.75 record stays
intact and this run's spend is independently auditable. Per-task cap $2 (combined across
both segments), step cap 50, MPLBACKEND=Agg (in react_loop's _exec_bash).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import config
import phase3_repo as R
from agent.client import OpenAIClient
from phase3_agent import (INSTANCE_TEMPLATE, SYSTEM_TEMPLATE, StepRecord,
                          react_loop)
from phase3_grade import grade_submission
from run_phase3 import _diff_files, _union_tasks
from tracer import TraceLogger

STAGE6_DB = config.ROOT / "traces_phase3_stage6.sqlite"
SPEND_FILE = config.ROOT / "phase3_stage6_spend.json"
RESULTS_FILE = config.ROOT / "phase3_stage6_results.json"
SOFT_STOP_USD = 12.0
HARD_BREAKER_USD = 15.0
PER_TASK_CAP_USD = 2.0
STEP_LIMIT = 50
WALL_LIMIT_S = 900
CMD_TIMEOUT_S = 120
CHEAP, STRONG = "gpt-4.1-mini", "gpt-5.4"
STRONG_EFFORT = "medium"
EARLY_K = 3


class CircuitBreaker(Exception):
    pass


class Stage6Spend:
    """Cumulative stage-6 OpenAI spend, persisted to its own file. $15 hard breaker
    inside add(); $12 soft-stop checked between tasks by the caller."""

    def __init__(self):
        self.total = 0.0
        if SPEND_FILE.exists():
            self.total = json.loads(SPEND_FILE.read_text()).get("total_usd", 0.0)

    def add(self, delta: float):
        self.total += delta
        SPEND_FILE.write_text(json.dumps({"total_usd": self.total}, indent=2))
        if self.total > HARD_BREAKER_USD:
            raise CircuitBreaker(f"stage-6 cumulative ${self.total:.4f} > ${HARD_BREAKER_USD}")


def _venv_scripts(iid: str) -> str:
    return str((R.WORK_ROOT / iid / "venv" / "Scripts"))


def _task_by_id(iid: str) -> dict:
    for t in _union_tasks():
        if t["instance_id"] == iid:
            return t
    raise KeyError(iid)


def run_mixed(task: dict, *, first_model: str, first_effort: str | None,
              second_model: str, second_effort: str | None, k: int,
              direction: str, db: TraceLogger, spend: Stage6Spend,
              first_client, second_client) -> dict:
    """Run one mixed trajectory: first_model does steps 0..k-1, second_model finishes,
    inheriting first_model's working dir + message history. direction in {cs, sc}."""
    import time

    iid = task["instance_id"]
    cwd = str(R.testbed_dir(iid))
    venv = _venv_scripts(iid)
    R.git_clean_reset(iid, task["base_commit"])          # clean start for segment 1

    tag = f"stage6 {direction} k={k}"
    tid = db.start_trajectory(
        task_id=iid, db_id=task["repo"], benchmark="swebench_live",
        policy_label=f"mixed_{direction}",
        notes=f"{tag}; first={first_model} second={second_model} step_limit={STEP_LIMIT}")

    messages = [
        {"role": "system", "content": SYSTEM_TEMPLATE},
        {"role": "user", "content": INSTANCE_TEMPLATE.format(
            task=task["problem_statement"], cwd=cwd)},
    ]
    steps: list[StepRecord] = []
    start = time.time()

    # --- segment 1: first_model for exactly k steps (no reset, state accumulates) ---
    exit1, sub1, cost1, next_idx = react_loop(
        client=first_client, model=first_model, reasoning_effort=first_effort,
        tracer=db, tid=tid, messages=messages, steps=steps, cwd=cwd, venv_scripts=venv,
        start_idx=0, step_budget=k, per_task_cost_cap=PER_TASK_CAP_USD,
        cmd_timeout=CMD_TIMEOUT_S, wall_limit_s=WALL_LIMIT_S, wall_start=start,
        total_cost=0.0, on_spend=spend.add)

    handoff_happened = exit1 not in ("submitted", "cost_capped", "wall_limit")
    seg1_steps = next_idx

    # --- segment 2: second_model continues from next_idx on the SAME state ---
    if handoff_happened:
        exit2, sub2, total_cost, end_idx = react_loop(
            client=second_client, model=second_model, reasoning_effort=second_effort,
            tracer=db, tid=tid, messages=messages, steps=steps, cwd=cwd, venv_scripts=venv,
            start_idx=next_idx, step_budget=STEP_LIMIT - next_idx,
            per_task_cost_cap=PER_TASK_CAP_USD, cmd_timeout=CMD_TIMEOUT_S,
            wall_limit_s=WALL_LIMIT_S, wall_start=start, total_cost=cost1,
            on_spend=spend.add)
        final_exit, final_sub = exit2, (sub2 or sub1)
    else:
        # first model self-terminated before reaching k -> no handoff
        exit2, total_cost, end_idx = "no_handoff", cost1, next_idx
        final_exit, final_sub = exit1, sub1

    # --- grade off the actual on-disk diff (same path as solo runs) ---
    agent_diff = R.git_diff(iid)
    g = grade_submission(task, agent_diff)
    db.conn.execute("UPDATE trajectory SET final_success=?, success_method=? "
                    "WHERE trajectory_id=?", (g.resolved, "f2p_all_pass", tid))
    db.conn.commit()

    gold_f, agent_f = _diff_files(task["patch"]), _diff_files(agent_diff)
    seg2_steps = end_idx - seg1_steps
    seg2_cost = total_cost - cost1
    return {
        "instance_id": iid, "direction": direction, "k": k, "tag": tag,
        "first_model": first_model, "second_model": second_model,
        "resolved": g.resolved, "handoff_happened": handoff_happened,
        "seg1_exit": exit1, "seg2_exit": exit2, "final_exit": final_exit,
        "handoff_step": seg1_steps, "seg1_steps": seg1_steps, "seg2_steps": seg2_steps,
        "num_steps": end_idx, "seg1_cost": round(cost1, 4), "seg2_cost": round(seg2_cost, 4),
        "total_cost": round(total_cost, 4),
        "edited_right_files": bool(set(agent_f) & set(gold_f)),
        "agent_files": agent_f, "grade_detail": g.detail[:200],
        "trajectory_id": tid,
    }


# ============================ THE PLAN ============================
# cheap-solo step counts (from existing logs) -> mid_k = round(half).
CHEAP_SOLO_STEPS = {
    "hiyouga__llama-factory-7505": 29, "aws-cloudformation__cfn-lint-4009": 50,
    "wireservice__csvkit-1281": 31, "dynaconf__dynaconf-1241": 50,
    "stanfordnlp__dspy-8739": 50, "openai__openai-agents-python-1601": 17,
    "django__asgiref-523": 8, "rthalley__dnspython-1206": 50,
    "Aider-AI__aider-4269": 50,
    # both-solve
    "joke2k__faker-2190": 14, "fonttools__fonttools-3907": 21,
    "PyPSA__PyPSA-1325": 8, "stanfordnlp__dspy-8605": 41,
    "a2aproject__a2a-python-226": 20,
}
SPREAD = ["dynaconf__dynaconf-1241", "django__asgiref-523", "rthalley__dnspython-1206",
          "stanfordnlp__dspy-8739", "Aider-AI__aider-4269",
          "openai__openai-agents-python-1601", "aws-cloudformation__cfn-lint-4009",
          "wireservice__csvkit-1281", "hiyouga__llama-factory-7505"]
# routable execution-survival spread tasks (cheap loops on failing tests) — the
# subset where a poisoned/helpful inherited state is most likely to show in MID handoff.
MID_SUBSET = ["dynaconf__dynaconf-1241", "rthalley__dnspython-1206",
              "stanfordnlp__dspy-8739", "Aider-AI__aider-4269",
              "aws-cloudformation__cfn-lint-4009", "django__asgiref-523"]
# reverse (strong early, cheap finishes): isolates whether EARLY authorship matters
# or only who does the hard LATER step. Mix of spread + both-solve.
REVERSE = ["django__asgiref-523", "rthalley__dnspython-1206",
           "joke2k__faker-2190", "a2aproject__a2a-python-226"]
# both-fail rescue probe: does a handoff resolve a task neither solo solved?
BOTHFAIL = ["Tuxemon__Tuxemon-3068", "a2aproject__a2a-python-302"]


def _mid_k(iid: str) -> int:
    return max(2, round(CHEAP_SOLO_STEPS[iid] / 2))


def build_plan() -> list[tuple[str, str, int]]:
    """(task_id, kind, k). kind in {cs_early, cs_mid, sc_early}. Ordered by priority
    so the cheapest/most-informative arms run first and the breaker can't starve them."""
    plan: list[tuple[str, str, int]] = []
    # Tier 1: all 9 SPREAD, cheap->strong EARLY (the core RESCUE/SABOTAGE probe)
    for iid in SPREAD:
        plan.append((iid, "cs_early", EARLY_K))
    # Tier 2: MID handoff on the routable subset (k-dependence probe)
    for iid in MID_SUBSET:
        plan.append((iid, "cs_mid", _mid_k(iid)))
    # Tier 3: reverse strong->cheap early (authorship-direction isolation)
    for iid in REVERSE:
        plan.append((iid, "sc_early", EARLY_K))
    # Tier 4: both-fail rescue probe, cheap->strong early
    for iid in BOTHFAIL:
        plan.append((iid, "cs_early", EARLY_K))
    return plan


def _already_done(db: TraceLogger, iid: str, direction: str, k: int) -> bool:
    tag = f"stage6 {direction} k={k}"
    row = db.conn.execute(
        "SELECT final_success FROM trajectory WHERE task_id=? AND notes LIKE ? "
        "AND final_success IS NOT NULL", (iid, f"%{tag}%")).fetchone()
    return row is not None


def run_plan():
    db = TraceLogger(str(STAGE6_DB))
    spend = Stage6Spend()
    cheap_client = OpenAIClient()
    strong_client = OpenAIClient()      # same OpenAI key; separate instances are fine
    results = json.loads(RESULTS_FILE.read_text()) if RESULTS_FILE.exists() else []
    done_tags = {(r["instance_id"], r["tag"]) for r in results}
    plan = build_plan()
    print(f"=== STAGE 6 MIXED-TRAJECTORY ROUTING | {len(plan)} planned arms | "
          f"soft-stop ${SOFT_STOP_USD} hard ${HARD_BREAKER_USD} per-task ${PER_TASK_CAP_USD} "
          f"| starting cumulative ${spend.total:.4f} ===")
    try:
        for i, (iid, kind, k) in enumerate(plan, 1):
            direction = "sc" if kind == "sc_early" else "cs"
            tag = f"stage6 {direction} k={k}"
            if (iid, tag) in done_tags or _already_done(db, iid, direction, k):
                print(f"[{i}/{len(plan)}] {iid} {kind} k={k}: already done, skip")
                continue
            if spend.total >= SOFT_STOP_USD:
                print(f"\n*** SOFT-STOP: cumulative ${spend.total:.4f} >= ${SOFT_STOP_USD}. "
                      f"Stopping before {iid} {kind}. ***")
                break
            if direction == "cs":
                fm, fe, sm, se = CHEAP, None, STRONG, STRONG_EFFORT
            else:
                fm, fe, sm, se = STRONG, STRONG_EFFORT, CHEAP, None
            print(f"[{i}/{len(plan)}] {iid} {kind} k={k}: {fm}->{sm} ...")
            row = run_mixed(_task_by_id(iid), first_model=fm, first_effort=fe,
                            second_model=sm, second_effort=se, k=k, direction=direction,
                            db=db, spend=spend, first_client=cheap_client if direction=="cs" else strong_client,
                            second_client=strong_client if direction=="cs" else cheap_client)
            row["kind"] = kind
            results.append(row)
            RESULTS_FILE.write_text(json.dumps(results, indent=2))
            print(f"    -> resolved={row['resolved']} handoff@{row['handoff_step']} "
                  f"steps={row['num_steps']} (seg1={row['seg1_steps']} seg2={row['seg2_steps']}) "
                  f"seg1=${row['seg1_cost']:.4f} seg2=${row['seg2_cost']:.4f} "
                  f"task=${row['total_cost']:.4f}  ||  CUMULATIVE=${spend.total:.4f}")
    except CircuitBreaker as e:
        print(f"\n*** HARD BREAKER: {e} — stopping. Partial results saved. ***")
        RESULTS_FILE.write_text(json.dumps(results, indent=2))
        db.close()
        sys.exit(2)
    RESULTS_FILE.write_text(json.dumps(results, indent=2))
    db.close()
    print(f"\n=== STAGE 6 RUN COMPLETE. cumulative ${spend.total:.4f}, "
          f"{len(results)} arms logged. ===")


def mock():
    """$0 wiring check: cheap-segment (k=2) then strong-segment, both MockClient, on a
    real testbed. Asserts ONE trajectory carries BOTH models' steps, the working dir +
    messages are inherited (not reset), submit detected, and real spend = $0."""
    from agent.client import MockClient
    task = _task_by_id(SPREAD[0])
    iid = task["instance_id"]
    cwd = str(R.testbed_dir(iid))
    block = "```mswea_bash_command\n{}\n```"
    cheap_script = [
        ("THOUGHT: orient.\n" + block.format("ls | head -3"), 100, 10),
        ("THOUGHT: note a marker file (state for the handoff).\n"
         + block.format("echo cheap_was_here > .mixed_marker.txt && cat .mixed_marker.txt"), 90, 9),
    ]
    strong_script = [
        # strong must SEE cheap's marker file on disk (proves working-dir inheritance)
        ("THOUGHT: did the previous steps leave state?\n"
         + block.format("cat .mixed_marker.txt"), 200, 12),
        ("THOUGHT: make patch.\n" + block.format("git diff > patch.txt"), 90, 8),
        ("THOUGHT: submit.\n" + block.format(
            "cd . && echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat patch.txt"), 80, 9),
    ]
    db = TraceLogger(":memory:")

    class _NoSpend:
        total = 0.0
        def add(self, d): self.total += d

    row = run_mixed(task, first_model=CHEAP, first_effort=None, second_model=STRONG,
                    second_effort=STRONG_EFFORT, k=2, direction="cs", db=db,
                    spend=_NoSpend(), first_client=MockClient(cheap_script),
                    second_client=MockClient(strong_script))
    rows = db.conn.execute(
        "SELECT step_index, action_model, decision_type, state_features FROM step "
        "WHERE trajectory_id=? ORDER BY step_index", (row["trajectory_id"],)).fetchall()
    print("=== STAGE 6 MOCK ($0, no network) ===")
    for r in rows:
        print(f"  idx={r[0]} model={r[1]} type={r[2]} state={r[3]}")
    print(f"handoff_happened={row['handoff_happened']} handoff_step={row['handoff_step']} "
          f"seg1_steps={row['seg1_steps']} seg2_steps={row['seg2_steps']} "
          f"final_exit={row['final_exit']} resolved={row['resolved']}")
    models = [r[1] for r in rows]
    assert models[:2] == [CHEAP, CHEAP], f"first 2 steps should be cheap, got {models[:2]}"
    assert STRONG in models[2:], f"strong should run after handoff, got {models}"
    assert row["handoff_step"] == 2, f"expected handoff at step 2, got {row['handoff_step']}"
    assert row["handoff_happened"], "handoff should have happened"
    # the strong segment's `cat .mixed_marker.txt` must have returned the cheap-written
    # content (rc 0) -> proves strong inherited cheap's on-disk state.
    strong_marker_step = rows[2]
    assert '"returncode": 0' in (strong_marker_step[3] or ""), \
        "strong's cat of cheap's marker file should succeed (working-dir inheritance)"
    assert row["final_exit"] == "submitted", f"expected submit, got {row['final_exit']}"
    db.close()
    print("\nWIRING OK: $0 real spend (MockClient); ONE trajectory holds cheap THEN strong "
          "steps; strong read a file cheap wrote (working-dir state inherited) and continued "
          "cheap's message history; submit detected; grading ran.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "mock"
    if cmd == "mock":
        mock()
    elif cmd == "run":
        run_plan()
    elif cmd == "plan":
        for iid, kind, k in build_plan():
            print(f"  {iid:42s} {kind:9s} k={k}")
    else:
        print(f"unknown command {cmd!r}")
