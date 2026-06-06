"""Phase 3 — PRE-REGISTERED mechanical task selection (guards vs cherry-picking).

Pool   : SWE-bench-Live/SWE-bench-Live, split `lite` (curated, lighter, well-formed).
Filter : created_at >= 2025-01-01 (post-cutoff for the cheap model; max date in
         lite is 2025-03 — see contamination caveat in the report).
Order  : created_at DESCENDING (most-recent first => most post-cutoff / least
         contaminated), tie-break instance_id ASC. Fixed and stated up front.
Accept : walk the order; a candidate is CHOSEN iff ALL hold, checked in order:
           1. repo fetches (single commit) on this host,
           2. venv + `pip install -e .` succeed within a 300s budget
              (this IS the objective "pure-Python / installable here" filter —
               heavy ML repos exceed it and are skipped),
           3. the held-out test_patch applies and FAIL_TO_PASS COLLECTS & runs,
           4. well-formed: >=1 F2P FAILS at base, and the gold patch makes all
              F2P PASS (so our grader provably detects a real fix).
         First 5 passing are FROZEN. Every skip is logged with its reason.
         No task is swapped after any agent result is seen.

Run: python phase3_tasks.py select
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

import config
import phase3_repo as R
from phase3_grade import verify_wellformed

MANIFEST = config.ROOT / "phase3_manifest.json"
TARGET = 5
MAX_ATTEMPTS = 30
MIN_DATE = "2025-01-01"

# ---- Stage 3: lighter / representative set -------------------------------------
MANIFEST_LIGHT = config.ROOT / "phase3_manifest_light.json"
TARGET_LIGHT = 8
MAX_ATTEMPTS_LIGHT = 45
# install footprint filter: skip a repo whose declared deps name any heavy marker
# (GPU/DL toolchains) — keeps the set "modest footprint", stated up front.
HEAVY_MARKERS = ("torch", "tensorflow", "jax", "jaxlib", "cupy", "vllm",
                 "deepspeed", "nvidia-", "onnxruntime-gpu", "xformers",
                 "bitsandbytes", "flash-attn", "accelerate")
_DEP_FILES = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
              "requirements-dev.txt", "requirements/test.txt")


def _has_simple_nodeids(f2p) -> bool:
    """True iff every F2P node-id is simple (no space, no '[' parametrization)."""
    return all((" " not in x) and ("[" not in x) for x in f2p)


def _heavy_deps(instance_id: str) -> str | None:
    """Return the first heavy marker found in the repo's declared deps, else None."""
    tb = R.testbed_dir(instance_id)
    for rel in _DEP_FILES:
        p = tb / rel
        if p.exists():
            try:
                txt = p.read_text(encoding="utf-8", errors="replace").lower()
            except Exception:
                continue
            for m in HEAVY_MARKERS:
                if m in txt:
                    return f"{m} in {rel}"
    return None


def _load_ordered(split: str = "lite") -> pd.DataFrame:
    from datasets import load_dataset
    df = load_dataset("SWE-bench-Live/SWE-bench-Live", split=split).to_pandas()
    df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
    df = df[df["created_at"] >= MIN_DATE].copy()
    df = df.sort_values(["created_at", "instance_id"],
                        ascending=[False, True]).reset_index(drop=True)
    return df


def _row_to_inst(row) -> dict:
    return {
        "instance_id": row["instance_id"],
        "repo": row["repo"],
        "base_commit": row["base_commit"],
        "created_at": str(row["created_at"])[:19],
        "problem_statement": row["problem_statement"],
        "patch": row["patch"],
        "test_patch": row["test_patch"],
        "FAIL_TO_PASS": [str(x) for x in list(row["FAIL_TO_PASS"])],
        "PASS_TO_PASS": [str(x) for x in list(row["PASS_TO_PASS"])],
        "test_cmds": [str(x) for x in list(row["test_cmds"])],
    }


