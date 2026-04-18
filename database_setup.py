import os
import sqlite3
from datetime import datetime


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
DB_PATH = os.path.join(INSTANCE_DIR, "crash_game.db")


def utc_now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def setup_database():
    os.makedirs(INSTANCE_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            balance REAL NOT NULL DEFAULT 1000.0,
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS rounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nonce INTEGER NOT NULL,
            server_seed TEXT NOT NULL,
            seed_hash TEXT NOT NULL,
            crash_point REAL NOT NULL,
            phase TEXT NOT NULL,
            started_at TEXT NOT NULL,
            crashed_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            round_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            auto_cashout REAL,
            cashout_multiplier REAL,
            result TEXT NOT NULL,
            payout REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            settled_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (round_id) REFERENCES rounds(id) ON DELETE CASCADE
        )
        """
    )
    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_bets_unique_round_user
        ON bets(round_id, user_id)
        """
    )
    conn.commit()
    conn.close()
    print(f"Crash game database ready at {DB_PATH} ({utc_now()} UTC)")


if __name__ == "__main__":
    setup_database()
