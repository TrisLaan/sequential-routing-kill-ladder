"""SQLite logging layer: trajectory -> step -> shadow.

Single-writer design: one TraceLogger instance owns one sqlite3 connection and
commits after every write. Safe for a serial agent loop and the smoke test;
NOT safe for concurrent writers (no WAL, no connection pool, no locking).
If we ever parallelize the agent, switch to one logger per worker writing to
separate DB files and merge offline.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

import config

_BENCHMARKS = ("bird", "spider", "swebench_live")
_POLICIES = ("baseline_strong", "shadow", "advisory", "active",
             "cheap_only", "strong_only")
_DECISIONS = ("schema_retrieval", "draft_sql", "repair", "finalize",
              "agent_step", "submit")
# Phase 2: counterfactual-arm discriminator on trajectory. Nullable, validated
# Python-side (no SQL CHECK) so the fresh CREATE and the ALTER migration agree.
# NULL = baseline_strong (and all pre-Phase-2 rows); the two values below are the
# policy='shadow' counterfactual trajectories.
_ARMS = (None, "cheap_all", "cheap_no_schema")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS trajectory (
  trajectory_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id          TEXT NOT NULL,
  db_id            TEXT NOT NULL,
  benchmark        TEXT NOT NULL CHECK (benchmark IN ('bird','spider','swebench_live')),
  policy_label     TEXT NOT NULL CHECK (policy_label IN
                     ('baseline_strong','shadow','advisory','active',
                      'cheap_only','strong_only')),
  gold_sql         TEXT,
  final_pred_sql   TEXT,
  final_success    INTEGER,
  success_method   TEXT,
  total_cost_usd   REAL,
  total_tok_in     INTEGER,
  total_tok_out    INTEGER,
  num_steps        INTEGER,
  started_at       TEXT NOT NULL,
  finished_at      TEXT,
  notes            TEXT,
  arm              TEXT   -- Phase 2 counterfactual arm; NULL for baseline_strong
);

CREATE TABLE IF NOT EXISTS step (
  step_id           INTEGER PRIMARY KEY AUTOINCREMENT,
  trajectory_id     INTEGER NOT NULL REFERENCES trajectory(trajectory_id),
  step_index        INTEGER NOT NULL,
  decision_type     TEXT NOT NULL CHECK (decision_type IN
                      ('schema_retrieval','draft_sql','repair','finalize',
                       'agent_step','submit')),
  state_features    TEXT,
  action_model      TEXT NOT NULL,
  action_effort     TEXT,
  prompt_tokens     INTEGER NOT NULL,
  completion_tokens INTEGER NOT NULL,
  cost_usd          REAL NOT NULL,
  latency_ms        INTEGER,
  output            TEXT,
  exec_error        TEXT,
  retry_of_step_id  INTEGER REFERENCES step(step_id)
);
CREATE INDEX IF NOT EXISTS idx_step_traj  ON step(trajectory_id);
CREATE INDEX IF NOT EXISTS idx_step_retry ON step(retry_of_step_id);

CREATE TABLE IF NOT EXISTS shadow (
  shadow_id         INTEGER PRIMARY KEY AUTOINCREMENT,
  step_id           INTEGER NOT NULL REFERENCES step(step_id),
  shadow_model      TEXT NOT NULL,
  shadow_output     TEXT,
  prompt_tokens     INTEGER NOT NULL,
  completion_tokens INTEGER NOT NULL,
  cost_usd          REAL NOT NULL,
  latency_ms        INTEGER,
  matched           INTEGER,
  match_method      TEXT,
  -- Phase 2 secondary labels (all nullable; populated per decision type).
  -- draft_sql/repair shadows set cheap_matches_gold + strong_matches_gold;
  -- schema_retrieval shadows set the two superset proxies. `matched` stays the
  -- primary boolean (cheap_matches_strong, or schema_superset_of_strong_draft).
  cheap_matches_gold              INTEGER,
  strong_matches_gold             INTEGER,
  schema_superset_of_gold         INTEGER,
  schema_superset_of_strong_draft INTEGER
);
CREATE INDEX IF NOT EXISTS idx_shadow_step ON shadow(step_id);
"""

