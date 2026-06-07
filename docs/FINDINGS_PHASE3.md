# Phase 3 — Code-agent domain de-risk (Stage 2 thin smoke test)

Goal: cheapest-signal test of whether the SEQUENTIAL/MDP cost-routing thesis has
ground in code agents, after SQL was killed (horizon ~2, failures clean-but-wrong).
Two questions: HORIZON (long?) and VIABILITY (cheap resolves a non-trivial fraction
+ is there a strong>cheap spread?).

## Setup
- Harness: mini-swe-agent ReAct loop + its `swebench_backticks` prompt, driven by OUR
  OpenAIClient + config.cost_usd + tracer (not litellm — can't price 2026 models, and
  this path is $0-validatable). Execution via Git-bash, per-task venv on PATH. No Docker.
- Tasks: SWE-bench-Live `lite`, pre-registered mechanical rule — created_at>=2025-01-01,
  order created_at DESC, first 5 that fetch+install (<=300s)+verify well-formed on this
  Windows host (>=1 F2P fails at base, gold patch makes all F2P pass). 7 skipped w/ reasons.
  Chosen (all single-F2P, large repos): pypsa-1195, llama-factory-7505, helm-3467,
  python-control-1142, yt-dlp-12714. Resolve = all FAIL_TO_PASS pass (P2P regression
  out of scope). Tracer migration additive; phase3 in its own traces_phase3.sqlite.
- Models: cheap gpt-4.1-mini, strong gpt-5.4 (reasoning_effort=medium). Strong cap
  $1.50/task; $25 cumulative breaker. Total spend $7.30 ($0.39 cheap + $6.90 strong).

## Results
| | cheap | strong |
|---|---|---|
| resolve | 0/5 | 1/5 (llama-factory, 30-step genuine) |
| horizon steps | 10,22,22,29,30 (med 22) | 22,27,30,30,30 (med 30) |
| cost/task | $0.079±0.060 | $1.38±0.25 |
| failures | 5/5 EXECUTION/TEST-FAIL, 0 clean-but-wrong | 4/4 EXEC/TEST-FAIL, 0 clean-but-wrong |
| capped (truncated) | 0 | 3 (pypsa, helm, yt-dlp) |

## Read
- HORIZON: long (10-30, median 22-30) with real write->run->test-fail->fix loops.
  Opposite of SQL (~2). Thesis horizon-leg has ground.
- FAILURE STRUCTURE: 0/9 unresolved trajectories were clean-but-wrong; ALL were
  execution/test-fail => escalate-on-failure has triggers => sequential coupling present.
  Exact inverse of SQL. Step-level pass/fail logged (state_features) => escalate-on-failure
  simulatable without re-running.
- VIABILITY/SPREAD: real but under-measured. Strong solved 1 task cheap could not (spread
  exists; not the "both ~0/5 unsolvable" branch). But hardest-skew + $1.50 cap truncated
  3/5 strong runs. On the 2 un-truncated strong runs: 1/2. Cheap 0/5 on the hardest set is
  directional, not a domain rate.
- Contamination: lite max date 2025-03 (clean for cheap; possibly in gpt-5.4 window). The
  one strong resolve was 30 steps (NOT memorized). No GUI/hang artifacts (MPLBACKEND=Agg).

## Correction (post-hoc, from logs, $0): the 3 "cost-capped" strong tasks were a HARNESS BUG
Submit-detection originally gated on the command starting with `echo SENTINEL`, but gpt-5.4
prefixed `cd {cwd} && echo SENTINEL && cat patch.txt` -> submission ran but wasn't recognized
-> agent burned remaining budget on `true` no-ops until the $1.50 cap. The cap was an ARTIFACT,
not mid-progress truncation. All 3 had already submitted COMPLETE candidate patches editing the
CORRECT gold-patch files. Fixed: detection now keys off the output first line (matches
mini-swe-agent LocalEnvironment._check_finished).

