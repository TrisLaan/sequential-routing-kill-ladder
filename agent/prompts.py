"""Prompt builders for the baseline_strong loop (all gpt-5.4).

Three LLM decisions: schema_retrieval, draft_sql, repair. `finalize` is NOT an
LLM call (deterministic bookkeeping in baseline.py), so it has no builder here.

Schema linking is LLM-based: the FULL database DDL goes into the schema_retrieval
prompt and the model returns the relevant subset, which then feeds draft_sql.
"""
from __future__ import annotations

_SCHEMA_LINK_SYSTEM = (
    "You are an expert at schema linking for SQLite text-to-SQL. Given a "
    "database schema and a question, return only the tables and columns needed "
    "to answer it."
)

_SQL_WRITER_SYSTEM = (
    "You are an expert SQLite query writer. Output exactly one SQLite SELECT "
    "query and nothing else — no markdown, no commentary. Quote identifiers "
    "containing spaces with backticks."
)


def _evidence_line(evidence: str | None) -> str:
    return f"Evidence: {evidence}\n" if evidence else ""


def schema_link_messages(
    question: str, evidence: str | None, full_ddl: str
) -> list[dict[str, str]]:
    user = (
        f"Question: {question}\n"
        f"{_evidence_line(evidence)}"
        f"\nDatabase schema:\n{full_ddl}\n\n"
        "List each relevant table and its relevant columns. Be inclusive enough "
        "to answer the question; exclude unrelated tables."
    )
    return [
        {"role": "system", "content": _SCHEMA_LINK_SYSTEM},
        {"role": "user", "content": user},
    ]


def draft_messages(
    question: str, evidence: str | None, linked_schema: str
) -> list[dict[str, str]]:
    user = (
        f"Question: {question}\n"
        f"{_evidence_line(evidence)}"
        f"\nRelevant schema:\n{linked_schema}\n\n"
        "Write one SQLite query that answers the question."
    )
    return [
        {"role": "system", "content": _SQL_WRITER_SYSTEM},
        {"role": "user", "content": user},
    ]


def repair_messages(
    question: str,
    evidence: str | None,
    linked_schema: str,
    prev_sql: str,
    error: str,
) -> list[dict[str, str]]:
    user = (
        f"Question: {question}\n"
        f"{_evidence_line(evidence)}"
        f"\nRelevant schema:\n{linked_schema}\n\n"
        f"Previous query:\n{prev_sql}\n\n"
        f"It failed with:\n{error}\n\n"
        "Return the corrected SQLite query, one statement, no commentary."
    )
    return [
        {"role": "system", "content": _SQL_WRITER_SYSTEM},
        {"role": "user", "content": user},
    ]
