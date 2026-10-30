"""One private SQLite file owns reservations, results and uncertain calls."""

import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal, ROUND_CEILING
from pathlib import Path


class BudgetError(RuntimeError):
    pass


class UncertainCall(RuntimeError):
    pass


class Checkpoints:
    def __init__(self, path: Path, limit="20"):
        self.path = path
        self.limit = self.micros(limit)
        if not 0 < self.limit <= 20_000_000:
            raise ValueError("Experiment budget must be positive and no greater than 20 yuan")
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS calls (key TEXT PRIMARY KEY, kind TEXT, state TEXT, reserve INTEGER, task_id TEXT, result TEXT, seconds REAL DEFAULT 0)")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
            row = db.execute("SELECT value FROM metadata WHERE key='limit'").fetchone()
            if row and int(row[0]) != self.limit:
                raise BudgetError("Cannot change the budget of an existing experiment")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('limit',?)", (str(self.limit),))

    @staticmethod
    def micros(value):
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            raise ValueError("Cost must be finite and nonnegative")
        return int((number * 1_000_000).to_integral_value(rounding=ROUND_CEILING))

    @contextmanager
    def connection(self):
        with sqlite3.connect(self.path, timeout=20) as db:
            db.row_factory = sqlite3.Row
            yield db

    def get(self, key):
        with self.connection() as db:
            row = db.execute("SELECT * FROM calls WHERE key=?", (key,)).fetchone()
            return dict(row) if row else None

    def reserve(self, key, kind, upper_yuan):
        value = self.micros(upper_yuan)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM calls WHERE key=?", (key,)).fetchone()
            if row:
                return False
            spent = db.execute("SELECT coalesce(sum(reserve),0) FROM calls").fetchone()[0]
            if spent + value > self.limit:
                raise BudgetError("Next call exceeds the 20-yuan experiment budget")
            db.execute("INSERT INTO calls(key,kind,state,reserve) VALUES (?,?,?,?)", (key, kind, "started", value))
        return True

    def update(self, key, **changes):
        if not changes or not set(changes) <= {"state", "task_id", "result", "seconds"}:
            raise ValueError("Invalid checkpoint fields")
        with self.connection() as db:
            db.execute("UPDATE calls SET " + ",".join(f"{field}=?" for field in changes) + " WHERE key=?",
                       (*changes.values(), key))

    def finish(self, key, value, seconds):
        self.update(key, state="complete", result=json.dumps(value, ensure_ascii=False), seconds=seconds)

    def summary(self):
        with self.connection() as db:
            rows = [dict(row) for row in db.execute("SELECT key,kind,state,reserve,seconds FROM calls")]
        return {"limit_yuan": self.limit / 1_000_000,
                "reserved_upper_yuan": sum(r["reserve"] for r in rows) / 1_000_000,
                "calls": rows, "actual_cloud_bill": "not_reconciled"}
