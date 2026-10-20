"""Additive login-admission indexes; preserve the legacy two-column IP table."""
from __future__ import annotations


def migrate(db):
    db.execute("CREATE TABLE IF NOT EXISTS login_account_attempts (username_hash TEXT NOT NULL, at REAL NOT NULL)")
    db.execute("CREATE INDEX IF NOT EXISTS login_account_attempts_subject ON login_account_attempts(username_hash,at)")
    db.execute("CREATE INDEX IF NOT EXISTS login_account_attempts_expiry ON login_account_attempts(at)")
    db.execute("CREATE INDEX IF NOT EXISTS login_attempts_ip_time ON login_attempts(ip,at)")
    db.execute("CREATE INDEX IF NOT EXISTS login_attempts_expiry ON login_attempts(at)")