# (table, [(column, decl), ...]) added after Phase 1. Applied idempotently on
# every connect so an existing traces.sqlite gains the columns without a rebuild.
_MIGRATIONS = (
    ("trajectory", [("arm", "TEXT")]),
    ("shadow", [
        ("cheap_matches_gold", "INTEGER"),
        ("strong_matches_gold", "INTEGER"),
        ("schema_superset_of_gold", "INTEGER"),
        ("schema_superset_of_strong_draft", "INTEGER"),
    ]),
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class TraceLogger:
    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add post-Phase-1 columns to a pre-existing DB. Idempotent."""
        for table, cols in _MIGRATIONS:
            have = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        self.conn.commit()

    def start_trajectory(
        self,
        *,
        task_id: str,
        db_id: str,
        benchmark: str,
        policy_label: str,
        gold_sql: str | None = None,
        notes: str | None = None,
        arm: str | None = None,
    ) -> int:
        if benchmark not in _BENCHMARKS:
            raise ValueError(f"benchmark must be one of {_BENCHMARKS}, got {benchmark!r}")
        if policy_label not in _POLICIES:
            raise ValueError(f"policy_label must be one of {_POLICIES}, got {policy_label!r}")
        if arm not in _ARMS:
            raise ValueError(f"arm must be one of {_ARMS}, got {arm!r}")
        cur = self.conn.execute(
            """
            INSERT INTO trajectory
              (task_id, db_id, benchmark, policy_label, gold_sql,
               started_at, notes, arm)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, db_id, benchmark, policy_label, gold_sql,
             _utcnow_iso(), notes, arm),
        )
        self.conn.commit()
        return cur.lastrowid

    def log_step(
        self,
        *,
        trajectory_id: int,
        step_index: int,
        decision_type: str,
        action_model: str,
        prompt_tokens: int,
        completion_tokens: int,
        state_features: dict[str, Any] | None = None,
        action_effort: str | None = None,
        latency_ms: int | None = None,
        output: str | None = None,
        exec_error: str | None = None,
        retry_of_step_id: int | None = None,
        cost_usd: float | None = None,
    ) -> int:
        if decision_type not in _DECISIONS:
            raise ValueError(f"decision_type must be one of {_DECISIONS}, got {decision_type!r}")
        if cost_usd is None:
            cost_usd = config.cost_usd(action_model, prompt_tokens, completion_tokens)
        sf_json = json.dumps(state_features) if state_features is not None else None
        cur = self.conn.execute(
            """
            INSERT INTO step
              (trajectory_id, step_index, decision_type, state_features,
               action_model, action_effort, prompt_tokens, completion_tokens,
               cost_usd, latency_ms, output, exec_error, retry_of_step_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (trajectory_id, step_index, decision_type, sf_json,
             action_model, action_effort, prompt_tokens, completion_tokens,
             cost_usd, latency_ms, output, exec_error, retry_of_step_id),
        )
        self.conn.commit()
        return cur.lastrowid

    def log_shadow(
        self,
        *,
        step_id: int,
        shadow_model: str,
        prompt_tokens: int,
        completion_tokens: int,
        shadow_output: str | None = None,
        latency_ms: int | None = None,
        matched: int | None = None,
        match_method: str | None = None,
        cost_usd: float | None = None,
        cheap_matches_gold: int | None = None,
        strong_matches_gold: int | None = None,
        schema_superset_of_gold: int | None = None,
        schema_superset_of_strong_draft: int | None = None,
    ) -> int:
        if cost_usd is None:
            cost_usd = config.cost_usd(shadow_model, prompt_tokens, completion_tokens)
        cur = self.conn.execute(
            """
            INSERT INTO shadow
              (step_id, shadow_model, shadow_output,
               prompt_tokens, completion_tokens, cost_usd,
               latency_ms, matched, match_method,
               cheap_matches_gold, strong_matches_gold,
               schema_superset_of_gold, schema_superset_of_strong_draft)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (step_id, shadow_model, shadow_output,
             prompt_tokens, completion_tokens, cost_usd,
             latency_ms, matched, match_method,
             cheap_matches_gold, strong_matches_gold,
             schema_superset_of_gold, schema_superset_of_strong_draft),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_trajectory(
        self,
        trajectory_id: int,
        *,
        final_pred_sql: str | None,
        final_success: int | None,
        success_method: str | None = None,
    ) -> None:
        # Aggregate from `step` only. Shadow rows are retained in the `shadow`
        # table but deliberately excluded from trajectory totals — they're
        # counterfactual measurements, not real spend on this trajectory.
        row = self.conn.execute(
            """
            SELECT COALESCE(SUM(cost_usd), 0.0),
                   COALESCE(SUM(prompt_tokens), 0),
                   COALESCE(SUM(completion_tokens), 0),
                   COUNT(*)
            FROM step
            WHERE trajectory_id = ?
            """,
            (trajectory_id,),
        ).fetchone()
        total_cost, tok_in, tok_out, num_steps = row
        self.conn.execute(
            """
            UPDATE trajectory
            SET final_pred_sql = ?,
                final_success  = ?,
                success_method = ?,
                total_cost_usd = ?,
                total_tok_in   = ?,
                total_tok_out  = ?,
                num_steps      = ?,
                finished_at    = ?
            WHERE trajectory_id = ?
            """,
            (final_pred_sql, final_success, success_method,
             total_cost, tok_in, tok_out, num_steps,
             _utcnow_iso(), trajectory_id),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
