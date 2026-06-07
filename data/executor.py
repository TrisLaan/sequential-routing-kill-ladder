"""Run a SQL string against a task's SQLite DB.

Contract: open the DB READ-ONLY, enforce a wall-clock query timeout, and NEVER
raise on bad SQL — capture the DB error message as a string instead.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

DEFAULT_TIMEOUT_S = 10.0


def run_sql(
    db_path: str,
    sql: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> tuple[list[tuple] | None, str | None]:
    """Execute `sql` against the SQLite DB at `db_path`.

    Returns (rows, error):
      * success -> (list_of_row_tuples, None)   — empty result is ([], None)
      * failure -> (None, error_message_string)
    Failure covers SQL/DB errors and timeouts; this function never raises.
    """
    # Read-only via URI. as_uri() emits file:///C:/... and percent-encodes
    # spaces; appending the query string yields ...?mode=ro for uri=True.
    db_uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = None
    try:
        conn = sqlite3.connect(db_uri, uri=True)
        # BIRD ships occasional non-UTF-8 text that crashes the default
        # str text_factory on fetch; decode leniently. Both sides of any
        # comparison use the same factory, so matching is unaffected.
        conn.text_factory = lambda b: b.decode("utf-8", "replace")
        # stdlib sqlite3 has no per-query timeout (connect(timeout=) is for
        # lock contention, not execution time). The progress handler fires
        # every N VM ops; returning non-zero aborts the query, which surfaces
        # as OperationalError('interrupted'). This bounds runaway queries.
        deadline = time.monotonic() + timeout_s
        conn.set_progress_handler(
            lambda: 1 if time.monotonic() > deadline else 0, 1000
        )
        cur = conn.execute(sql)
        rows = cur.fetchall()
        return rows, None
    except sqlite3.Error as e:
        msg = str(e)
        if "interrupted" in msg.lower():
            msg = f"query timeout after {timeout_s:g}s"
        return None, msg
    except Exception as e:  # defensive: contract says never raise to caller
        return None, f"{type(e).__name__}: {e}"
    finally:
        if conn is not None:
            conn.close()
