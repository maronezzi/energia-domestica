#!/usr/bin/env python3
"""Check which DB tuya_dashboard is using and its status."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent if '__file__' in dir() else Path("/home/bruno/.openclaw/workspace")
DB_FILE = BASE_DIR / "tuya_history.db"

print(f"DB path: {DB_FILE}")
print(f"DB exists: {DB_FILE.exists()}")
print(f"DB size: {os.path.getsize(DB_FILE) if DB_FILE.exists() else 0} bytes")

if DB_FILE.exists() and os.path.getsize(DB_FILE) > 0:
    import sqlite3
    conn = sqlite3.connect(str(DB_FILE))
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    print(f"Tables: {[t[0] for t in tables]}")
    if ('readings',) in [(t[0],) for t in tables]:
        cnt = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        last = conn.execute("SELECT MAX(timestamp) FROM readings").fetchone()[0]
        print(f"Readings: {cnt:,}")
        print(f"Last reading: {last}")
    conn.close()