def _setup_one(inst: dict) -> tuple[bool, str]:
    iid = inst["instance_id"]
    r = R.fetch_checkout(iid, inst["repo"], inst["base_commit"])
    if not r.ok:
        return False, f"fetch: {r.detail}"
    r = R.make_venv(iid)
    if not r.ok:
        return False, f"venv: {r.detail}"
    r = R.pip_install(iid)
    if not r.ok:
        return False, f"install: {r.detail}"
    ok, detail = verify_wellformed(inst)
    return ok, detail


def _write_manifest(chosen, skips, attempts) -> dict:
    manifest = {
        "pool": "SWE-bench-Live/SWE-bench-Live:lite",
        "order": "created_at DESC, instance_id ASC",
        "min_date": MIN_DATE,
        "target": TARGET,
        "attempts": attempts,
        "chosen": chosen,
        "skipped": skips,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def select() -> dict:
    df = _load_ordered()
    print(f"[select] {len(df)} candidates with created_at >= {MIN_DATE} "
          f"(date range {str(df['created_at'].min())[:10]} .. {str(df['created_at'].max())[:10]})")
    # Resume: reload any prior checkpoint and skip already-processed instances.
    chosen: list[dict] = []
    skips: list[dict] = []
    done_ids: set[str] = set()
    if MANIFEST.exists():
        prev = json.loads(MANIFEST.read_text(encoding="utf-8"))
        chosen = prev.get("chosen", [])
        skips = prev.get("skipped", [])
        done_ids = {t["instance_id"] for t in chosen} | {s["instance_id"] for s in skips}
        print(f"[select] resuming: {len(chosen)} chosen, {len(skips)} skipped already")
    attempts = len(done_ids)
    for _, row in df.iterrows():
        if len(chosen) >= TARGET or attempts >= MAX_ATTEMPTS:
            break
        inst = _row_to_inst(row)
        iid = inst["instance_id"]
        if iid in done_ids:
            continue
        attempts += 1
        print(f"\n[try {attempts}] {iid} ({inst['repo']}, {inst['created_at']}, "
              f"F2P={len(inst['FAIL_TO_PASS'])}) ...", flush=True)
        try:
            ok, detail = _setup_one(inst)
        except Exception as e:  # noqa: BLE001 — any setup blowup => skip, keep walking
            ok, detail = False, f"exception: {type(e).__name__}: {e}"
        if ok:
            chosen.append(inst)
            print(f"   CHOSEN ({len(chosen)}/{TARGET}): {detail}", flush=True)
        else:
            skips.append({"instance_id": iid, "repo": inst["repo"],
                          "created_at": inst["created_at"], "reason": detail})
            print(f"   skip: {detail[:200]}", flush=True)
        _write_manifest(chosen, skips, attempts)   # checkpoint after EVERY candidate

    manifest = _write_manifest(chosen, skips, attempts)
    print(f"\n[select] chose {len(chosen)} tasks, {len(skips)} skipped, "
          f"manifest -> {MANIFEST}")
    return manifest


def _setup_one_light(inst: dict) -> tuple[bool, str]:
    """Like _setup_one but with the modest-footprint (heavy-dep) gate after fetch."""
    iid = inst["instance_id"]
    r = R.fetch_checkout(iid, inst["repo"], inst["base_commit"])
    if not r.ok:
        return False, f"fetch: {r.detail}"
    heavy = _heavy_deps(iid)
    if heavy:
        return False, f"heavy footprint ({heavy})"
    r = R.make_venv(iid)
    if not r.ok:
        return False, f"venv: {r.detail}"
    r = R.pip_install(iid)
    if not r.ok:
        return False, f"install: {r.detail}"
    return verify_wellformed(inst)


def select_light() -> dict:
    """Stage 3: fresh, lighter, representative set (anti-cherry-pick, pre-registered).

    Pre-filters (cheap, from the dataset, before any install):
      - created_at >= 2025-01-01; EXCLUDE every instance touched in Stage 2 (chosen+skipped);
      - FAIL_TO_PASS count in 1..2 (contained bug, not a sprawling feature);
      - all F2P node-ids simple (no space / no '[' parametrization) so targeting is reliable.
    Then walk created_at DESC and accept iff: not heavy-footprint (no GPU/DL deps declared),
    installs <=300s, F2P collects cleanly at base, and well-formed (base fails, gold passes).
    First TARGET_LIGHT frozen; resumable; every skip logged with reason.
    """
    df = _load_ordered()
    touched: set[str] = set()
    if MANIFEST.exists():
        prev = json.loads(MANIFEST.read_text(encoding="utf-8"))
        touched = ({t["instance_id"] for t in prev.get("chosen", [])}
                   | {s["instance_id"] for s in prev.get("skipped", [])})
    # cheap dataset pre-filters
    def _ok_pre(row) -> bool:
        f2p = [str(x) for x in list(row["FAIL_TO_PASS"])]
        return (row["instance_id"] not in touched
                and 1 <= len(f2p) <= 2
                and _has_simple_nodeids(f2p))
    df = df[df.apply(_ok_pre, axis=1)].reset_index(drop=True)
    print(f"[select_light] {len(df)} candidates after pre-filters "
          f"(date {str(df['created_at'].min())[:10]}..{str(df['created_at'].max())[:10]})")

    chosen, skips, done = [], [], set()
    if MANIFEST_LIGHT.exists():
        prevl = json.loads(MANIFEST_LIGHT.read_text(encoding="utf-8"))
        chosen = prevl.get("chosen", [])
        skips = prevl.get("skipped", [])
        done = {t["instance_id"] for t in chosen} | {s["instance_id"] for s in skips}
        print(f"[select_light] resuming: {len(chosen)} chosen, {len(skips)} skipped")
    attempts = len(done)

    def _write():
        MANIFEST_LIGHT.write_text(json.dumps(
            {"pool": "SWE-bench-Live:lite", "order": "created_at DESC",
             "min_date": MIN_DATE, "target": TARGET_LIGHT, "attempts": attempts,
             "prefilters": "fresh(not in stage2); 1<=F2P<=2; simple node-ids",
             "footprint_rule": f"skip if deps name any of {HEAVY_MARKERS}",
             "chosen": chosen, "skipped": skips}, indent=2), encoding="utf-8")

    for _, row in df.iterrows():
        if len(chosen) >= TARGET_LIGHT or attempts >= MAX_ATTEMPTS_LIGHT:
            break
        inst = _row_to_inst(row)
        iid = inst["instance_id"]
        if iid in done:
            continue
        attempts += 1
        print(f"\n[try {attempts}] {iid} ({inst['repo']}, {inst['created_at']}, "
              f"F2P={inst['FAIL_TO_PASS']}) ...", flush=True)
        try:
            ok, detail = _setup_one_light(inst)
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"exception: {type(e).__name__}: {e}"
        if ok:
            chosen.append(inst)
            print(f"   CHOSEN ({len(chosen)}/{TARGET_LIGHT}): {detail}", flush=True)
        else:
            skips.append({"instance_id": iid, "repo": inst["repo"],
                          "created_at": inst["created_at"], "reason": detail})
            print(f"   skip: {detail[:200]}", flush=True)
        _write()
    _write()
    print(f"\n[select_light] chose {len(chosen)}, skipped {len(skips)} -> {MANIFEST_LIGHT}")
    return {"chosen": chosen, "skipped": skips}


# ---- Stage 4: larger-N on the FULL split (post-cutoff to 2025-09) --------------
MANIFEST_FULL = config.ROOT / "phase3_manifest_full.json"
TARGET_FULL = 22
MAX_ATTEMPTS_FULL = 150


def select_full() -> dict:
    """Stage 4: same EXACT Stage-3 mechanical filter, on the FULL split, excluding
    every Stage-2 AND Stage-3 instance. created_at DESC (most recent first => 2025-09
    down => safely post-cutoff for BOTH models, removing the gpt-5.4 contamination
    caveat). Skipped candidates' workdirs are deleted to bound disk over the long walk.
    """
    df = _load_ordered("full")
    touched: set[str] = set()
    for mpath in (MANIFEST, MANIFEST_LIGHT):
        if mpath.exists():
            prev = json.loads(mpath.read_text(encoding="utf-8"))
            touched |= ({t["instance_id"] for t in prev.get("chosen", [])}
                        | {s["instance_id"] for s in prev.get("skipped", [])})

    def _ok_pre(row) -> bool:
        f2p = [str(x) for x in list(row["FAIL_TO_PASS"])]
        return (row["instance_id"] not in touched
                and 1 <= len(f2p) <= 2 and _has_simple_nodeids(f2p))
    df = df[df.apply(_ok_pre, axis=1)].reset_index(drop=True)
    print(f"[select_full] {len(df)} fresh candidates after pre-filters "
          f"(dates {str(df['created_at'].min())[:10]}..{str(df['created_at'].max())[:10]})")

    chosen, skips, done = [], [], set()
    if MANIFEST_FULL.exists():
        prevf = json.loads(MANIFEST_FULL.read_text(encoding="utf-8"))
        chosen, skips = prevf.get("chosen", []), prevf.get("skipped", [])
        done = {t["instance_id"] for t in chosen} | {s["instance_id"] for s in skips}
        print(f"[select_full] resuming: {len(chosen)} chosen, {len(skips)} skipped")
    attempts = len(done)

    def _write():
        MANIFEST_FULL.write_text(json.dumps(
            {"pool": "SWE-bench-Live:full", "order": "created_at DESC",
             "min_date": MIN_DATE, "target": TARGET_FULL, "attempts": attempts,
             "prefilters": "fresh(not in stage2/3); 1<=F2P<=2; simple node-ids",
             "footprint_rule": f"skip if deps name any of {HEAVY_MARKERS}",
             "chosen": chosen, "skipped": skips}, indent=2), encoding="utf-8")

    for _, row in df.iterrows():
        if len(chosen) >= TARGET_FULL or attempts >= MAX_ATTEMPTS_FULL:
            break
        inst = _row_to_inst(row)
        iid = inst["instance_id"]
        if iid in done:
            continue
        attempts += 1
        print(f"\n[try {attempts}] {iid} ({inst['repo']}, {inst['created_at']}, "
              f"F2P={inst['FAIL_TO_PASS']}) ...", flush=True)
        try:
            ok, detail = _setup_one_light(inst)
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"exception: {type(e).__name__}: {e}"
        if ok:
            chosen.append(inst)
            print(f"   CHOSEN ({len(chosen)}/{TARGET_FULL}): {detail}", flush=True)
        else:
            skips.append({"instance_id": iid, "repo": inst["repo"],
                          "created_at": inst["created_at"], "reason": detail})
            R.cleanup(iid)   # reclaim disk: we won't use a skipped candidate
            print(f"   skip: {detail[:180]}", flush=True)
        _write()
    _write()
    print(f"\n[select_full] chose {len(chosen)}, skipped {len(skips)} -> {MANIFEST_FULL}")
    return {"chosen": chosen, "skipped": skips}


def load_manifest(path: Path = MANIFEST) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "select"
    if cmd in ("select", "select_light", "select_full"):
        m = {"select": select, "select_light": select_light,
             "select_full": select_full}[cmd]()
        print("\n=== CHOSEN TASKS ===")
        for i, t in enumerate(m["chosen"], 1):
            print(f"{i}. {t['instance_id']}  {t['repo']}  @{t['base_commit'][:10]}  "
                  f"{t['created_at']}  F2P={t['FAIL_TO_PASS']}")
    else:
        print(f"unknown command {cmd!r}")
