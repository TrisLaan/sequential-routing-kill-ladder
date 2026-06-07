"""PROXY schema-linking labels for the schema_retrieval shadow.

We have no parse tree for the cheap model's free-text "relevant tables/columns"
output, so we approximate "does the cheap selection cover what's needed" by
identifier-vocabulary overlap: build the DB's real {tables, columns} vocabulary
from sqlite_master/PRAGMA, then treat any word in a text that hits that vocab as
a "referenced" identifier.

This is deliberately coarse and INFLATES superset rates — flagged everywhere it's
used. Known failure modes:
  * common-word column names ('name','id','type','year') match spuriously in the
    question echo or prose, so cheap looks like it "selected" them;
  * a column shared by several tables can't be attributed to one table, so we
    compare flat column-name sets, not (table,column) pairs;
  * gold/draft SQL identifiers are matched the same loose way (a column named in
    a string literal would count).
Hence the schema_retrieval component of any savings/coverage number is the most
inflated — always reported as an upper bound / proxy.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def schema_vocab(db_path: str) -> tuple[set[str], set[str]]:
    """Return (table-name set, column-name set), all lowercased, read-only."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.text_factory = lambda b: b.decode("utf-8", "replace")
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        cols: set[str] = set()
        for t in tables:
            # PRAGMA table_info needs the literal name; quote to be safe.
            for r in conn.execute(f'PRAGMA table_info("{t}")'):
                cols.add(str(r[1]).lower())
        return {t.lower() for t in tables}, cols
    finally:
        conn.close()


def referenced(text: str | None, tvocab: set[str], cvocab: set[str]) -> tuple[set[str], set[str]]:
    """Identifiers in `text` that hit the table/column vocab (lowercased)."""
    toks = {m.group(0).lower() for m in _IDENT.finditer(text or "")}
    return toks & tvocab, toks & cvocab


def is_superset(
    selection_text: str | None,
    target_sql: str | None,
    tvocab: set[str],
    cvocab: set[str],
) -> bool:
    """True iff `selection_text` references every table+column `target_sql` does.

    PROXY: see module docstring. Empty target (no recognized identifiers) is a
    vacuous superset (True) — rare, and noted as a proxy artifact.
    """
    sel_t, sel_c = referenced(selection_text, tvocab, cvocab)
    tgt_t, tgt_c = referenced(target_sql, tvocab, cvocab)
    return tgt_t <= sel_t and tgt_c <= sel_c
