"""SQLite persistence helpers."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from flask import Flask, current_app, g


def database_path(app: Flask | None = None) -> str:
    if app is not None:
        return app.config["DATABASE_PATH"]
    return os.environ.get(
        "DATABASE_PATH",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "verification.db"),
    )


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        try:
            path = current_app.config["DATABASE_PATH"]
        except RuntimeError:
            path = database_path()
        parent = os.path.dirname(os.path.abspath(path))
        if parent and path != ":memory:":
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(_error: BaseException | None = None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db() -> None:
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS cases (
            id TEXT PRIMARY KEY,
            box_code TEXT NOT NULL,
            warehouse_filename TEXT,
            qc_filename TEXT,
            raw_warehouse_csv TEXT,
            raw_qc_csv TEXT,
            current_verdict TEXT,
            current_conclusion_id INTEGER,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id TEXT NOT NULL,
            uid TEXT NOT NULL,
            table_name TEXT NOT NULL,
            record_no TEXT NOT NULL,
            minute_key TEXT NOT NULL,
            minute_text TEXT NOT NULL,
            row_no INTEGER NOT NULL,
            action_raw TEXT NOT NULL,
            kind TEXT NOT NULL,
            UNIQUE(case_id, uid),
            UNIQUE(case_id, table_name, row_no),
            FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_records_case ON records(case_id);

        CREATE TABLE IF NOT EXISTS supplements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id TEXT NOT NULL,
            before_uid TEXT NOT NULL,
            after_uid TEXT NOT NULL,
            before_table TEXT NOT NULL,
            before_record_no TEXT NOT NULL,
            after_table TEXT NOT NULL,
            after_record_no TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(case_id, before_uid, after_uid),
            FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_supplements_case ON supplements(case_id);

        CREATE TABLE IF NOT EXISTS conclusions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id TEXT NOT NULL,
            verdict TEXT NOT NULL,
            compliant_possible INTEGER NOT NULL,
            noncompliant_possible INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            trigger_type TEXT NOT NULL DEFAULT 'supplement',
            created_at TEXT NOT NULL,
            FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_conclusions_case ON conclusions(case_id);
        """
    )
    db.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def loads_conclusion(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row) if not isinstance(row, dict) else row
    payload = json.loads(data["payload_json"])
    payload["id"] = data["id"]
    payload["case_id"] = data["case_id"]
    payload["trigger_type"] = data.get("trigger_type")
    payload["created_at"] = data.get("created_at")
    return payload