Corrected strong horizons (excl. post-submit filler): pypsa submit@19 (~20, wrong),
llama submit@12 (~13, RESOLVED — genuine multi-step, NOT memorized), helm submit@17 (~18, wrong),
python-control 30 (kept iterating, no filler, wrong), yt-dlp submit@12 (~13, wrong). Strong
resolve = **1/5 FIRM** (the 4 failures are completed right-file-but-wrong near-misses, NOT
budget casualties). Median corrected horizon ~18. Re-running with more budget unwarranted
(tasks self-terminated) -> un-truncation step spent $0. Total de-risk phase spend $7.30.

## Stage 3 — viability/spread on a LIGHTER representative set (N=8, fresh small pure-Python)
Set (mechanical: created_at DESC, fresh/not-in-stage2, 1<=F2P<=2, simple node-ids, no
GPU/DL deps, installs<=300s, well-formed here): yt-dlp-12684, faker-2190, cfn-lint-{4016,
4009,3982}, csvkit-1281, dynaconf-{1249,1241}. 33 skipped (torch/clang/C-lib/collection-err).
Strong cap $6/task, step cap 50, MPLBACKEND=Agg. Submit-detection bug fixed; ALSO fixed a
Windows subprocess hang (timeout killed bash but not grandchildren holding the stdout pipe ->
now taskkill /F /T the tree on timeout). Cumulative spend after Stage 3 = $14.64 (<$25/<$40).

Results: cheap 1/8, strong 4/8. Strong>cheap spread on 3 tasks => routing has something to
decide; viability leg STANDS (cheap is a participant but weak, 1/8). No contamination flags
(resolves at 7-14 genuine steps). Horizon: cheap median 40 (thrashes to step cap on exec-fail),
strong median 13 (submits efficiently). Cost: cheap $0.17/task, strong $0.69/task.

Failure-mode breakdown (the open concern): cheap unresolved = 4 EXECUTION/TEST-FAIL (A) +
3 SEMANTIC near-miss (B); strong unresolved = 1 A + 3 B. The 3 spread tasks' CHEAP mode:
cfn-lint-4009 (A), dynaconf-1241 (A), csvkit-1281 (B) => 2 of 3 strong>cheap wins are the
ROUTABLE execution-survival pattern (cheap loops on failing checks -> escalate -> strong
rescues), 1 is flat-capability semantic. Dominant single mode across both arms is still
semantic near-miss (6/11 unresolved), but cheap uniquely carries the exec-survival failures
strong fixes.

Stage-3 read: MIXED but encouraging for the sequential thesis — a real but MODEST routable
target (execution-survival, ~2/8) plus a flat-capability (semantic) component. Stronger than
SQL (which had neither horizon nor a routable failure mode); not a slam-dunk for a large MDP
anticipation edge. Next (gated, not started): Gate A cascade-prevalence, or larger-N (~25-30)
viability, or stop. q-classifier/bandit/oracle/ablations OUT OF SCOPE.

## Stage 4 — larger-N + POST-CUTOFF (full split, N=22; the contamination-clean headline)
Set: 22 fresh tasks from SWE-bench-Live `full`, dates 2025-06-23..2025-08-30 (safely past
gpt-5.4 cutoff -> removes the memorization caveat). Same EXACT Stage-3 mechanical filter,
excluding all stage2/3 instances. 123 skipped. Strong per-task cap lowered 6.0->2.5 to cover
all 22 within the $35 breaker (strong resolves are early/cheap, so this preserves the spread
signal); only SDV-2636 hit the cap. Final cumulative spend $32.75 (<$35, <$40).

