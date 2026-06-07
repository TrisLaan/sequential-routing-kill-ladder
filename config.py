"""Model pricing and path config for the text-to-SQL cost-opt harness."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
TRACE_DB_PATH = ROOT / "traces.sqlite"          # Phase 1 baseline_strong only
# Phase 2 (shadow + counterfactual arms) writes to its OWN db so Phase-1 baseline
# rows can't pollute Phase-2 aggregates (e.g. the #1 savings denominator) or
# create duplicate-task_id baselines for the tasks both phases happen to touch.
PHASE2_DB_PATH = ROOT / "traces_phase2.sqlite"
# Phase 3 (code-agent smoke test) writes to its OWN db for the same isolation
# reason — phase-1/2 SQL aggregates can never be polluted by code-domain rows.
PHASE3_DB_PATH = ROOT / "traces_phase3.sqlite"

# Benchmark dev-set roots. Raw strings (r"...") so backslashes aren't read as
# escape sequences. adapter.py reads these — do not hardcode paths there.
# Point BIRD_DEV_ROOT at your local BIRD dev download (the directory that
# contains dev.json and database/); override via the BIRD_DEV_ROOT env var.
BIRD_DEV_ROOT = Path(os.environ.get("BIRD_DEV_ROOT", r"C:\path\to\bird\dev"))
# Set the real Spider dev path; the 'spider' benchmark won't load until this
# points at a directory containing dev.json and database/.
SPIDER_DEV_ROOT = Path(os.environ.get("SPIDER_DEV_ROOT", r"C:\path\to\spider\dev"))

# (input_per_1M, output_per_1M) in USD.
# OpenAI standard short-context, non-cached rates, verified 2026-06-03.
# Caveats this flat model ignores: long-context (>~272K input) is billed higher,
# and cached input / Batch / Flex tiers are cheaper. Revisit if the agent starts
# hitting those — see config.cost_usd().
PRICES: dict[str, tuple[float, float]] = {
    "gpt-5.4": (2.50, 15.0),       # strong
    "gpt-4.1-mini": (0.40, 1.60),  # cheap
    # Out-of-family probe via OpenRouter (model-diversity / nesting-break test).
    # Rates are OpenRouter list prices fetched live 2026-06-06; the underlying
    # routed provider can differ slightly, so cost_usd() here is our ACCOUNTING
    # estimate — the real hard backstop is the $15 cap set on the OpenRouter key.
    "qwen/qwen3-coder": (0.22, 1.80),       # Qwen / Alibaba
    "deepseek/deepseek-v3.2": (0.229, 0.343),  # DeepSeek
    "z-ai/glm-4.6": (0.43, 1.74),           # Zhipu
}


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """USD cost for one call. Unknown model -> loud warning + 0.0.

    Returning 0 silently would erase any 'savings' number downstream, so the
    warning is intentionally noisy.
    """
    price = PRICES.get(model)
    if price is None:
        print(
            f"WARNING: unknown model {model!r} in cost_usd() — "
            f"returning 0.0. Add it to config.PRICES.",
            file=sys.stderr,
        )
        return 0.0
    in_per_1m, out_per_1m = price
    return (prompt_tokens / 1_000_000) * in_per_1m + (
        completion_tokens / 1_000_000
    ) * out_per_1m
