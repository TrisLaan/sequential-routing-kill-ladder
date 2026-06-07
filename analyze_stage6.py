"""Stage 6 analysis: join mixed-trajectory arms with the existing solo results,
build the per-task deliverable row, classify RESCUE/SABOTAGE/NEUTRAL, and answer
the k-dependence question. $0 (reads JSON only)."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load(name):
    return json.loads((ROOT / name).read_text())


# --- solo outcomes (reuse, don't re-run) ---
solo = {}  # iid -> {cheap:(res,steps,cost), strong:(res,steps,cost)}
for arm, files in {"cheap": ["phase3_results_cheap.json", "phase3_results_cheap_light.json",
                             "phase3_results_cheap_full.json"],
                   "strong": ["phase3_results_strong.json", "phase3_results_strong_light.json",
                              "phase3_results_strong_full.json"]}.items():
    for f in files:
        for r in load(f):
            solo.setdefault(r["instance_id"], {})[arm] = (
                r["resolved"], r["num_steps"], round(r["cost_usd"], 3))

mixed = load("phase3_stage6_results.json")
# index mixed by (iid, kind)
mix = {}
for r in mixed:
    mix[(r["instance_id"], r["kind"])] = r

SPREAD = ["hiyouga__llama-factory-7505", "aws-cloudformation__cfn-lint-4009",
          "wireservice__csvkit-1281", "dynaconf__dynaconf-1241", "stanfordnlp__dspy-8739",
          "openai__openai-agents-python-1601", "django__asgiref-523",
          "rthalley__dnspython-1206", "Aider-AI__aider-4269"]
BOTHSOLVE_TESTED = ["joke2k__faker-2190", "a2aproject__a2a-python-226"]
BOTHFAIL_TESTED = ["Tuxemon__Tuxemon-3068", "a2aproject__a2a-python-302"]


def cell(iid, kind):
    r = mix.get((iid, kind))
    if not r:
        return "   -   "
    return f"{r['resolved']}@{r['handoff_step']}/{r['num_steps']}st/${r['total_cost']:.2f}"


print("=" * 110)
print("STAGE 6 DELIVERABLE TABLE  (resolved @ handoff_step / total_steps / $cost)")
print("=" * 110)
hdr = f"{'task':40s} {'cheap-solo':>14s} {'strong-solo':>14s} {'cs@early(k3)':>14s} {'cs@mid':>16s} {'reverse sc':>16s}"
print(hdr)
print("-" * 110)


def solo_cell(iid, arm):
    if iid not in solo or arm not in solo[iid]:
        return "   -   "
    res, st, c = solo[iid][arm]
    return f"{res}/{st}st/${c:.2f}"


allrows = SPREAD + BOTHSOLVE_TESTED + BOTHFAIL_TESTED
for iid in allrows:
    print(f"{iid:40s} {solo_cell(iid,'cheap'):>14s} {solo_cell(iid,'strong'):>14s} "
          f"{cell(iid,'cs_early'):>14s} {cell(iid,'cs_mid'):>16s} {cell(iid,'sc_early'):>16s}")

# ---------------- classification ----------------
print("\n" + "=" * 110)
print("CLASSIFICATION (forward = cheap->strong handoff)")
print("=" * 110)
rescue, sabotage, neutral = [], [], []
for (iid, kind), r in mix.items():
    if kind not in ("cs_early", "cs_mid"):
        continue
    cs = solo.get(iid, {}).get("cheap", (None,))[0]
    ss = solo.get(iid, {}).get("strong", (None,))[0]
    m = r["resolved"]
    if cs == 0 and m == 1:
        rescue.append((iid, kind))
    if ss == 1 and m == 0:
        sabotage.append((iid, kind))
    if m == ss:
        neutral.append((iid, kind))
print(f"RESCUE   (cheap-solo failed, cheap->strong resolved): {len(rescue)}")
for x in rescue:
    print("   ", x)
print(f"SABOTAGE (strong-solo resolved, cheap->strong failed): {len(sabotage)}")
for x in sabotage:
    print("   ", x)
print(f"NEUTRAL  (mixed outcome == strong-solo outcome): {len(neutral)} / "
      f"{sum(1 for (i,k) in mix if k in ('cs_early','cs_mid'))} forward arms")

# NEW resolves beyond strong-solo (does any mixed resolve a task strong-solo missed?)
new_beyond_strong = [(iid, kind) for (iid, kind), r in mix.items()
                     if r["resolved"] == 1 and solo.get(iid, {}).get("strong", (0,))[0] == 0]
print(f"\nMixed resolves a task STRONG-SOLO missed: {len(new_beyond_strong)} {new_beyond_strong}")

# ---------------- k-dependence ----------------
print("\n" + "=" * 110)
print("k-DEPENDENCE: does the cheap->strong outcome change with handoff position?")
print("=" * 110)
paired = [iid for iid in allrows if (iid, "cs_early") in mix and (iid, "cs_mid") in mix]
flips = 0
for iid in paired:
    e = mix[(iid, "cs_early")]["resolved"]
    m = mix[(iid, "cs_mid")]["resolved"]
    flag = "" if e == m else "  <-- FLIP"
    if e != m:
        flips += 1
    print(f"  {iid:40s} early(k=3)={e}  mid(k={mix[(iid,'cs_mid')]['k']})={m}{flag}")
print(f"\n  => {flips} outcome flips across {len(paired)} tasks tested at two handoff points.")

# ---------------- reverse (authorship direction) ----------------
print("\n" + "=" * 110)
print("REVERSE (strong->cheap): does early authorship matter, or who finishes?")
print("=" * 110)
for iid in BOTHSOLVE_TESTED + ["django__asgiref-523", "rthalley__dnspython-1206"]:
    r = mix.get((iid, "sc_early"))
    if not r:
        continue
    cs = solo.get(iid, {}).get("cheap", (None,))[0]
    ss = solo.get(iid, {}).get("strong", (None,))[0]
    print(f"  {iid:40s} cheap-solo={cs} strong-solo={ss}  strong->cheap(reverse)={r['resolved']} "
          f"(cheap finished {r['seg2_steps']} steps)")

# ---------------- cost: mixed vs strong-solo on the spread (rescued) tasks ----------------
print("\n" + "=" * 110)
print("COST: cheap->strong@early vs strong-solo on the 9 spread tasks")
print("=" * 110)
m_tot = s_tot = 0.0
for iid in SPREAD:
    me = mix[(iid, "cs_early")]["total_cost"]
    ss_cost = solo[iid]["strong"][2]
    m_tot += me
    s_tot += ss_cost
    print(f"  {iid:40s} mixed@early=${me:.3f}  strong-solo=${ss_cost:.3f}  "
          f"delta={'+' if me>ss_cost else ''}{me-ss_cost:+.3f}")
print(f"\n  TOTAL mixed@early=${m_tot:.3f}  strong-solo=${s_tot:.3f}  "
      f"(mixed is {(m_tot/s_tot-1)*100:+.1f}% vs always-strong)")
# mid cost inflation
mid_pairs = [iid for iid in SPREAD if (iid, "cs_mid") in mix]
me_e = sum(mix[(i, "cs_early")]["total_cost"] for i in mid_pairs)
me_m = sum(mix[(i, "cs_mid")]["total_cost"] for i in mid_pairs)
print(f"  On the {len(mid_pairs)} tasks with both: early=${me_e:.3f} vs mid=${me_m:.3f} "
      f"(mid {(me_m/me_e-1)*100:+.1f}% — later handoff inflates strong's inherited context).")
