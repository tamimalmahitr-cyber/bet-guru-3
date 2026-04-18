import hashlib
import math
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime

from flask import Flask, render_template
from flask_socketio import SocketIO, emit, join_room


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
DB_PATH = os.path.join(INSTANCE_DIR, "crash_game.db")
GAME_ROOM = "crash-main-room"
STARTING_BALANCE = 1000.0
COUNTDOWN_SECONDS = 10
CRASH_HOLD_SECONDS = 3
TICK_RATE = 0.1
MAX_CRASH_POINT = 20.0
MAX_CONCURRENT_PLAYERS = 10

STATE_WAITING = "WAITING"
STATE_STARTING = "STARTING"
STATE_RUNNING = "RUNNING"
STATE_CRASHED = "CRASHED"


app = Flask(__name__)
app.config["SECRET_KEY"] = "crash-game-dev-secret"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

db_lock = threading.RLock()
user_socket_map = {}
socket_user_map = {}


def utc_now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db():
    os.makedirs(INSTANCE_DIR, exist_ok=True)
    with db_lock:
        conn = get_db()
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

        # Close unfinished rounds cleanly on boot so the new loop owns the next round.
        cur.execute(
            "UPDATE rounds SET phase = ? WHERE phase IN (?, ?)",
            (STATE_CRASHED, STATE_STARTING, STATE_RUNNING),
        )
        cur.execute(
            """
            UPDATE bets
            SET result = 'lost',
                payout = 0,
                settled_at = ?
            WHERE result = 'pending'
            """,
            (utc_now(),),
        )
        conn.commit()
        conn.close()


def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def format_money(value):
    return round(float(value), 2)


