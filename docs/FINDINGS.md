# sql-cost-opt — Results & Findings

Text-to-SQL cost-optimization harness on the **BIRD** dev set.
Strong model = `gpt-5.4` (reasoning, `reasoning_effort="medium"`); cheap model =
`gpt-4.1-mini`. Exec-match = official BIRD set-equality vs gold (`data/evaluate.py`).

Reproduce: `python analyze_phase2.py` (deliverables #1–#7).
Data: Phase 1 → `traces.sqlite`; Phase 2 → `traces_phase2.sqlite` (separate DB so
Phase-1 baseline rows can't pollute Phase-2 aggregates). Phase-2 sample manifest →
`phase2_sample.json` (seed=20240627, ordered by `question_id` before sampling).

---

## Phase 1 — baseline_strong (first 20 BIRD tasks, dataset order)

`gpt-5.4` at every step: `schema_retrieval → draft_sql → repair×≤3 → finalize`.

- **70.0% exec-match (14/20), $0.2371 (~$0.012/task).**
- The **repair loop never fired**: every first draft executed cleanly. The 6
  misses were all *clean-executing-but-wrong* queries (semantic errors), which an
  exec-error-triggered repair structurally cannot fix. So the 70% ceiling was
  bounded by draft semantics, not execution failures.

---

## Phase 2 — shadow mode + counterfactual arms (seeded N=500)

Three arms per task, all keyed to the same `task_id`:
- **A · baseline_strong** (unchanged) + per-step **cheap shadow** (counterfactual
  "could the cheap model have done this step?" — logged, never acts).
- **B · cheap_all** (`policy='shadow', arm='cheap_all'`): `gpt-4.1-mini` everywhere.
- **C · cheap_no_schema** (`arm='cheap_no_schema'`): strong schema step, cheap rest.

Cost invariant (checked): the production baseline number is summed **only** over
`policy='baseline_strong'`; shadow rows and arms B/C never leak into it.

### #2 — Realized exec-match vs gold, and policy-scoped cost
| Arm | exec-match | cost | vs baseline |
|---|---|---|---|
| baseline_strong (gpt-5.4 everywhere) | 59.6% (298/500) [55.2, 63.8] | $4.30 | — |
| **cheap_all** (gpt-4.1-mini everywhere) | **58.8%** (294/500) [54.4, 63.0] | **$0.46** | **−0.8 pts, −89% cost** |
| cheap_no_schema (strong schema + cheap rest) | 59.4% (297/500) [55.0, 63.6] | $2.50 | −0.2 pts, −42% cost |

**Headline:** the cheap model end-to-end **nearly ties** the strong baseline at
~1/10 the cost. CIs overlap heavily — the aggregate accuracy difference is not
significant.

### Exec-match × BIRD difficulty (the aggregate parity is misleading)
| Difficulty | n | baseline_strong | cheap_all | Δ (cheap−strong) |
|---|---:|---|---|---:|
| simple | 296 (59%) | 66.9% | 66.6% | −0.3 |
| moderate | 152 (30%) | 51.3% | 46.1% | **−5.3** |
| challenging | 52 (10%) | 42.3% | 51.9% | +9.6 (small-n, likely noise) |

- The sample **skews easy** (59% simple), and on `simple` the arms tie — that
  props up the aggregate.
- **`moderate` is where strong actually pays off (−5.3 pts, n=152).**
- `challenging` favoring cheap is a 5-task swing on n=52 with overlapping CIs —
  **not** a reliable "cheap beats strong on hard tasks" signal.

### #3 — Cascade gap is NEGATIVE: −11.8 pts
Myopic predictor (cheap succeeds iff strong succeeded **and** every per-step shadow
matched) = 47.0%, but realized cheap_all = 58.8%. Per-step "cheap must agree with
strong everywhere" **badly under-predicts** cheap's real success: cheap reaches
gold via divergent-but-correct paths, and the schema-superset proxy is conservative
(80.6% match). A per-step-agreement router would be far too pessimistic.

### #4 — Composition-attributed failures (the real risk): 1.2%
Tasks where strong won, every shadow label was green, yet cheap_all still failed:
**6/500 = 1.2% [0.55, 2.59]** (conditional 6/235 = 2.6%). Of those 6, **4 are
recovered by cheap_no_schema** (strong schema → schema-proxy artifact / schema
mattered), leaving just **2/500 genuine downstream draft/repair coupling** failures.

### #5 — Schema marginal contribution: ~0
cheap_all − cheap_no_schema = **−0.6 pts**, but cheap_no_schema costs $2.50 vs
$0.46. The expensive strong schema step buys essentially nothing in aggregate on
BIRD.

### #6 — schema_retrieval shadow match-rate
80.6% overall; variance 0.025 across 11 DBs, range **52–100%** → **state-dependent**,
not a clean static rule.

### #1 — Myopic upper-bound saving (upper bound on strong-induced states)
64.3% of baseline cost flagged "movable" (schema 38.9% ⟵ PROXY, most inflated;
draft 25.2%; repair 0.2%). The realized arms already beat this bound.

### #7 — Repair-chain coverage (first real-data draft←repair links)
27 (cheap_all) + 12 (cheap_no_schema) = **39** `retry_of_step_id` links. Baseline
fired repair once over 500 tasks. The cheap model produces execution-erroring SQL
markedly more often than strong.

---

## Implications for routing (Phase 4–5)

- The aggregate tie is **not** a mandate for "always cheap." Routing value is
  concentrated in the **moderate** bucket (−5.3 pts), where strong genuinely helps;
  `simple → cheap` captures the bulk of the savings safely.
- Per-step shadow agreement is a **poor success predictor** (negative cascade gap),
  so a confidence/value signal for routing must be more than "cheap matched strong."
- The strong **schema step is not worth its cost** here; if anything is routed to
  strong, it should be the draft/repair body, not schema linking.
- **Caveat:** these conclusions are on one BIRD difficulty mix that skews easy.
  Validate on a harder / larger `moderate`+`challenging` sample before generalizing.
