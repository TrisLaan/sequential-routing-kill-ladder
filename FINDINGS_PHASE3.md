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

## Verdict
Domain NOT dead (unlike SQL): long horizon (~13-30, median ~18-22) + sequential failure
structure (0 clean-but-wrong; escalate-on-failure has triggers) + a real but THIN strong>cheap
spread (strong 1/5, cheap 0/5; one genuine LLaMA-Factory solve). Bottleneck on this
hardest-skewed set is task difficulty (strong near-misses on the right files), not budget.
Cheapest next step to measure true viability/spread magnitude: a LIGHTER representative task set
(N~8-10 small pure-Python repos, ~$3-6), then pre-registered Gate A. q-classifier/bandit/oracle/
ablations remain OUT OF SCOPE pending user decision.