def get_user_by_id(user_id):
    with db_lock:
        conn = get_db()
        row = conn.execute(
            "SELECT id, username, balance FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        conn.close()
    return row


def get_or_create_user(username):
    cleaned = (username or "").strip()[:20]
    if not cleaned:
        return None, "Username is required."
    with db_lock:
        conn = get_db()
        row = conn.execute(
            "SELECT id, username, balance FROM users WHERE lower(username) = lower(?)",
            (cleaned,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (username, balance, created_at) VALUES (?, ?, ?)",
                (cleaned, STARTING_BALANCE, utc_now()),
            )
            conn.commit()
            row = conn.execute(
                "SELECT id, username, balance FROM users WHERE lower(username) = lower(?)",
                (cleaned,),
            ).fetchone()
        conn.close()
    return row, None


def adjust_user_balance(user_id, delta):
    with db_lock:
        conn = get_db()
        row = conn.execute(
            "SELECT id, username, balance FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            conn.close()
            return None, "User not found."
        new_balance = format_money(row["balance"] + delta)
        if new_balance < 0:
            conn.close()
            return None, "Insufficient balance."
        conn.execute(
            "UPDATE users SET balance = ? WHERE id = ?",
            (new_balance, user_id),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT id, username, balance FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        conn.close()
    return updated, None


def fetch_round_history(limit=10):
    with db_lock:
        conn = get_db()
        rows = conn.execute(
            """
            SELECT id, crash_point, seed_hash, started_at, crashed_at
            FROM rounds
            WHERE phase = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (STATE_CRASHED, limit),
        ).fetchall()
        conn.close()
    return [
        {
            "id": row["id"],
            "crash_point": round(row["crash_point"], 2),
            "seed_hash": row["seed_hash"],
            "started_at": row["started_at"],
            "crashed_at": row["crashed_at"],
        }
        for row in rows
    ]


def fetch_player_bet(round_id, user_id):
    with db_lock:
        conn = get_db()
        row = conn.execute(
            """
            SELECT id, user_id, round_id, amount, auto_cashout, cashout_multiplier,
                   result, payout, created_at, settled_at
            FROM bets
            WHERE round_id = ? AND user_id = ?
            """,
            (round_id, user_id),
        ).fetchone()
        conn.close()
    return row


def fetch_round_player_list(round_id):
    with db_lock:
        conn = get_db()
        rows = conn.execute(
            """
            SELECT b.id, u.username, b.amount, b.auto_cashout,
                   b.cashout_multiplier, b.result, b.payout
            FROM bets b
            JOIN users u ON u.id = b.user_id
            WHERE b.round_id = ?
            ORDER BY b.created_at ASC
            """,
            (round_id,),
        ).fetchall()
        conn.close()
    players = []
    for row in rows:
        players.append(
            {
                "username": row["username"],
                "amount": format_money(row["amount"]),
                "auto_cashout": row["auto_cashout"],
                "cashout_multiplier": row["cashout_multiplier"],
                "result": row["result"],
                "payout": format_money(row["payout"]),
            }
        )
    return players


def get_user_public_state(user_id):
    user = get_user_by_id(user_id)
    if user is None:
        return None
    return {
        "id": user["id"],
        "username": user["username"],
        "balance": format_money(user["balance"]),
    }


class CrashGameEngine:
    def __init__(self, sio):
        self.socketio = sio
        self.lock = threading.RLock()
        self.state = STATE_WAITING
        self.round_id = None
        self.countdown = COUNTDOWN_SECONDS
        self.current_multiplier = 1.0
        self.crash_point = 1.0
        self.seed_hash = ""
        self.server_seed = ""
        self.nonce = self._load_next_nonce()
        self.running_since = None
        self.last_tick_sent = 0.0
        self.player_bets = {}
        self.rate_limits = {}
        self.thread_started = False

    def _load_next_nonce(self):
        with db_lock:
            conn = get_db()
            row = conn.execute("SELECT COALESCE(MAX(nonce), 0) AS max_nonce FROM rounds").fetchone()
            conn.close()
        return int(row["max_nonce"]) + 1

    def start(self):
        with self.lock:
            if self.thread_started:
                return
            self.thread_started = True
            self.socketio.start_background_task(self._game_loop)

    def _make_crash_point(self):
        payload = f"{self.server_seed}:{self.nonce}".encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        normalized = int(digest[:13], 16) / float(0x1FFFFFFFFFFFF)
        normalized = clamp(normalized, 0.000001, 0.999999)
        crash_point = 1.0 + (-math.log(1 - normalized) * 2.75)
        crash_point = clamp(round(crash_point, 2), 1.0, MAX_CRASH_POINT)
        return crash_point, digest

    def _create_round(self):
        self.server_seed = secrets.token_hex(16)
        crash_point, digest = self._make_crash_point()
        started_at = utc_now()
        with db_lock:
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO rounds (nonce, server_seed, seed_hash, crash_point, phase, started_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (self.nonce, self.server_seed, digest, crash_point, STATE_STARTING, started_at),
            )
            round_id = cur.lastrowid
            conn.commit()
            conn.close()
        self.round_id = round_id
        self.crash_point = crash_point
        self.seed_hash = digest
        self.countdown = COUNTDOWN_SECONDS
        self.current_multiplier = 1.0
        self.running_since = None
        self.player_bets = {}
        self.nonce += 1

    def _update_round_phase(self, phase):
        if self.round_id is None:
            return
        with db_lock:
            conn = get_db()
            conn.execute(
                "UPDATE rounds SET phase = ?, crashed_at = CASE WHEN ? = ? THEN ? ELSE crashed_at END WHERE id = ?",
                (phase, phase, STATE_CRASHED, utc_now(), self.round_id),
            )
            conn.commit()
            conn.close()

    def get_snapshot(self):
        return {
            "state": self.state,
            "round_id": self.round_id,
            "countdown": self.countdown,
            "multiplier": round(self.current_multiplier, 2),
            "crash_point": round(self.crash_point, 2) if self.state == STATE_CRASHED else None,
            "seed_hash": self.seed_hash,
            "history": fetch_round_history(10),
            "players": fetch_round_player_list(self.round_id) if self.round_id else [],
        }

    def player_state(self, user_id):
        user = get_user_public_state(user_id)
        if user is None:
            return None
        active_bet = None
        if self.round_id:
            bet = fetch_player_bet(self.round_id, user_id)
            if bet is not None:
                active_bet = {
                    "amount": format_money(bet["amount"]),
                    "auto_cashout": bet["auto_cashout"],
                    "cashout_multiplier": bet["cashout_multiplier"],
                    "result": bet["result"],
                    "payout": format_money(bet["payout"]),
                }
        user["active_bet"] = active_bet
        return user

    def _emit_room(self, event, payload):
        self.socketio.emit(event, payload, room=GAME_ROOM)

    def _emit_user(self, user_id, event, payload):
        sid = user_socket_map.get(user_id)
        if sid:
            self.socketio.emit(event, payload, to=sid)

    def _broadcast_table_state(self):
        self._emit_room(
            "table_state",
            {
                "state": self.state,
                "round_id": self.round_id,
                "countdown": self.countdown,
                "multiplier": round(self.current_multiplier, 2),
                "seed_hash": self.seed_hash,
                "players": fetch_round_player_list(self.round_id) if self.round_id else [],
                "history": fetch_round_history(10),
            },
        )

    def _broadcast_history(self):
        self._emit_room("round_history", {"items": fetch_round_history(10)})

    def _broadcast_players(self):
        self._emit_room(
            "live_players",
            {"players": fetch_round_player_list(self.round_id) if self.round_id else []},
        )

    def _emit_balance(self, user_id):
        state = self.player_state(user_id)
        if state:
            self._emit_user(user_id, "player_state", state)

    def _mark_losses(self):
        if self.round_id is None:
            return
        with db_lock:
            conn = get_db()
            conn.execute(
                """
                UPDATE bets
                SET result = 'lost',
                    payout = 0,
                    settled_at = ?
                WHERE round_id = ? AND result = 'pending'
                """,
                (utc_now(), self.round_id),
            )
            conn.commit()
            rows = conn.execute(
                """
                SELECT DISTINCT user_id, amount
                FROM bets
                WHERE round_id = ? AND result = 'lost'
                """,
                (self.round_id,),
            ).fetchall()
            conn.close()
        for row in rows:
            self._emit_user(
                row["user_id"],
                "bet_result",
                {
                    "status": "loss",
                    "payout": 0,
                    "multiplier": round(self.crash_point, 2),
                    "message": f"Missed the cash out. Lost {format_money(row['amount']):.2f}.",
                },
            )
            self._emit_balance(row["user_id"])

    def _settle_cashout(self, user_id, multiplier):
        if self.round_id is None:
            return False, "No active round."
        with db_lock:
            conn = get_db()
            bet = conn.execute(
                """
                SELECT id, amount, result
                FROM bets
                WHERE round_id = ? AND user_id = ?
                """,
                (self.round_id, user_id),
            ).fetchone()
            if bet is None:
                conn.close()
                return False, "No active bet found."
            if bet["result"] != "pending":
                conn.close()
                return False, "Bet already settled."

            payout = format_money(bet["amount"] * multiplier)
            conn.execute(
                """
                UPDATE bets
                SET result = 'cashed_out',
                    cashout_multiplier = ?,
                    payout = ?,
                    settled_at = ?
                WHERE id = ?
                """,
                (round(multiplier, 2), payout, utc_now(), bet["id"]),
            )
            user = conn.execute(
                "SELECT balance FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            next_balance = format_money(user["balance"] + payout)
            conn.execute(
                "UPDATE users SET balance = ? WHERE id = ?",
                (next_balance, user_id),
            )
            conn.commit()
            conn.close()

        self._emit_user(
            user_id,
            "player_cashout_result",
            {
                "status": "win",
                "multiplier": round(multiplier, 2),
                "payout": payout,
                "message": f"Cashed out at {multiplier:.2f}x",
            },
        )
        self._emit_user(
            user_id,
            "bet_result",
            {
                "status": "win",
                "payout": payout,
                "multiplier": round(multiplier, 2),
                "message": f"Win {payout:.2f}",
            },
        )
        self._emit_balance(user_id)
        self._broadcast_players()
        return True, None

    def _check_auto_cashouts(self):
        if self.round_id is None:
            return
        with db_lock:
            conn = get_db()
            rows = conn.execute(
                """
                SELECT user_id, auto_cashout
                FROM bets
                WHERE round_id = ? AND result = 'pending' AND auto_cashout IS NOT NULL
                """,
                (self.round_id,),
            ).fetchall()
            conn.close()
        for row in rows:
            target = row["auto_cashout"]
            if target and self.current_multiplier >= target:
                self._settle_cashout(row["user_id"], target)

    def rate_limit_ok(self, sid, action, cooldown=0.5):
        key = (sid, action)
        now = time.time()
        previous = self.rate_limits.get(key, 0.0)
        if now - previous < cooldown:
            return False
        self.rate_limits[key] = now
        return True

    def place_bet(self, user_id, amount, auto_cashout=None):
        with self.lock:
            if self.state != STATE_STARTING or self.round_id is None:
                return False, "Bets are only accepted during the countdown."

            amount = format_money(amount)
            if amount <= 0:
                return False, "Bet amount must be greater than zero."

            active_players = fetch_round_player_list(self.round_id)
            if len(active_players) >= MAX_CONCURRENT_PLAYERS:
                return False, "This room already has 10 active players."

            existing = fetch_player_bet(self.round_id, user_id)
            if existing is not None:
                return False, "You already placed a bet this round."

            if auto_cashout is not None:
                auto_cashout = clamp(round(float(auto_cashout), 2), 1.1, MAX_CRASH_POINT)

            user, error = adjust_user_balance(user_id, -amount)
            if error:
                return False, error

            try:
                with db_lock:
                    conn = get_db()
                    conn.execute(
                        """
                        INSERT INTO bets (
                            user_id, round_id, amount, auto_cashout, cashout_multiplier,
                            result, payout, created_at
                        ) VALUES (?, ?, ?, ?, NULL, 'pending', 0, ?)
                        """,
                        (user_id, self.round_id, amount, auto_cashout, utc_now()),
                    )
                    conn.commit()
                    conn.close()
            except sqlite3.IntegrityError:
                adjust_user_balance(user_id, amount)
                return False, "You already placed a bet this round."

            self._emit_balance(user_id)
            self._broadcast_players()
            return True, f"Bet accepted for {amount:.2f}."

    def cash_out(self, user_id):
        with self.lock:
            if self.state != STATE_RUNNING:
                return False, "Cash out is only available while the round is running."
            if self.current_multiplier >= self.crash_point:
                return False, "Too late. The round already crashed."
            return self._settle_cashout(user_id, self.current_multiplier)

    def _game_loop(self):
        while True:
            with self.lock:
                self.state = STATE_STARTING
                self._create_round()
                self._update_round_phase(STATE_STARTING)
                self._broadcast_table_state()

            for seconds_left in range(COUNTDOWN_SECONDS, 0, -1):
                with self.lock:
                    self.state = STATE_STARTING
                    self.countdown = seconds_left
                    self._emit_room(
                        "countdown_timer",
                        {
                            "seconds_left": seconds_left,
                            "round_id": self.round_id,
                            "seed_hash": self.seed_hash,
                        },
                    )
                time.sleep(1)

            with self.lock:
                self.state = STATE_RUNNING
                self.running_since = time.time()
                self.current_multiplier = 1.0
                self._update_round_phase(STATE_RUNNING)
                self._emit_room(
                    "round_start",
                    {
                        "round_id": self.round_id,
                        "seed_hash": self.seed_hash,
                        "started_at": utc_now(),
                    },
                )
                self._broadcast_players()

            while True:
                with self.lock:
                    elapsed = time.time() - self.running_since
                    self.current_multiplier = round(max(1.0, math.exp(0.15 * elapsed)), 2)
                    self._check_auto_cashouts()
                    if self.current_multiplier >= self.crash_point:
                        self.current_multiplier = self.crash_point
                        break
                    self._emit_room(
                        "multiplier_update",
                        {
                            "round_id": self.round_id,
                            "value": self.current_multiplier,
                        },
                    )
                time.sleep(TICK_RATE)

            with self.lock:
                self.state = STATE_CRASHED
                self._update_round_phase(STATE_CRASHED)
                self._mark_losses()
                self._emit_room(
                    "round_crash",
                    {
                        "round_id": self.round_id,
                        "crash_point": round(self.crash_point, 2),
                        "server_seed": self.server_seed,
                        "seed_hash": self.seed_hash,
                    },
                )
                self._broadcast_players()
                self._broadcast_history()

            time.sleep(CRASH_HOLD_SECONDS)

    def send_full_state(self, user_id):
        self._emit_user(user_id, "player_state", self.player_state(user_id))
        self._emit_user(user_id, "table_state", self.get_snapshot())

init_db()
engine = CrashGameEngine(socketio)
engine.start()


@app.route("/")
def index():
    return render_template("index.html")


@socketio.on("connect")
def on_connect():
    emit("connected", {"ok": True, "message": "Connected to crash server."})


@socketio.on("join_game")
def on_join_game(data):
    username = (data or {}).get("username", "")
    user, error = get_or_create_user(username)
    if error:
        emit("server_error", {"message": error})
        return

    socket_user_map[request_sid()] = user["id"]
    user_socket_map[user["id"]] = request_sid()
    join_room(GAME_ROOM)
    engine.start()
    engine.send_full_state(user["id"])
    emit(
        "joined_game",
        {
            "user": {
                "id": user["id"],
                "username": user["username"],
                "balance": format_money(user["balance"]),
            }
        },
    )


def request_sid():
    from flask import request

    return request.sid


def require_user():
    user_id = socket_user_map.get(request_sid())
    if not user_id:
        emit("server_error", {"message": "Join the game first."})
        return None
    return user_id


@socketio.on("place_bet")
def on_place_bet(data):
    user_id = require_user()
    if user_id is None:
        return
    sid = request_sid()
    if not engine.rate_limit_ok(sid, "place_bet", cooldown=0.75):
        emit("server_error", {"message": "Slow down before placing another bet."})
        return

    payload = data or {}
    try:
        amount = float(payload.get("amount", 0))
    except (TypeError, ValueError):
        emit("server_error", {"message": "Invalid bet amount."})
        return

    auto_cashout = payload.get("auto_cashout")
    if auto_cashout in ("", None):
        auto_cashout = None
    else:
        try:
            auto_cashout = float(auto_cashout)
        except (TypeError, ValueError):
            emit("server_error", {"message": "Invalid auto cash-out target."})
            return

    ok, message = engine.place_bet(user_id, amount, auto_cashout=auto_cashout)
    if ok:
        emit("bet_placed", {"message": message})
    else:
        emit("server_error", {"message": message})


@socketio.on("cash_out")
def on_cash_out():
    user_id = require_user()
    if user_id is None:
        return
    sid = request_sid()
    if not engine.rate_limit_ok(sid, "cash_out", cooldown=0.35):
        emit("server_error", {"message": "Cash out request is cooling down."})
        return

    ok, message = engine.cash_out(user_id)
    if not ok and message:
        emit("server_error", {"message": message})


@socketio.on("disconnect")
def on_disconnect():
    sid = request_sid()
    user_id = socket_user_map.pop(sid, None)
    if user_id is not None and user_socket_map.get(user_id) == sid:
        user_socket_map.pop(user_id, None)


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)