Results: cheap 4/22, strong 9/22 (strong resolves are a strict SUPERSET of cheap's). 
PRIMARY — spread (strong>cheap) = 5 tasks, decomposed by CHEAP's failure mode:
  (A) execution-survival (routable): dspy-8739, dnspython-1206, aider-4269  = 3
  (B) flat semantic (not sequential): openai-agents-1601, asgiref-523       = 2
  ROUTABLE FRACTION = 3/5 = 60%. HOLDS vs Stage-3's 2/3=67%. Pooled 5/8=62.5%.
Secondary: cheap unresolved 10A/8B; strong unresolved 4A/9B. Horizon med: cheap 31.5,
strong 11.5. Cost: cheap $2.56, strong $15.56. NO suspicious short resolves (all 8-19 steps)
and post-cutoff => spread is UNCONTAMINATED.

Stage-4 read: the execution-survival (sequential, routable) axis HOLDS at larger N and is
contamination-clean (60% of spread, 62.5% pooled) -> robust, not a 2-task fluke -> clears the
bar the user set for justifying Gate A. Cheap is a real participant (4/22~18%). Flat-semantic
is also real (~40% of spread; dominant among both-fail) so routing value is a MIX, but the
sequential component is solidly present (the load-bearing MDP claim). Strongest pro-thesis
evidence so far. Next (gated, not started): Gate A cascade-prevalence (largely simulatable
offline from logged step checks), or stop and design the MDP-vs-escalate study.

## Stage 5 — ESCALATE-ON-FAILURE offline simulation (the moat test; $0, read-only on traces_phase3.sqlite)
The real competitor to an MDP is NOT a bandit — it's escalate-on-failure (run cheap; when a
step's check fails, hand off to strong for the rest). If reaction captures the execution-survival
savings for free, the MDP's anticipation edge has no room. Measured from the existing N=35
step-level logs (Stage2 5 + Stage3 8 + Stage4 22). NO new model calls.

### Simulation soundness (stated explicitly — the one unsound place)
Per task we have TWO independent full trajectories, each from a CLEAN repo: cheap-only and
strong-only. We do NOT have "cheap until step k, then strong continues from cheap's partial
repo/conversation state." So strong-continuing-from-cheap's-state is UNOBSERVED. Approximations:
- OUTCOME: escalate's outcome := strong-only's realized outcome. Justified by a CONFIRMED strict
  superset (strong's resolves ⊇ cheap's resolves, 0/35 violations) so handing to strong recovers
  at least strong's set. Bias: cheap's partial wrong edits could derail strong (optimistic) or its
  correct prep could help (pessimistic); on these tasks cheap mostly THRASHES (exploration/failing
  tests, no committed destructive correct-direction work) → strong-outcome is a fair central est.
- COST: escalate cost := (cheap cost actually incurred up to the escalation step) + (strong-only's
  FULL realized cost). Treats strong as redoing from scratch post-handoff → OVERSTATES the strong
  leg (escalate-and-stay-strong would only do the REMAINDER) → escalate cost is biased HIGH
  (conservative). The clean results below (resolve ceiling, oracle headroom) DON'T depend on this.
- TRIGGER: per-step `check`(pass/fail), `is_test_run`, `is_submit` logged in state_features. A
  "first failing check" trigger is DEGENERATE — step-0 exploration commands return non-zero on
  ~every trajectory → escalate ≡ always-strong. The meaningful signal is failing TEST runs (Ft);
  cheap-WINS show transient Ft then converge to passing-submit, cheap exec-survival LOSSES LOOP on
  Ft to the step cap. We report two endpoints that bracket reaction (trigger sensitivity is real).

### Three reference policies + escalate, full N=35
| policy | resolve | total cost | cost/resolved | note |
|---|---|---|---|---|
| ALWAYS-CHEAP | 5/35 (14%) | $4.27 | $0.855 | median $0.121 |
| ALWAYS-STRONG | 14/35 (40%) | $28.02 | $2.001 | median $0.537 |
| ESCALATE-loop3 (escalate at 3rd failing test) | **10/35** | $13.21 | $1.321 | misses 4 spread recoveries |
| ESCALATE-late (cheap-to-completion, then strong) | 14/35 | **$30.63** | $2.188 | > always-strong! |
| ORACLE-anticipation (cheap iff cheap-wins, else strong) | 14/35 | $26.72 | $1.908 | perfect-foresight ceiling |

Pareto: {always-cheap, escalate-loop3, oracle} are non-dominated; always-strong sits just behind
oracle; **escalate-late is DOMINATED** (more cost than always-strong at the same resolve).

### THE HEADLINE — escalate on the 5 execution-survival routable tasks (dspy-8739, dnspython-1206,
aider-4269, cfn-lint-4009, dynaconf-1241)
Reaction DOES resolve the 4 loop-type ones (cheap visibly loops on failing tests → escalate →
strong rescues, all strong-resolved). BUT cost is the surprise: on a recovered task escalate pays
cheap_sunk + strong ≥ strong, so it is MORE expensive than always-strong, not cheaper. On the 4
loop tasks (excl. cfn-4009 which has 0 flagged test-fails): always-strong $0.89 vs escalate $1.71;
across all 5: always-strong $1.49 vs escalate $2.54 — a **+$1.05 cheap-waste tax**. The "savings
reaction captures for free" is INVERTED on recovered tasks: reaction's only saving vs always-strong
is the strong-cost it AVOIDS on the 5 cheap-WIN tasks ($1.66), not anything on recovered tasks.

