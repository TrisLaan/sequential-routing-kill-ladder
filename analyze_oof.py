"""Phase 3 Stage-1A deliverables: out-of-family model-diversity probe analysis.

Reads traces_phase3.sqlite. Builds the per-task x per-model resolve matrix over the
35-task union, then reports the two thesis-deciding numbers:
  (1) nesting-break count + union ceiling vs gpt-5.4 alone
  (2) cheapest-successful-model distribution (does the winner vary per task?)
plus secondary per-model resolve rate / cost-per-resolved / contamination flags.

$0 — pure offline read. Compares OOF arms against the EXISTING gpt-5.4 / gpt-4.1-mini
logs (policy_label strong_only / cheap_only); does NOT re-run them.
"""
from __future__ import annotations
import sqlite3
import config

# display name -> policy_label in the DB
MODELS = [
    ("gpt-4.1-mini", "cheap_only"),
    ("gpt-5.4", "strong_only"),
    ("qwen3-coder", "qwen3coder"),
    ("deepseek-v3.2", "deepseek_v32"),
    ("glm-4.6", "glm46"),
]
PRICE_MODEL_ID = {  # display name -> config.PRICES key (for cost/resolved sanity)
    "gpt-4.1-mini": "gpt-4.1-mini", "gpt-5.4": "gpt-5.4",
    "qwen3-coder": "qwen/qwen3-coder", "deepseek-v3.2": "deepseek/deepseek-v3.2",
    "glm-4.6": "z-ai/glm-4.6",
}


def load():
    con = sqlite3.connect(config.PHASE3_DB_PATH)
    # task universe = the 35 tasks the original arms ran (cheap_only set)
    tasks = [r[0] for r in con.execute(
        "SELECT DISTINCT task_id FROM trajectory WHERE policy_label='cheap_only' ORDER BY task_id")]
    data = {}  # (task, label) -> (resolved, cost, steps)
    for _, label in MODELS:
        for tid, succ, cost, steps in con.execute(
            "SELECT task_id, final_success, total_cost_usd, num_steps FROM trajectory "
            "WHERE policy_label=?", (label,)):
            data[(tid, label)] = (succ, cost or 0.0, steps)
    con.close()
    return tasks, data


def main():
    tasks, data = load()
    labels = [l for _, l in MODELS]
    names = [n for n, _ in MODELS]

    # coverage check
    missing = {n: [t for t in tasks if (t, l) not in data] for n, l in MODELS}
    print(f"N tasks = {len(tasks)}")
    for n in names:
        if missing[n]:
            print(f"  WARNING: {n} missing {len(missing[n])} tasks: {missing[n][:5]}...")

    def resolved(t, l):
        return (data.get((t, l), (0, 0, 0))[0] or 0) == 1

    # ---- resolve matrix ----
    print("\n=== RESOLVE MATRIX (1=resolved) ===")
    hdr = "task".ljust(40) + "".join(n[:13].rjust(14) for n in names)
    print(hdr)
    for t in tasks:
        row = t.ljust(40) + "".join(("1" if resolved(t, l) else ".").rjust(14) for l in labels)
        print(row)

    # ---- per-model resolve rate + cost/resolved ----
    print("\n=== per-model resolve rate / cost / cost-per-resolved ===")
    for n, l in MODELS:
        res = [t for t in tasks if resolved(t, l)]
        tot = sum(data.get((t, l), (0, 0, 0))[1] for t in tasks)
        cpr = tot / len(res) if res else float('nan')
        print(f"  {n:14} resolve {len(res):2}/{len(tasks)}  totcost ${tot:6.3f}  cost/resolved ${cpr:.3f}")

    strong = "strong_only"
    oof_labels = ["qwen3coder", "deepseek_v32", "glm46"]
    oof_names = {"qwen3coder": "qwen3-coder", "deepseek_v32": "deepseek-v3.2", "glm46": "glm-4.6"}

    # ---- DELIVERABLE 1: nesting break ----
    print("\n=== DELIVERABLE 1 — NESTING BREAK (does the ceiling move?) ===")
    strong_res = {t for t in tasks if resolved(t, strong)}
    print(f"gpt-5.4 resolves: {len(strong_res)}/{len(tasks)}")
    break_tasks = []
    for t in tasks:
        winners = [oof_names[l] for l in oof_labels if resolved(t, l)]
        if winners and t not in strong_res:
            break_tasks.append((t, winners))
    print(f"NESTING-BREAK tasks (out-of-family resolves, gpt-5.4 does NOT): {len(break_tasks)}")
    for t, w in break_tasks:
        print(f"   {t}  <- resolved by {w}")
    union = set(strong_res)
    for l in oof_labels:
        union |= {t for t in tasks if resolved(t, l)}
    print(f"UNION ceiling {{gpt-5.4 + all OOF}} = {len(union)}/{len(tasks)} "
          f"({100*len(union)/len(tasks):.0f}%)  vs gpt-5.4 alone {len(strong_res)}/{len(tasks)} "
          f"({100*len(strong_res)/len(tasks):.0f}%)  -> +{len(union)-len(strong_res)} tasks")
    # union over ALL 5 models (incl cheap) for completeness
    union_all = set()
    for l in labels:
        union_all |= {t for t in tasks if resolved(t, l)}
    print(f"UNION over all 5 models = {len(union_all)}/{len(tasks)}")

    # ---- DELIVERABLE 2: cheapest-winner-per-task ----
    print("\n=== DELIVERABLE 2 — CHEAPEST SUCCESSFUL MODEL per task (routing signal) ===")
    from collections import Counter
    cheapest = {}
    for t in tasks:
        opts = [(data[(t, l)][1], n) for n, l in MODELS if resolved(t, l)]
        if opts:
            cheapest[t] = min(opts)  # (cost, name)
    dist = Counter(name for _, name in cheapest.values())
    print(f"tasks resolved by >=1 model: {len(cheapest)}/{len(tasks)}")
    print("distribution of CHEAPEST successful model:")
    for name, c in dist.most_common():
        print(f"   {name:14} wins {c:2} tasks")
    # among tasks resolvable by >1 model, does the cheapest vary?
    multi = {t: [n for n, l in MODELS if resolved(t, l)] for t in tasks}
    multi = {t: v for t, v in multi.items() if len(v) >= 2}
    print(f"\ntasks resolvable by >=2 models: {len(multi)} (these are the contested/routable ones)")
    distinct_cheapest = {cheapest[t][1] for t in multi}
    print(f"distinct cheapest-winners among contested tasks: {sorted(distinct_cheapest)}")
    print("-> winner VARIES per task" if len(distinct_cheapest) > 1 else "-> ONE model always cheapest (flat rule)")

    # ---- secondary: contamination (suspiciously short resolves) ----
    print("\n=== contamination flag: resolves in <=4 steps (post-cutoff => should be clean) ===")
    flagged = []
    for n, l in MODELS:
        for t in tasks:
            d = data.get((t, l))
            if d and (d[0] or 0) == 1 and d[2] is not None and d[2] <= 4:
                flagged.append((t, n, d[2]))
    print(flagged if flagged else "  none (no suspiciously short resolves)")


if __name__ == "__main__":
    main()
