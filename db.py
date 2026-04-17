import os
import sqlite3

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
SQLITE_PATH = os.environ.get("SQLITE_PATH", "betting_app.db")

try:
    import psycopg2
    HAS_POSTGRESQL = True
except ImportError:
    HAS_POSTGRESQL = False


class SQLiteCursorWrapper:
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, query, params=None):
        normalized_query = query.replace("%s", "?").replace("NOW()", "CURRENT_TIMESTAMP")
        if params is None:
            return self._cursor.execute(normalized_query)
        return self._cursor.execute(normalized_query, params)

    def executemany(self, query, seq_of_params):
        normalized_query = query.replace("%s", "?").replace("NOW()", "CURRENT_TIMESTAMP")
        return self._cursor.executemany(normalized_query, seq_of_params)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class SQLiteConnectionWrapper:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return SQLiteCursorWrapper(self._conn.cursor())

    def __getattr__(self, name):
        return getattr(self._conn, name)


def using_postgresql():
    return HAS_POSTGRESQL and bool(DATABASE_URL)


def _normalized_database_url():
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if "sslmode=" not in url:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}sslmode=require"
    return url


def get_db():
    if using_postgresql():
        return psycopg2.connect(_normalized_database_url())

    conn = sqlite3.connect(SQLITE_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return SQLiteConnectionWrapper(conn)


def _column_exists(cursor, table_name, column_name, is_sqlite):
    if is_sqlite:
        cursor.execute(f"PRAGMA table_info({table_name})")
        return any(row[1] == column_name for row in cursor.fetchall())

    cursor.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
        """,
        (table_name, column_name),
    )
    return cursor.fetchone() is not None


def init_db():
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        is_sqlite = not using_postgresql()

        if is_sqlite:
            c.execute(
                """CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                email TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                balance INTEGER DEFAULT 100
            )"""
            )

            c.execute(
                """CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                type TEXT NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT DEFAULT 'Pending',
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )"""
            )

            c.execute(
                """CREATE TABLE IF NOT EXISTS game_rooms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_type TEXT NOT NULL,
                status TEXT DEFAULT 'waiting',
                max_players INTEGER DEFAULT 10,
                bet_amount INTEGER DEFAULT 0,
                result TEXT DEFAULT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                ended_at DATETIME DEFAULT NULL,
                creator TEXT DEFAULT NULL
            )"""
            )

            c.execute(
                """CREATE TABLE IF NOT EXISTS game_players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id INTEGER,
                username TEXT NOT NULL,
                bet_amount INTEGER NOT NULL,
                choice TEXT DEFAULT NULL,
                payout INTEGER DEFAULT 0,
                result TEXT DEFAULT 'pending',
                joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (room_id) REFERENCES game_rooms(id)
            )"""
            )
        else:
            c.execute(
                """CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                email TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                balance INTEGER DEFAULT 100
            )"""
            )

            c.execute(
                """CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                username TEXT NOT NULL,
                type TEXT NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT DEFAULT 'Pending',
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
            )

            c.execute(
                """CREATE TABLE IF NOT EXISTS game_rooms (
                id SERIAL PRIMARY KEY,
                game_type TEXT NOT NULL,
                status TEXT DEFAULT 'waiting',
                max_players INTEGER DEFAULT 10,
                bet_amount INTEGER DEFAULT 0,
                result TEXT DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ended_at TIMESTAMP DEFAULT NULL,
                creator TEXT DEFAULT NULL
            )"""
            )

            c.execute(
                """CREATE TABLE IF NOT EXISTS game_players (
                id SERIAL PRIMARY KEY,
                room_id INTEGER REFERENCES game_rooms(id),
                username TEXT NOT NULL,
                bet_amount INTEGER NOT NULL,
                choice TEXT DEFAULT NULL,
                payout INTEGER DEFAULT 0,
                result TEXT DEFAULT 'pending',
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
            )

        if not _column_exists(c, "game_rooms", "creator", is_sqlite):
            c.execute("ALTER TABLE game_rooms ADD COLUMN creator TEXT DEFAULT NULL")

        if not is_sqlite:
            try:
                c.execute(
                    "ALTER TABLE users ADD CONSTRAINT balance_non_negative CHECK (balance >= 0)"
                )
            except Exception:
                conn.rollback()
                c = conn.cursor()

        conn.commit()
        conn.close()
        print(
            f"Database initialized successfully using {'PostgreSQL' if using_postgresql() else 'SQLite'}!"
        )
    except Exception as e:
        if conn is not None:
            conn.rollback()
            conn.close()
        print(f"Error initializing database: {e}")
        raise