### THE READ (stated plainly)
1. RESOLVE CEILING is exact & trigger-independent: strong⊇cheap (0 violations) → no cheap-using
   policy (incl. any MDP) resolves >14/35; cheap adds zero NEW resolves.
2. ANTICIPATION HEADROOM is THIN and assumption-free: the oracle (perfect foresight, route cheap
   only where cheap wins) costs $26.72 vs always-strong $28.02 → **max MDP saving = $1.29 = 4.6%**.
   This uses only exact cheap-win costs + strong costs; independent of the continuation model. The
   entire moat over always-strong is ≤4.6% on this set.
3. REACTION DOESN'T DOMINATE: escalate-late (the only escalate variant reaching 14/35) is MORE
   expensive than always-strong ($30.63 vs $28.02) because cheap's doomed trajectories thrash to
   the step cap ($3.90 wasted cheap across 21 losses) — more than the $1.66 it saves on cheap-wins.
   escalate-loop3 is cheaper ($13.21) but resolves only 10/35: it MISSES 4 spread tasks with <3
   flagged test-fails — cfn-4009 (loops on failing COMMANDS, not tests) and the 3 FLAT-SEMANTIC
   spread tasks (asgiref-523, openai-1601, llama-7505) where cheap PASSES its own checks but is
   wrong (SQL-like clean-but-wrong → reaction has NO trigger).
4. WHAT'S LEFT FOR AN MDP: the sequential/loop spread (the load-bearing "execution-survival" axis)
   is recoverable by pure REACTION — so the MDP's distinctly-sequential anticipation adds little
   there. The spread reaction can't get is flat-semantic, but routing those needs a per-task
   classifier/BANDIT (predict cheap-will-fail, route strong upfront), NOT a sequential MDP.
   Net: the distinctly-MDP value beyond escalate-on-failure looks THIN on this set.

### The one thing offline data CANNOT rule out (honest residual)
Whether cheap's partial work makes strong's COMPLETION cheaper than strong-from-scratch (positive
transfer). My logs have no spliced "cheap-then-strong" trajectory to measure it; I conservatively
assumed strong redoes from scratch. If positive transfer is large, escalate/MDP could beat
always-strong by more than the 4.6% oracle ceiling. The thrash-heavy cheap losses suggest transfer
is small, but this is the only door this $0 analysis leaves open — and the cheapest thing a next
step could measure (a handful of real spliced escalate runs).

## Stage 1A — MODEL-DIVERSITY PROBE (does out-of-family break strong⊇cheap nesting?)
Stage-5 showed the MDP routing headroom is ~4.6% because gpt-5.4 (strong) resolves a strict
SUPERSET of gpt-4.1-mini (cheap), 0 violations — same family, correlated errors. Hypothesis:
out-of-family models make DIFFERENT errors → may resolve tasks gpt-5.4 misses → non-nested
sets → higher ceiling. Ran 3 out-of-family models via OpenRouter on the SAME 35 tasks, same
harness/grading, step cap 50, ~$2/task cap. Real spend ~$11 (estimate $17.5; config.PRICES
list rates run ~1.6x hot vs OpenRouter's real charges — the $15 key cap was the true backstop).
Models: qwen/qwen3-coder (Qwen), deepseek/deepseek-v3.2 (DeepSeek), z-ai/glm-4.6 (Zhipu),
non-reasoning (temperature=0 path). Additive wiring only (client base_url/api_key + .env reader,
config.PRICES, tracer policy whitelist, run_phase3 `oof` arm); baseline gpt-5.4/4.1-mini paths
and data untouched. No contamination (0 resolves <=4 steps).

### Per-model resolve / cost (cost = config.PRICES estimate; real ~0.62x)
| model | family | resolve | cost/resolved (est) |
|---|---|---|---|
| gpt-4.1-mini | OpenAI (cheap) | 5/35 | $0.86 |
| gpt-5.4 | OpenAI (strong) | 14/35 | $2.00 |
| qwen3-coder | Qwen | 8/35 | $0.54 |
| deepseek-v3.2 | DeepSeek | 11/35 | $0.46 |
| glm-4.6 | Zhipu | 7/35 | $1.15 |

### DELIVERABLE 1 — NESTING BREAK (capability ceiling): essentially HOLDS (1/35 break)
Out-of-family resolves that gpt-5.4 does NOT: **just 1** — `cfn-lint-4016` (deepseek-v3.2, 50
genuine steps; gpt-5.4 self-terminated at step 8 with a WRONG patch → real different-error
break, not a budget artifact). Across all 3 OOF models / 26 total OOF resolves, **25 are nested
under gpt-5.4**; only that 1 is outside. UNION {gpt-5.4 ∪ all OOF} = **15/35 (43%)** vs gpt-5.4
alone 14/35 (40%) → **+1 task / +3pp**. The "ceiling much higher" hypothesis did NOT
materialize: out-of-family diversity adds essentially NO new capability on this domain. The
degeneracy (one strong model ≈ the whole resolvable frontier) is DEEPER than model family.

### DELIVERABLE 2 — WINNER VARIES PER TASK (cost-routing signal): YES
15/35 tasks resolved by ≥1 model; **13 contested** (≥2 models can solve). Cheapest successful
model is spread across ALL FIVE: deepseek 4, gpt-4.1-mini 4, qwen 3, gpt-5.4 2, glm 2 → winner
genuinely VARIES per task. Out-of-family models are the CHEAPEST resolver on 9/15 tasks. On the
14 tasks gpt-5.4 solves, a cheaper model also solves 12 of them. Cost-routing oracle
(cheapest resolver/task, perfect foresight): **15 resolves for $1.97** vs gpt-5.4's $5.94 on its
own 14 → ~69% cost saving available; cost/resolved $0.13 vs gpt-5.4 $0.42.

### THE READ (plainly)
- CAPABILITY axis → leans KILL: out-of-family diversity barely moved the ceiling (+1 task,
  one marginal/possibly-fragile break where deepseek thrashed to the step cap). Nesting
  essentially holds even across families → the capability degeneracy is robust, not an
  artifact of same-family correlated errors. This is the "harder kill" branch.
- COST axis → real but NOT MDP-shaped: there IS a per-task routing decision (cheapest model
  varies, ~69% oracle cost saving over always-gpt-5.4 on its resolves). BUT this is COST
  SUBSTITUTION (a cheaper model replaces an expensive one on tasks BOTH solve), i.e. a
  bandit/portfolio/classifier problem — NOT the sequential anticipation an MDP would add. And
  per Stage 5, escalate-on-failure already captures most reactive cost savings for free. So the
  cost-routing headroom does NOT revive the SEQUENTIAL/MDP thesis; it restates the portfolio
  one.
- NET: multi-provider diversity expands cheap SUBSTITUTES, not the capability frontier. The
  load-bearing pro-MDP hope (out-of-family lifts the ceiling a lot) is NOT supported here.
  The one remaining shot at a real ceiling lift is a FRONTIER out-of-family model (Claude
  Sonnet) on the contested subset — pre-gated as Stage 1B, user's decision.

## Stage 6 — MIXED-TRAJECTORY ROUTING (the DIRECT sequential test; $7.89, dedicated stage-6 ledger)
Everything prior measured models running WHOLE trajectories SOLO, or shadow-labeled the strong
trajectory step by step. We never ran a TRUE mixed trajectory where one model does the early steps,
hands off its ACCUMULATED state, and another finishes mid-task. Stage 5 inferred weak coupling from
two independent solo trajectories; the one door it left open was that strong-continuing-from-cheap's-
state was UNOBSERVED. This stage observes it directly.

### How state is carried across the switch (auditable — code in phase3_handoff.py)
Handoff at step k: model A runs ReAct steps 0..k-1 on a CLEAN testbed; model B runs k..end. B inherits
A's state two ways, neither reset: (1) the SAME on-disk working dir C:\p3\<iid>\testbed (no git-reset /
re-clone between segments — every file A edited, every repro/patch.txt A wrote, persists); (2) the SAME
`messages` list (B's first call sees A's whole conversation — every THOUGHT/command + every shell
observation). B is NOT told a switch happened → isolates PURE state coupling, not a coaching effect.
Both segments run the IDENTICAL phase3_agent.react_loop (extracted, behavior-preserving — verified the
prior `mock` is byte-identical); only the model/client swaps. ONE tracer trajectory per run, each step
row carries its own action_model so the switch is visible. Mock-validated at $0 (strong segment read a
file the cheap segment wrote → proves on-disk inheritance). Own DB (traces_phase3_stage6.sqlite) + own
spend ledger ($12 soft-stop / $15 hard; per-task $2; step cap 50) — the historical OpenAI $32.75 record
untouched. 21 arms, $7.89 (cheap segments ~free; strong-completion is the cost).

