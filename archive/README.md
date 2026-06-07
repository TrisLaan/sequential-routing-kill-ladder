# archive/ — SQL-phase scratch (historical, not maintained)

This directory holds the **Phase 1–2 text-to-SQL** code that preceded the
Phase-3 code-agent study. The SQL thesis was killed (see `docs/FINDINGS.md` and
the "SQL → code-agents" row in `docs/SUMMARY.md` / the top-level README
kill-ladder); these scripts are kept only for provenance.

**These files are NOT import-path-safe from this folder.** The whole harness was
written as a flat layout that runs from the repository root (`import config`,
`import phase3_repo as R`, etc. resolve against the repo root, and every data
artifact is anchored to `config.ROOT / "<filename>"`). To run any of these you
would execute them *from the repo root*, e.g. `python archive/analyze_phase2.py`
— and they additionally require the Phase-1/2 trace DBs (`traces.sqlite`,
`traces_phase2.sqlite`), which are **.gitignored** and therefore not shipped in
the public repo. The load-bearing SQL numbers they produced are recorded in
`docs/FINDINGS.md` and `docs/SUMMARY.md`.

Contents:
- `run_baseline.py`   — Phase 1 baseline_strong runner (gpt-5.4 every step). Needs an API key.
- `run_phase2.py`     — Phase 2 shadow + counterfactual-arms runner (N=500). Needs an API key.
- `validate_phase2.py`— Phase 2 mock validation (imports `analyze_phase2`).
- `analyze_phase2.py` — Phase 2 deliverables #1–#7 (reads `traces_phase2.sqlite`).
- `run_smoke.py`      — early tracer smoke/demo.
- `phase2_sample.json`— Phase 2 seeded N=500 task manifest (seed=20240627).
