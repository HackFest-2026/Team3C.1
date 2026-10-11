import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).with_name("mining_safety.db")


def get_connection():
    """Create a database connection."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_database():
    """Create the required database tables."""

    with get_connection() as conn:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                device_id TEXT NOT NULL,
                mq2_raw INTEGER NOT NULL,
                mq7_raw INTEGER NOT NULL,
                mq2_slope REAL DEFAULT 0,
                mq7_slope REAL DEFAULT 0,
                mq2_anomaly INTEGER DEFAULT 0,
                mq7_anomaly INTEGER DEFAULT 0
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                device_id TEXT NOT NULL,
                sensor TEXT NOT NULL,
                level TEXT NOT NULL,
                reason TEXT NOT NULL,
                raw_reading INTEGER,
                acknowledged INTEGER DEFAULT 0
            )
        """)

        conn.commit()


def save_reading(
    timestamp,
    device_id,
    mq2_raw,
    mq7_raw,
    mq2_slope=0,
    mq7_slope=0,
    mq2_anomaly=False,
    mq7_anomaly=False
):
    """Save one sensor reading."""

    with get_connection() as conn:
        conn.execute("""
            INSERT INTO readings (
                timestamp,
                device_id,
                mq2_raw,
                mq7_raw,
                mq2_slope,
                mq7_slope,
                mq2_anomaly,
                mq7_anomaly
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            timestamp,
            device_id,
            mq2_raw,
            mq7_raw,
            mq2_slope,
            mq7_slope,
            int(mq2_anomaly),
            int(mq7_anomaly)
        ))

        conn.commit()


def save_alert(
    timestamp,
    device_id,
    sensor,
    level,
    reason,
    raw_reading=None
):
    """Save an alert to the database."""

    with get_connection() as conn:
        conn.execute("""
            INSERT INTO alerts (
                timestamp,
                device_id,
                sensor,
                level,
                reason,
                raw_reading
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            timestamp,
            device_id,
            sensor,
            level,
            reason,
            raw_reading
        ))

        conn.commit()


def get_recent_readings(limit=500):
    """Retrieve recent readings."""

    with get_connection() as conn:
        rows = conn.execute("""
            SELECT *
            FROM readings
            ORDER BY id DESC
            LIMIT ?
        """, (limit,)).fetchall()

    return [dict(row) for row in reversed(rows)]


def get_recent_alerts(limit=100):
    """Retrieve recent alerts."""

    with get_connection() as conn:
        rows = conn.execute("""
            SELECT *
            FROM alerts
            ORDER BY id DESC
            LIMIT ?
        """, (limit,)).fetchall()

    return [dict(row) for row in rows]


def initialize():
    initialize_database()


if __name__ == "__main__":
    initialize_database()
    print("Database initialized successfully!")
    print(f"Database location: {DB_PATH}")