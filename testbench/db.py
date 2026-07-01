"""SQLite persistence for comparison tests — the Test Journal backing store.

Ported pattern from frontera_ml/persistence/detections_db.py: a small class
(not module globals) wrapping sqlite3 with a lock, so concurrent HTTP request
threads never corrupt the file.
"""
from __future__ import annotations

import sqlite3
import threading
from typing import List, Optional

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS comparisons (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc       TEXT    NOT NULL,
    start_a      REAL, stop_a REAL, step_a REAL,
    start_b      REAL, stop_b REAL, step_b REAL,
    conditions_a TEXT,  conditions_b TEXT,
    npy_a        TEXT,  npy_b TEXT,
    png_a        TEXT,  png_b TEXT,
    peak_dbm_a   REAL,  peak_freq_a REAL,
    peak_dbm_b   REAL,  peak_freq_b REAL,
    n_points_a   INTEGER, n_points_b INTEGER,
    pdf_path     TEXT
);
"""


class JournalDB:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        with self._connect() as con:
            con.execute(_CREATE_SQL)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, check_same_thread=False)
        con.row_factory = sqlite3.Row
        return con

    def insert(self, record: dict) -> int:
        cols = list(record.keys())
        placeholders = ",".join("?" for _ in cols)
        with self._lock, self._connect() as con:
            cur = con.execute(
                f"INSERT INTO comparisons ({','.join(cols)}) VALUES ({placeholders})",
                [record[c] for c in cols],
            )
            return cur.lastrowid

    def recent(self, limit: int = 200) -> List[dict]:
        with self._lock, self._connect() as con:
            rows = con.execute(
                "SELECT * FROM comparisons ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get(self, comparison_id: int) -> Optional[dict]:
        with self._lock, self._connect() as con:
            row = con.execute(
                "SELECT * FROM comparisons WHERE id=?", (comparison_id,)
            ).fetchone()
        return dict(row) if row else None

    def delete(self, ids: List[int]) -> List[dict]:
        """Delete rows by id. Returns the deleted rows so the caller can also
        remove their associated files on disk."""
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._lock, self._connect() as con:
            rows = con.execute(
                f"SELECT * FROM comparisons WHERE id IN ({placeholders})", ids
            ).fetchall()
            con.execute(f"DELETE FROM comparisons WHERE id IN ({placeholders})", ids)
        return [dict(r) for r in rows]