### Deliverable table (resolved @ handoff / total_steps / $; cs = cheap→strong, sc = reverse)
| task (cheap-solo→strong-solo) | cs@early k3 | cs@mid | reverse sc |
|---|---|---|---|
| dynaconf-1241 (0→1) | 1@3 (10st,$.13) | 1@25 (40st,$.62) | — |
| asgiref-523 (0→1)   | 1@3 (11st,$.16) | 1@4 (12st,$.15)  | **0**@3 (10st) |
| dnspython-1206 (0→1)| 1@3 (14st,$.30) | 1@25 (32st,$.37) | **0**@3 (50st) |
| dspy-8739 (0→1)     | 1@3 (15st,$.32) | 1@25 (33st,$.40) | — |
| aider-4269 (0→1)    | 1@3 (16st,$.39) | 1@25 (40st,$.67) | — |
| openai-1601 (0→1)   | 1@3 (19st,$.35) | — | — |
| cfn-4009 (0→1)      | 1@3 (23st,$.93) | 1@25 (41st,$.96) | — |
| csvkit-1281 (0→1)   | 1@3 (17st,$.35) | — | — |
| llama-7505 (0→1)    | 1@3 (23st,$1.01)| — | — |
| faker-2190 (1→1)    | — | — | 1@3 (6st) |
| a2a-226 (1→1)       | — | — | **0**@3 (26st) |
| Tuxemon-3068 (0→0)  | 0@3 (11st) | — | — |
| a2a-302 (0→0)       | 0@3 (10st) | — | — |

### HEADLINE NUMBERS
- **RESCUE** (cheap-solo failed, cheap→strong resolved) = **15/15** (9 spread @early + 6 @mid) — BUT
  **0** of them resolve a task strong-solo missed. They are "rescues" only vs CHEAP; the handoff adds
  ZERO capability beyond always-strong. (Both-fail probe: Tuxemon, a2a-302 cheap→strong = 0/2, no rescue.)
