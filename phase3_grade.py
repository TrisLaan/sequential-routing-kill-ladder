"""Phase 3 — faithful-but-thin grading + task well-formedness verification.

Resolve signal (smoke test): all FAIL_TO_PASS node ids PASS after applying the
held-out test_patch + the agent's source diff onto a clean base_commit checkout.
Full PASS_TO_PASS regression checking is intentionally out of scope this phase
(documented) — it does not affect the horizon read, which is the deliverable.
"""
from __future__ import annotations

from dataclasses import dataclass

import phase3_repo as R


@dataclass
class GradeResult:
    resolved: int            # 1 / 0
    detail: str
    statuses: dict


def _f2p(inst) -> list[str]:
    return [str(x) for x in list(inst["FAIL_TO_PASS"])]


def verify_wellformed(inst) -> tuple[bool, str]:
    """Confirm the task is usable on THIS host with no Docker:
      base+test_patch  -> at least one F2P FAILS (there is a real bug), and
      base+test_patch+gold_patch -> all F2P PASS (our grader detects the fix).
    Leaves the testbed reset to base_commit.
    """
    iid = inst["instance_id"]
    base = inst["base_commit"]
    f2p = _f2p(inst)

    R.git_clean_reset(iid, base)
    r = R.apply_patch(iid, inst["test_patch"], "test_patch")
    if not r.ok:
        return False, f"test_patch did not apply: {r.detail}"
    st, tail = R.run_pytest_nodeids(iid, f2p)
    if not st:
        return False, f"F2P did not collect/run at base (no statuses). tail: {tail[-400:]}"
    if not R.f2p_any_fail(st, f2p):
        return False, f"F2P did not fail at base (not a real bug here?): {st}"

    r = R.apply_patch(iid, inst["patch"], "gold_patch")
    if not r.ok:
        R.git_clean_reset(iid, base)
        return False, f"gold patch did not apply: {r.detail}"
    st2, tail2 = R.run_pytest_nodeids(iid, f2p)
    R.git_clean_reset(iid, base)
    if not R.f2p_all_pass(st2, f2p):
        return False, f"gold patch did NOT make all F2P pass: {st2}"
    return True, "well-formed (base fails, gold passes)"


def grade_submission(inst, agent_diff: str) -> GradeResult:
    """Reset -> apply test_patch -> apply agent diff -> run F2P. resolved iff all pass."""
    iid = inst["instance_id"]
    base = inst["base_commit"]
    f2p = _f2p(inst)

    R.git_clean_reset(iid, base)
    r = R.apply_patch(iid, inst["test_patch"], "test_patch")
    if not r.ok:
        return GradeResult(0, f"test_patch failed at grade time: {r.detail}", {})
    if agent_diff and agent_diff.strip():
        r = R.apply_patch(iid, agent_diff, "agent_diff")
        if not r.ok:
            R.git_clean_reset(iid, base)
            return GradeResult(0, f"agent diff did not apply cleanly: {r.detail}", {})
    st, tail = R.run_pytest_nodeids(iid, f2p)
    resolved = 1 if R.f2p_all_pass(st, f2p) else 0
    R.git_clean_reset(iid, base)
    return GradeResult(resolved, f"f2p_all_pass={bool(resolved)}; tail: {tail[-300:]}", st)
