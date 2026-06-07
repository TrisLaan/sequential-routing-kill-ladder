# sql-cost-opt → code-agent cost-routing: a kill-ladder study

<!-- TODO(prose): one-line summary. Suggested shape: "An honest, staged
investigation into whether SEQUENTIAL (MDP) cost-routing between a cheap and a
strong model is a defensible moat — across text-to-SQL and code agents — that
ends by closing its own thesis." Write your own one-liner here. -->
**TODO — one-line summary.**

> Status: research complete. The sequential/MDP thesis is **honestly closed** on
> the domains tested; the remaining real value is per-prompt cost *substitution*
> (a portfolio/bandit problem), not sequential anticipation. All numbers below are
> verified against the committed trace DBs — see [`docs/SUMMARY.md`](docs/SUMMARY.md)
> for per-figure source citations.

---

## 1. The question / thesis

<!-- TODO(prose): explain, in your framing:
  - What "sequential MDP routing as a moat" means: a policy that, step by step
    inside a single multi-step task, decides whether to spend the cheap or the
    strong model, using accumulated state to ANTICIPATE where strong is needed —
    as opposed to (a) always-strong, (b) a static per-task classifier/bandit, or
    (c) reactive escalate-on-failure.
  - Why it would matter if real: it would be a defensible, data-driven moat
    (cost down with no quality loss) that a competitor couldn't trivially copy,
    because it requires the trajectory data + the learned value function.
  - The two things that must BOTH hold for the MDP framing to have teeth:
    (1) long horizon, and (2) genuine sequential coupling (early decisions change
    later optimal actions) that reaction/substitution can't already capture. -->
**TODO — thesis prose.** Load-bearing definitions to keep precise: *sequential/MDP
routing* (anticipatory, step-level) vs *escalate-on-failure* (reactive) vs
*cost-substitution* (per-task, portfolio/bandit).

---

## 2. The kill-ladder (the spine)

Each rung is a cheap test designed to kill the thesis; passing only buys the next,
more expensive rung. Numbers are decisive figures; full citations in `docs/SUMMARY.md`.

| # | Stage | What it tested | Decisive number | Gate decision |
|---|---|---|---|---|
| 0 | **SQL** (Phases 1–2, BIRD text-to-SQL) | Is there horizon + a routable failure structure? | horizon ≈ **2 steps**; cheap_all **58.8%** vs strong 59.6% at **−89% cost**; **0** repair firings → all misses clean-but-wrong (semantic) | **KILL SQL** — no sequential structure for an MDP |
| 1 | **Code smoke** (Stage 2, SWE-bench-Live, N=5) | Long horizon? Real strong>cheap spread? | horizon **10–30** (med 22); strong **1/5** vs cheap **0/5**; **0** clean-but-wrong failures | **PASS** de-risk gate → continue |
| 2 | **Code viability** (Stages 3–4, N=8 then N=22 **post-cutoff**) | Does a *routable* spread hold at larger N, uncontaminated? | spread **5/22**; execution-survival (routable) fraction **60%** (pooled **62.5%**); strong ⊇ cheap, **0** violations; 0 resolves ≤4 steps | **PASS** → justifies the moat test |
| 3 | **Escalate sim** (Stage 5, $0 offline) | Does reactive escalate already capture the savings, leaving the MDP no room? | resolve ceiling **14/35** (trigger-independent); oracle saving over always-strong = **$1.29 = 4.6%**; escalate-late is **dominated** | MDP anticipation headroom **THIN** |
| 4 | **Diversity probe** (Stage 1A, out-of-family) | Does out-of-family break the strong⊇cheap nesting / lift the ceiling? | nesting break **1/35**; union ceiling **15/35 (43%)** = +1 task; cost-substitution oracle **≈69%** saving (winner varies per task) | ceiling lift **≈nil**; value is **portfolio**, not MDP |
| 5 | **Mixed-trajectory** (Stage 6, direct splice) | Does early-step authorship actually propagate (true sequential coupling)? | rescue-beyond-strong **0**; sabotage **0**; k-flips **0**; cost **−7.7%** @early, **+41.6%** @mid | coupling **WEAK** → **thesis CLOSED** |

<!-- TODO(prose): a sentence or two framing this table as the centerpiece — that
each rung was pre-committed to be able to KILL the thesis, and that the project's
value is the discipline of the ladder, not a positive result. -->

---

## 3. Headline result

- The distinctly **sequential/MDP** value **over always-strong** is **≤ 4.6%**
  (oracle ceiling, Stage 5) and **over reactive escalate-on-failure is ≈ nil**:
  the direct mixed-trajectory splice (Stage 6) shows **0** rescues beyond
  always-strong, **0** sabotage, **0** outcome-flips with handoff position, and a
  cost that is *negative* (handoff is **+41.6%** more expensive when it inherits
  more cheap context). [`docs/SUMMARY.md` §2, §4]
- **Why:** on these domains `strong` resolves a **strict superset** of `cheap`
  (0/35 violations), so no cheap-using policy resolves more, and `strong` is
  robust to whatever `cheap` accumulates — the outcome is set by *who does the
  hard later steps*, not by early authorship. [`docs/SUMMARY.md` §2]
- **What real value remains:** per-prompt **cost substitution** — a cheaper
  (often out-of-family) model co-solves a task strong also solves, giving a
  **≈69%** cost-substitution oracle (Stage 1A). That is a **bandit/portfolio**
  problem (commodity, copyable), **not** the sequential anticipation an MDP would
  add. [`docs/SUMMARY.md` §3]

<!-- TODO(prose): your framing of "honestly closed" — what would have changed the
verdict, and the one residual the offline data couldn't rule out before Stage 6
(positive transfer), which Stage 6 then closed as net-negative. -->

---

## 4. Methodology / reusable infrastructure

What transfers to a next project (all in this repo):

- **Tracer** (`tracer.py`) — SQLite trajectory/step logger with per-step
  model/effort/cost/token rollups and `retry_of_step_id` repair chains; one
  trajectory row per run, `action_model` per step so a mid-task model switch is
  visible. Phase-isolated DBs to prevent aggregate pollution.
- **$0-validatable harness** — agent loop driven by our own `OpenAIClient` +
  `config.cost_usd` (not litellm; prices post-cutoff models and stays mockable).
  `MockClient` enables full wiring validation with **no network/API key**
  (`validate_baseline.py`, `python run_phase3.py mock`).
- **Shadow / counterfactual arms** (`agent/shadow.py`) — per-step "could cheap
  have done this?" labels logged without acting (SQL phase).
- **Cost-scoping & budget guards** — persisted spend ledgers with soft/hard
  breakers and per-task caps that survive across run halts.
- **Cascade / composition decomposition** — separating "cheap fails because a
  step diverged" from "cheap fails on flat capability," and the escalate-vs-oracle
  vs always-strong policy comparison computed offline from logged step checks.
- **Contamination handling** — post-training-cutoff task windows (2025-06..08),
  plus a short-resolve (≤4 step) contamination flag.
- **Out-of-family probe** — additive multi-provider wiring (OpenRouter base_url +
  `.env` key reader + `config.PRICES`) leaving the OpenAI paths untouched.
- **Direct mixed-trajectory splicing** (`phase3_handoff.py`) — true cheap→strong
  handoff carrying real on-disk + conversation state, mock-validated at $0.

<!-- TODO(prose, optional): which one or two of these you'd most reuse and why. -->

---

## 5. What I'd do differently

<!-- TODO(prose): YOUR argument. Note to self to write up:
  Vertical selection for cost-routing should key on ASYMMETRIC ERROR COSTS, not
  horizon length. SQL and code agents both had the property that strong⊇cheap and
  failures were either clean-but-wrong (no trigger) or recoverable by reaction —
  so the MDP's anticipation had nothing to price. A domain where a wrong cheap
  action is expensive/irreversible (asymmetric error cost) is where anticipatory
  routing could actually pay, regardless of horizon. Reference: Amin 2026.
  -> add the citation and your reasoning here. -->
**TODO — argue that vertical selection should key on asymmetric error costs, not
horizon length (ref. Amin 2026).**

---

## 6. How to reproduce

```bash
# 1. Environment (Python 3.10)
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. $0 — offline, no API key, no model calls (reproduces the load-bearing analysis)
python validate_baseline.py     # mock-validates the tracer/agent wiring ($0)
python run_phase3.py mock        # $0 dry-run of the code-agent loop
python analyze_oof.py            # Stage 1A: resolve matrix, nesting break, cost-substitution (reads traces_phase3.sqlite)
python analyze_stage6.py         # Stage 6: rescue/sabotage/neutral, k-invariance, cost (reads phase3_*results*.json)

# 3. Live runs (COST MONEY — need keys in .env: OPENAI_API_KEY, and OPENROUTER_API_KEY for the OOF probe)
#    Task selection (needs `datasets`); the chosen-task manifests are already committed:
python phase3_tasks.py select          # / select_light
#    Solo arms (full N=22 split shown):
python run_phase3.py cheap_full        # gpt-4.1-mini
python run_phase3.py strong_full       # gpt-5.4
python run_phase3.py oof                # out-of-family probe (OpenRouter)
#    Stage 6 mixed-trajectory:
python phase3_handoff.py plan          # show the handoff plan
python phase3_handoff.py run           # execute splices (writes traces_phase3_stage6.sqlite)
```

<!-- TODO(prose, optional): note that the SQL phase (archive/) needs the BIRD dev
set (set BIRD_DEV_ROOT) and its trace DBs, which are .gitignored / not shipped. -->
Notes: the committed trace DBs + results JSON make steps (2) reproducible with no
spend. The SQL phase lives in [`archive/`](archive/) (historical; needs the BIRD
dev set and the .gitignored SQL trace DBs). See [`docs/`](docs/) for the full
stage write-ups (`FINDINGS.md`, `FINDINGS_PHASE3.md`) and `SUMMARY.md`.

---

## 7. Cost & scope footnote

- **OpenAI Phase-3 total ≈ $40.6** (`phase3_spend.json` $32.75 + `phase3_stage6_spend.json` $7.89).
- Out-of-family probe (OpenRouter, `phase3_oof_spend.json`): **$17.50** accounting
  estimate; **real charge ≈ $11** (list rates run ~1.6× hot; the $15 key cap was
  the true backstop). **Grand total ≈ $51.6** (real) to **$58.2** (estimate).
- Wall-clock ≈ **13 h** cumulative agent runtime across the Phase-3 trajectories
  (spread over ~48 h calendar, 2026-06-05 → 06-07).
- Scope guardrails held throughout: **no** q-classifier / bandit / oracle policy
  was trained; this is a measurement + kill-ladder study, not a routing product.
  Per-figure citations: [`docs/SUMMARY.md`](docs/SUMMARY.md).
