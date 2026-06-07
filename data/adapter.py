"""Load a benchmark dev set into Task objects.

Paths come from config (config.BIRD_DEV_ROOT / config.SPIDER_DEV_ROOT) — never
hardcoded here. Supports 'bird' and 'spider' via the benchmark argument.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import config

BENCHMARKS = ("bird", "spider")


@dataclass(frozen=True)
class Task:
    task_id: str           # BIRD: str(question_id); Spider: str(list index)
    db_id: str
    question: str
    gold_sql: str
    evidence: str | None   # BIRD only; None for Spider
    db_path: str           # absolute path to <db_id>.sqlite
    benchmark: str         # 'bird' | 'spider'
    difficulty: str | None = None  # BIRD: simple|moderate|challenging; None for Spider


def _bird_db_path(root: Path, db_id: str) -> Path:
    """Resolve <db_id>.sqlite under the BIRD dev tree.

    The released dev_databases.zip extracts with a redundant nesting on some
    systems (dev_databases/dev_databases/<db_id>/...) alongside __MACOSX /
    .DS_Store junk, so probe the nested layout first, then the flat one.
    """
    bases = [
        root / "dev_databases" / "dev_databases",
        root / "dev_databases",
    ]
    for base in bases:
        cand = base / db_id / f"{db_id}.sqlite"
        if cand.is_file():
            return cand
    raise FileNotFoundError(
        f"BIRD sqlite for db_id={db_id!r} not found. Looked under: "
        + ", ".join(str(b / db_id / f'{db_id}.sqlite') for b in bases)
    )


def _load_bird() -> Iterator[Task]:
    root = config.BIRD_DEV_ROOT
    dev_json = root / "dev.json"
    if not dev_json.is_file():
        raise FileNotFoundError(f"BIRD dev.json not found at {dev_json}")
    with open(dev_json, encoding="utf-8") as f:
        records = json.load(f)
    for rec in records:
        db_id = rec["db_id"]
        yield Task(
            task_id=str(rec["question_id"]),
            db_id=db_id,
            question=rec["question"],
            gold_sql=rec["SQL"],
            evidence=rec.get("evidence"),
            db_path=str(_bird_db_path(root, db_id)),
            benchmark="bird",
            difficulty=rec.get("difficulty"),
        )


def _load_spider() -> Iterator[Task]:
    root = config.SPIDER_DEV_ROOT
    dev_json = root / "dev.json"
    if not dev_json.is_file():
        raise FileNotFoundError(
            f"Spider dev.json not found at {dev_json}. "
            f"Set config.SPIDER_DEV_ROOT to your Spider dev directory."
        )
    with open(dev_json, encoding="utf-8") as f:
        records = json.load(f)
    for idx, rec in enumerate(records):
        db_id = rec["db_id"]
        yield Task(
            task_id=str(idx),
            db_id=db_id,
            question=rec["question"],
            gold_sql=rec["query"],
            evidence=None,
            db_path=str(root / "database" / db_id / f"{db_id}.sqlite"),
            benchmark="spider",
            difficulty=None,
        )


def load_tasks(benchmark: str) -> Iterator[Task]:
    if benchmark == "bird":
        return _load_bird()
    if benchmark == "spider":
        return _load_spider()
    raise ValueError(f"benchmark must be one of {BENCHMARKS}, got {benchmark!r}")
