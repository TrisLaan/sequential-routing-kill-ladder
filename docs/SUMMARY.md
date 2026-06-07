# SUMMARY — load-bearing numbers (data only)

This file collects the decisive figures with a source citation for each, so the
README can be written from verified numbers rather than memory. **No narrative
spin** — reads and interpretation live in `docs/FINDINGS.md` (SQL) and
`docs/FINDINGS_PHASE3.md` (code agents).

Source legend:
- **[P3 DB]** = `traces_phase3.sqlite`, table `trajectory` (cols: `policy_label`,
  `final_success`, `total_cost_usd`, `num_steps`, `task_id`, `started_at`,
  `finished_at`). Verifiable with `python analyze_oof.py`.
- **[S6 DB]** = `traces_phase3_stage6.sqlite`, table `trajectory` (`policy_label`
  in {`mixed_cs`, `mixed_sc`}). Verifiable with `python analyze_stage6.py`.
- **[FP3]** = `docs/FINDINGS_PHASE3.md` (the stage write-ups; decompositions there
  are computed by `analyze_oof.py` / `analyze_stage6.py` over the DBs/results JSON).
- **[FSQL]** = `docs/FINDINGS.md` (SQL phase; computed by `archive/analyze_phase2.py`
  over `traces_phase2.sqlite`, which is .gitignored / not shipped).
- **[spend]** = the committed `phase3_*_spend.json` ledgers.

---

## 1. SQL phase (Phases 1–2, text-to-SQL on BIRD) — the first kill

| metric | value | source |
|---|---|---|
| Phase-1 baseline_strong exec-match (gpt-5.4, first 20 BIRD) | **70.0%** (14/20), $0.237 | [FSQL] |
| Phase-1 repair loop firings | **0** — every first draft executed clean; all 6 misses clean-but-wrong (semantic) | [FSQL] |
| Phase-2 baseline_strong exec-match (N=500) | 59.6% (298/500), $4.30 | [FSQL] |
| Phase-2 **cheap_all** exec-match (gpt-4.1-mini everywhere, N=500) | **58.8%** (294/500), **$0.46** → −0.8 pts, **−89% cost** | [FSQL] |
| Phase-2 cheap_no_schema (strong schema + cheap rest) | 59.4% (297/500), $2.50 | [FSQL] |
| Cascade gap (per-step "cheap agrees with strong" predictor vs realized cheap) | myopic 47.0% vs realized 58.8% = **−11.8 pts** (predictor under-predicts) | [FSQL] |
| Composition-attributed cheap failures (strong won, all shadows green, cheap still failed) | **6/500 = 1.2%**; 4 recovered by cheap_no_schema → **2/500 genuine** downstream coupling | [FSQL] |
| Schema marginal contribution | cheap_all − cheap_no_schema = −0.6 pts for +$2.04 cost → ≈0 | [FSQL] |

**Kill reason (SQL):** horizon ≈ 2 steps; failures are clean-but-wrong (semantic),
not execution failures → an exec-error-triggered repair/escalate has **no trigger**;
cheap nearly ties strong at ~1/10 cost in aggregate. No sequential structure for an
MDP to exploit. [FSQL], [FP3 intro]

---

## 2. Code-agent phase (SWE-bench-Live, Stages 2–4) — horizon + spread

Models: cheap `gpt-4.1-mini`, strong `gpt-5.4` (reasoning_effort=medium).

### Resolve rate per arm, full N=35 union (Stages 2+3+4), [P3 DB]
| arm (`policy_label`) | resolved | total cost | cost/resolved |
|---|---|---|---|
| `cheap_only`  | **5/35** (14%) | $4.273 | $0.855 |
| `strong_only` | **14/35** (40%) | $28.017 | $2.001 |

`strong_only` resolves are a **strict superset** of `cheap_only` (cheap-not-in-strong
= ∅, **0 violations**); union = 14/35. [P3 DB, verified]

### Horizon distribution (steps) — long, unlike SQL [FP3]
- Stage 2 (N=5): cheap steps 10,22,22,29,30 (med 22); strong 22,27,30,30,30 (med 30; corrected ~18 excl. post-submit filler).
- Stage 4 (N=22, post-cutoff): cheap median **31.5**, strong median **11.5** (`num_steps`). [P3 DB / FP3]
- SQL horizon for contrast: ≈2. [FSQL]

### Strong>cheap spread decomposition (Stage 4, N=22) [FP3]
- Spread (strong resolves, cheap does not) = **5 tasks**.
- (A) execution-survival / routable (cheap loops on failing tests): dspy-8739, dnspython-1206, aider-4269 = **3**.
- (B) flat semantic / clean-but-wrong (no sequential trigger): openai-agents-1601, asgiref-523 = **2**.
- **ROUTABLE FRACTION = 3/5 = 60%** (Stage-3 was 2/3 = 67%; pooled 5/8 = **62.5%**).
- Contamination: 0 resolves ≤4 steps; Stage-4 tasks dated 2025-06..08 (post gpt-5.4 cutoff) → uncontaminated. [FP3, analyze_oof contamination check]

### Stage 5 — escalate-on-failure offline sim (the moat test, $0) [FP3 §Stage5]
Reference policies over N=35 (costs from `trajectory.total_cost_usd`):
| policy | resolve | total cost | note |
|---|---|---|---|
| ALWAYS-CHEAP | 5/35 | $4.27 | = `cheap_only` [P3 DB] |
| ALWAYS-STRONG | 14/35 | $28.02 | = `strong_only` [P3 DB] |
| ESCALATE-loop3 (escalate at 3rd failing test) | 10/35 | $13.21 | misses 4 spread recoveries |
| ESCALATE-late (cheap to completion, then strong) | 14/35 | $30.63 | **> always-strong (dominated)** |
| ORACLE-anticipation (cheap iff cheap-wins) | 14/35 | **$26.72** | perfect-foresight ceiling |

- **Resolve ceiling = 14/35**, trigger-independent (strong⊇cheap → cheap adds 0 new resolves).
- **Max MDP saving over always-strong = $28.02 − $26.72 = $1.29 = 4.6%** (oracle ceiling, assumption-free).
- escalate-late is **dominated** (more cost than always-strong at equal resolve), because doomed cheap trajectories thrash to the step cap ($3.90 wasted cheap across 21 losses > $1.66 saved on cheap-wins).

---

## 3. Diversity probe (Stage 1A) — out-of-family models, same 35 tasks [P3 DB / FP3 §Stage1A]

### Per-model resolve (config.PRICES cost estimate; real OpenRouter ≈0.62×)
| model (`policy_label`) | family | resolved | cost/resolved (est) | source |
|---|---|---|---|---|
| `cheap_only` gpt-4.1-mini | OpenAI | 5/35 | $0.855 | [P3 DB] |
| `strong_only` gpt-5.4 | OpenAI | 14/35 | $2.001 | [P3 DB] |
| `qwen3coder` qwen/qwen3-coder | Qwen | **8/35** | $0.54 | [P3 DB] |
| `deepseek_v32` deepseek/deepseek-v3.2 | DeepSeek | **11/35** | $0.464 | [P3 DB] |
| `glm46` z-ai/glm-4.6 | Zhipu | **7/35** | $1.15 | [P3 DB] |

### Deliverables [analyze_oof.py output]
- **Nesting break = 1/35.** Out-of-family resolves that gpt-5.4 does NOT: just **cfn-lint-4016** (deepseek, 50 genuine steps). 25 of 26 total OOF resolves are nested under gpt-5.4.
- **Union {gpt-5.4 ∪ all OOF} = 15/35 (43%)** vs gpt-5.4 alone 14/35 (40%) → **+1 task / +3pp**.
- **Winner varies per task:** 15/35 resolved by ≥1 model; **13 contested** (≥2 models solve); distinct cheapest-winners among contested = all 5 models {deepseek, gpt-4.1-mini, qwen, gpt-5.4, glm}.
- **Cost-substitution oracle** (cheapest resolver per task, perfect foresight): 15 resolves for **$1.97** vs gpt-5.4's $5.94 on its own 14 → **≈69% cost saving**; cost/resolved $0.13 vs $0.42. (Substitution on tasks both solve — a bandit/portfolio signal, not sequential.)
- Contamination: 0 resolves ≤4 steps. [analyze_oof.py]

---

## 4. Mixed-trajectory (Stage 6) — the direct sequential test [S6 DB / analyze_stage6.py / FP3 §Stage6]

21 spliced arms; handoff at step k, state carried via shared on-disk testbed +
shared `messages` list; one tracer trajectory per run, `action_model` per step.

- **RESCUE (cheap→strong)** = **15/15** (9 spread @early k=3 + 6 @mid) — **but 0 resolve a task strong-solo missed** (rescue only vs cheap; zero capability beyond always-strong).
- **SABOTAGE (strong-solo resolved, cheap→strong failed)** = **0** (even at k=25, after cheap ran ~half its ~50-step thrash).
- **NEUTRAL (mixed outcome == strong-solo outcome)** = **17/17** forward arms.
- **k-invariance** = **0 outcome flips** across the 6 tasks run at both early (k=3) and mid (k=4/25).
- **Cost vs always-strong:** cheap→strong@early total **$3.947** vs always-strong **$4.278** = **−7.7%** (and that saving is just 3 cheap steps replacing 3 strong steps); @mid is **+41.6%** more expensive than @early (inherited context bloats strong's prompt) → positive transfer is net **negative** on cost.
- **Reverse arm (strong→cheap):** FAILS on spread tasks (asgiref-523, dnspython-1206 → 0, = cheap-solo); one both-solve task (a2a-226) also fails under strong→cheap → coupling shows up only as *negative* in the policy-irrelevant direction.

---

## 5. Total spend & wall-clock

| ledger | amount | scope | source |
|---|---|---|---|
| `phase3_spend.json` | **$32.75** | OpenAI, Stages 2–5 main runs (gpt-5.4 / gpt-4.1-mini) | [spend] |
| `phase3_stage6_spend.json` | **$7.89** | Stage 6 mixed-trajectory (OpenAI) | [spend] |
| **OpenAI Phase-3 total** | **$40.64** | = the "~$40.6" headline figure | sum of above |
| `phase3_oof_spend.json` | **$17.50** *(accounting estimate)* | Stage 1A out-of-family via OpenRouter; **real charge ≈ $11** (config.PRICES list rates run ~1.6× hot; the $15 OpenRouter key cap was the true backstop) | [spend] / [FP3 §Stage1A] |
| **Grand total incl. OOF** | **≈ $51.6 real → $58.2 by estimate** | all of Phase 3 | sum |

- **Wall-clock:** ≈ **13.1 h** cumulative agent runtime summed over the 175
  Phase-3 trajectories (`traces_phase3.sqlite` started_at→finished_at), spread
  across a ~48 h calendar window (2026-06-05 → 2026-06-07). Stage-6 trajectories
  did not log start/finish timestamps. [P3 DB]

> Earlier SQL-phase spend (Phases 1–2): Phase-1 $0.237, Phase-2 $4.30+$0.46+$2.50
> (per-arm), recorded in [FSQL]; not part of the Phase-3 total above.