- **SABOTAGE** (strong-solo resolved, cheap→strong failed) = **0**. Cheap's early steps NEVER poisoned
  strong — not even at k=25, where cheap had already run HALF of its full ~50-step thrash (failing tests,
  partial edits) before handing off. Strong recovered every time.
- **NEUTRAL** = **17/17** forward arms: the mixed outcome EQUALS the strong-solo outcome, every time.
- **k-DEPENDENCE = 0 outcome flips** across the 6 tasks run at BOTH early (k=3) and mid (k=4/25). The
  cheap→strong outcome is INVARIANT to where the handoff happens.

### THE READ (this is the anti-thesis branch, stated plainly)
Rescue-beyond-strong ≈ 0 AND sabotage = 0 AND outcome invariant to k ⇒ early-step authorship does NOT
propagate. The mixed trajectory behaves exactly as "whoever does the HARD LATER steps decides the
outcome," with no residual effect from who authored the early steps. **State coupling is WEAK in the
policy-relevant (cheap→escalate→strong) direction — now DIRECTLY confirmed, not inferred.** This is the
exact result Stage 5 predicted from offline simulation; the splice we couldn't observe then now confirms it.

The REVERSE arm proves it's about the FINISHER, not authorship: strong→cheap FAILS on the spread tasks
(asgiref, dnspython — cheap finisher can't do the hard step, = cheap-solo) while cheap→strong on the SAME
tasks resolves. Direction-of-handoff, not position, is what moves the outcome — and only because it
changes who does the later steps. (One honest exception: a2a-226, a both-solve task, FAILED under
strong→cheap even though cheap-solo resolved it — strong's early authorship led the cheap finisher down a
path it couldn't complete. So coupling is not literally zero; it shows up as NEGATIVE in the reverse
direction. But that direction is irrelevant to a cost-routing policy, which only ever escalates cheap→strong.)

### CLOSES Stage 5's one open door (positive transfer)
Stage 5 conservatively assumed strong redoes from scratch post-handoff and flagged "does cheap's partial
work make strong's COMPLETION cheaper?" as the only unmeasured pro-thesis door. Now measured on real
spliced runs: cheap→strong@early totals $3.95 vs always-strong $4.28 (−7.7% — and that small saving is
just 3 cheap steps replacing 3 strong steps, NOT strong finishing more efficiently). And @mid is **+41.6%
MORE expensive** than @early on the same tasks: inheriting cheap's accumulated context BLOATS strong's
prompt — a tax, not a transfer saving. So positive transfer is not merely small, it's net NEGATIVE on
cost. The door is closed: the handoff neither resolves more nor costs less than always-strong.

### NET
The direct mixed-trajectory test confirms the inference from Stages 4–5: on this code-agent domain the
distinctly-SEQUENTIAL/MDP value over always-strong (and over reactive escalate) is ~nil. Strong is robust
to whatever cheap accumulates; the outcome is set by who does the hard later steps; the handoff adds no
resolves and no cost saving. The sequential thesis is honestly CLOSED on this domain — measured, not
merely inferred. What real routing value remains is portfolio/substitution (Stage 1A: a cheaper model
often co-solves a task strong solves) — a bandit/classifier problem, NOT an MDP.

## Verdict
Domain NOT dead (unlike SQL): long horizon (~13-30, median ~18-22) + sequential failure
structure (0 clean-but-wrong; escalate-on-failure has triggers) + a real but THIN strong>cheap
spread (strong 1/5, cheap 0/5; one genuine LLaMA-Factory solve). Bottleneck on this
hardest-skewed set is task difficulty (strong near-misses on the right files), not budget.
Cheapest next step to measure true viability/spread magnitude: a LIGHTER representative task set
(N~8-10 small pure-Python repos, ~$3-6), then pre-registered Gate A. q-classifier/bandit/oracle/
ablations remain OUT OF SCOPE pending user decision.
