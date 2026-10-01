"""SQLite 存储层：建表与连接。所有写操作在 BEGIN IMMEDIATE 事务中完成。"""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS referrals (
    referral_id TEXT PRIMARY KEY,
    employee_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL,
    current_stint_id INTEGER,
    has_paid_out INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stints (
    stint_id INTEGER PRIMARY KEY AUTOINCREMENT,
    referral_id TEXT NOT NULL,
    hired_at TEXT NOT NULL,
    unhired_at TEXT,
    active INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bonuses (
    bonus_id TEXT PRIMARY KEY,
    referral_id TEXT NOT NULL,
    stint_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    paid_at TEXT,
    cancelled_at TEXT
);

CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    args_json TEXT NOT NULL,
    result_json TEXT NOT NULL
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path, timeout=30.0, isolation_level=None, check_same_thread=False
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn
