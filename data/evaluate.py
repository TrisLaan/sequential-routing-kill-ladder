"""Execution-match comparator: does a predicted result set match gold?

Faithful to each benchmark's official released eval script (semantics differ,
so we keep them separate rather than blending):

  BIRD   — bird-bench/mini_dev, evaluation/evaluation_ex.py :: calculate_ex
           `set(predicted_res) == set(ground_truth_res)`
           Pure SET comparison: row order is ignored *even when gold has
           ORDER BY*, and duplicate rows collapse.
           src: https://raw.githubusercontent.com/bird-bench/mini_dev/main/evaluation/evaluation_ex.py

  SPIDER — taoyds/test-suite-sql-eval, exec_eval.py :: result_eq
           `order_matters = 'order by' in g_str.lower()`, then bag/multiset
           comparison (sequence comparison when order matters).
           src: https://raw.githubusercontent.com/taoyds/test-suite-sql-eval/master/exec_eval.py
           DEFERRED (not yet ported): the official test-suite also allows
           column-permutation invariance and approximate float matching. Until
           the Spider path is wired and exercised, Spider here requires exact
           column order and exact values. Revisit when 'spider' goes live.
"""
from __future__ import annotations

from collections import Counter


def _as_tuples(rows) -> list[tuple]:
    return [tuple(r) for r in rows]


def execution_match(
    gold_rows,
    pred_rows,
    gold_sql: str,
    benchmark: str,
) -> bool:
    """True iff pred_rows matches gold_rows under `benchmark`'s EX semantics.

    A None result set (i.e. the query errored in the executor) never matches.
    """
    if gold_rows is None or pred_rows is None:
        return False
    gold = _as_tuples(gold_rows)
    pred = _as_tuples(pred_rows)

    if benchmark == "bird":
        return set(gold) == set(pred)

    if benchmark == "spider":
        if "order by" in gold_sql.lower():
            return gold == pred                 # order-sensitive sequence compare
        return Counter(gold) == Counter(pred)   # order-insensitive bag compare

    raise ValueError(f"unknown benchmark {benchmark!r}")
