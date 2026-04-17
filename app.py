from datetime import datetime, timedelta
import json
import os
import random
import threading

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import CheckConstraint, UniqueConstraint, case, func
from werkzeug.security import check_password_hash, generate_password_hash

from realtime_games import build_game_registry


def configure_database_url():
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        return "sqlite:///betting_app.db"
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    if database_url.startswith("postgresql://") and "sslmode=" not in database_url:
        separator = "&" if "?" in database_url else "?"
        database_url = f"{database_url}{separator}sslmode=require"
    return database_url


app = Flask(__name__)
app.secret_key = os.environ.get(
    "SECRET_KEY", "change_this_in_production_use_long_random_string"
)
app.config["SQLALCHEMY_DATABASE_URI"] = configure_database_url()
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
}

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
wallet_lock = threading.RLock()

REALTIME_GAME_CATALOG = [
    {
        "slug": "neon-rocket",
        "title": "Neon Rocket",
        "tagline": "Cash out before the server-generated crash point hits.",
        "icon": "fa-rocket",
        "theme": "rocket",
        "template": "games/neon_rocket.html",
        "betting_window": 12,
        "max_players": 10,
    },
    {
        "slug": "color-wheel",
        "title": "Color Wheel Fortune",
        "tagline": "Weighted wheel with red, blue, green, and gold payouts.",
        "icon": "fa-dharmachakra",
        "theme": "wheel",
        "template": "games/color_wheel.html",
        "betting_window": 45,
        "max_players": 10,
    },
    {
        "slug": "cyber-derby",
        "title": "Cyber Horse Derby",
        "tagline": "Back a synthetic horse and watch the weighted sprint unfold.",
        "icon": "fa-horse",
        "theme": "derby",
        "template": "games/cyber_derby.html",
        "betting_window": 20,
        "max_players": 10,
    },
    {
        "slug": "dice-duel",
        "title": "Dice Duel",
        "tagline": "Pick high or low before the dual dice stop rolling.",
        "icon": "fa-dice",
        "theme": "dice",
        "template": "games/dice_duel.html",
        "betting_window": 20,
        "max_players": 10,
    },
]
REALTIME_GAME_LOOKUP = {game["slug"]: game for game in REALTIME_GAME_CATALOG}


class User(db.Model):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("balance >= 0", name="balance_non_negative"),)

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password = db.Column(db.Text, nullable=False)
    email = db.Column(db.Text, nullable=False, default="")
    phone = db.Column(db.Text, nullable=False, default="")
    balance = db.Column(db.Integer, nullable=False, default=1000)


class Wallet(db.Model):
    __tablename__ = "wallets"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    balance = db.Column(db.Integer, nullable=False, default=1000)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class Transaction(db.Model):
    __tablename__ = "transactions"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(
        db.String(80),
        db.ForeignKey("users.username", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    type = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="Pending")
    timestamp = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, server_default=func.now()
    )


class GameRoom(db.Model):
    __tablename__ = "game_rooms"

    id = db.Column(db.Integer, primary_key=True)
    game_type = db.Column(db.String(30), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="waiting")
    max_players = db.Column(db.Integer, nullable=False, default=10)
    bet_amount = db.Column(db.Integer, nullable=False, default=0)
    result = db.Column(db.String(30), nullable=True)
    created_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, server_default=func.now()
    )
    ended_at = db.Column(db.DateTime, nullable=True)
    creator = db.Column(
        db.String(80),
        db.ForeignKey("users.username", ondelete="SET NULL"),
        nullable=True,
    )


class GamePlayer(db.Model):
    __tablename__ = "game_players"

    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(
        db.Integer, db.ForeignKey("game_rooms.id", ondelete="CASCADE"), nullable=False
    )
    username = db.Column(
        db.String(80),
        db.ForeignKey("users.username", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    bet_amount = db.Column(db.Integer, nullable=False)
    choice = db.Column(db.String(30), nullable=True)
    payout = db.Column(db.Integer, nullable=False, default=0)
    result = db.Column(db.String(20), nullable=False, default="pending")
    joined_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, server_default=func.now()
    )


class GameRound(db.Model):
    __tablename__ = "rt_game_rounds"

    id = db.Column(db.Integer, primary_key=True)
    game_slug = db.Column(db.String(50), nullable=False, index=True)
    round_code = db.Column(db.String(20), nullable=False, index=True)
    phase = db.Column(db.String(20), nullable=False, default="betting")
    started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    betting_ends_at = db.Column(db.DateTime, nullable=True)
    running_started_at = db.Column(db.DateTime, nullable=True)
    result_at = db.Column(db.DateTime, nullable=True)
    state_json = db.Column(db.Text, nullable=False, default="{}")


class GameBet(db.Model):
    __tablename__ = "rt_game_bets"
    __table_args__ = (UniqueConstraint("round_id", "username", name="uq_round_user_bet"),)

    id = db.Column(db.Integer, primary_key=True)
    round_id = db.Column(db.Integer, db.ForeignKey("rt_game_rounds.id", ondelete="CASCADE"), nullable=False, index=True)
    game_slug = db.Column(db.String(50), nullable=False, index=True)
    username = db.Column(db.String(80), db.ForeignKey("users.username", ondelete="CASCADE"), nullable=False, index=True)
    amount = db.Column(db.Integer, nullable=False)
    choice = db.Column(db.String(50), nullable=False)
    extra_json = db.Column(db.Text, nullable=False, default="{}")
    status = db.Column(db.String(30), nullable=False, default="placed")
    payout = db.Column(db.Integer, nullable=False, default=0)
    cashout_multiplier = db.Column(db.Float, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class BetHistory(db.Model):
    __tablename__ = "rt_game_history"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), db.ForeignKey("users.username", ondelete="CASCADE"), nullable=False, index=True)
    game_slug = db.Column(db.String(50), nullable=False, index=True)
    round_id = db.Column(db.Integer, db.ForeignKey("rt_game_rounds.id", ondelete="SET NULL"), nullable=True)
    bet_id = db.Column(db.Integer, db.ForeignKey("rt_game_bets.id", ondelete="SET NULL"), nullable=True)
    amount = db.Column(db.Integer, nullable=False)
    payout = db.Column(db.Integer, nullable=False, default=0)
    outcome = db.Column(db.String(30), nullable=False)
    details_json = db.Column(db.Text, nullable=False, default="{}")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, server_default=func.now())


def future_time(seconds):
    return datetime.utcnow() + timedelta(seconds=seconds)


def ensure_wallet_for_user(user, *, commit=False):
    wallet = Wallet.query.filter_by(user_id=user.id).first()
    if not wallet:
        wallet = Wallet(user_id=user.id, balance=user.balance)
        db.session.add(wallet)
    else:
        wallet.balance = user.balance
    if commit:
        db.session.commit()
    return wallet


def get_balance(username):
    user = User.query.filter_by(username=username).first()
    if not user:
        return 0
    wallet = Wallet.query.filter_by(user_id=user.id).first()
    return wallet.balance if wallet else user.balance


def adjust_balance(username, delta, reason="wallet:update"):
    with wallet_lock:
        user = User.query.filter_by(username=username).first()
        if not user:
            return False, "User not found."
        wallet = ensure_wallet_for_user(user)
        next_balance = wallet.balance + delta
        if next_balance < 0:
            db.session.rollback()
            return False, "Insufficient virtual points."
        wallet.balance = next_balance
        user.balance = next_balance
        db.session.commit()
        app.logger.info("Wallet change for %s: %s (%s)", username, delta, reason)
        return True, "Wallet updated."


def sync_existing_wallets():
    users = User.query.all()
    for user in users:
        ensure_wallet_for_user(user)
    db.session.commit()


def init_db():
    with app.app_context():
        db.create_all()
        sync_existing_wallets()
        app.logger.info("Database initialized successfully.")


@app.before_request
def keep_wallet_in_sync():
    if "user" not in session:
        return None
    user = User.query.filter_by(username=session["user"]).first()
    if user:
        wallet = Wallet.query.filter_by(user_id=user.id).first()
        if not wallet or wallet.balance != user.balance:
            ensure_wallet_for_user(user, commit=True)
    return None


def current_game_snapshot(game_slug):
    engine = realtime_games[game_slug]
    snapshot = engine.get_public_snapshot()
    snapshot["wallet_balance"] = get_balance(session["user"]) if "user" in session else 0
    snapshot["choices"] = engine.choices
    snapshot["supports_cashout"] = engine.supports_cashout
    snapshot["game"] = REALTIME_GAME_LOOKUP[game_slug]
    return snapshot


def recent_game_history(game_slug, limit=12):
    rows = (
        BetHistory.query.filter_by(game_slug=game_slug)
        .order_by(BetHistory.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "username": row.username,
            "amount": row.amount,
            "payout": row.payout,
            "outcome": row.outcome,
            "details": json.loads(row.details_json or "{}"),
            "created_at": row.created_at.strftime("%H:%M:%S"),
        }
        for row in rows
    ]


def my_game_history(game_slug, username, limit=10):
    rows = (
        BetHistory.query.filter_by(game_slug=game_slug, username=username)
        .order_by(BetHistory.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "amount": row.amount,
            "payout": row.payout,
            "outcome": row.outcome,
            "details": json.loads(row.details_json or "{}"),
            "created_at": row.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        }
        for row in rows
    ]


@app.route("/", methods=["GET", "POST"])
def login():
    if "user" in session:
        return redirect("/games")

    if request.method == "POST":
        user = request.form.get("username", "").strip()
        pw = request.form.get("password", "")

        if not user or not pw:
            flash("Username and password are required.", "danger")
            return render_template("login.html")

        try:
            existing_user = User.query.filter_by(username=user).first()
        except Exception as e:
            db.session.rollback()
            app.logger.exception("Database error during login: %s", e)
            flash("Database connection error. Please try again.", "danger")
            return render_template("login.html")

        if existing_user:
            stored_pw = existing_user.password
            valid = check_password_hash(stored_pw, pw) if stored_pw.startswith(("pbkdf2:", "scrypt:")) else stored_pw == pw
            if valid:
                session["user"] = existing_user.username
                return redirect("/games")

        flash("Invalid username or password.", "danger")

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        user = request.form.get("username", "").strip()
        pw = request.form.get("password", "")

        if not user or not pw:
            flash("Username and password are required.", "danger")
            return render_template("register.html")
        if len(pw) < 4:
            flash("Password must be at least 4 characters.", "danger")
            return render_template("register.html")

        try:
            if User.query.filter_by(username=user).first():
                flash("Username already taken.", "danger")
                return render_template("register.html")

            new_user = User(username=user, password=generate_password_hash(pw), balance=1000)
            db.session.add(new_user)
            db.session.flush()
            db.session.add(Wallet(user_id=new_user.id, balance=new_user.balance))
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            app.logger.exception("Database error during registration: %s", e)
            flash("Database connection error. Please try again.", "danger")
            return render_template("register.html")

        flash("Account created! Please login.", "success")
        return redirect("/")

    return render_template("register.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/")


@app.route("/games")
def games():
    if "user" not in session:
        return redirect("/")

    balance = get_balance(session["user"])
    try:
        player_counts = (
            db.session.query(
                GamePlayer.room_id, func.count(GamePlayer.id).label("player_count")
            )
            .group_by(GamePlayer.room_id)
            .subquery()
        )

        rows = (
            db.session.query(
                GameRoom.id,
                GameRoom.game_type,
                GameRoom.status,
                GameRoom.bet_amount,
                func.coalesce(player_counts.c.player_count, 0),
            )
            .outerjoin(player_counts, GameRoom.id == player_counts.c.room_id)
            .filter(GameRoom.status.in_(["waiting", "running"]))
            .order_by(GameRoom.created_at.desc())
            .limit(20)
            .all()
        )
        rooms = [tuple(row) for row in rows]
        live_games = []
        for item in REALTIME_GAME_CATALOG:
            snapshot = realtime_games[item["slug"]].get_public_snapshot()
            live_games.append(
                {
                    **item,
                    "phase": snapshot.get("phase", "booting"),
                    "player_count": snapshot.get("player_count", 0),
                    "status_text": snapshot.get("state", {}).get("status_text", "Starting..."),
                }
            )
        return render_template("games.html", balance=balance, rooms=rooms, live_games=live_games)
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Database error in games: %s", e)
        flash("Database connection error. Please try again.", "danger")
        return render_template("games.html", balance=balance, rooms=[], live_games=[])


@app.route("/realtime/<game_slug>")
def realtime_game(game_slug):
    if "user" not in session:
        return redirect("/")
    if game_slug not in REALTIME_GAME_LOOKUP:
        flash("Game not found.", "danger")
        return redirect("/games")

    config = REALTIME_GAME_LOOKUP[game_slug]
    return render_template(
        config["template"],
        balance=get_balance(session["user"]),
        game=config,
        snapshot=current_game_snapshot(game_slug),
        recent_history=recent_game_history(game_slug),
        my_history=my_game_history(game_slug, session["user"]),
    )


@app.route("/api/realtime/<game_slug>/state")
def realtime_state(game_slug):
    if "user" not in session:
        return jsonify({"ok": False, "message": "Login required."}), 401
    if game_slug not in REALTIME_GAME_LOOKUP:
        return jsonify({"ok": False, "message": "Unknown game."}), 404
    return jsonify({"ok": True, "state": current_game_snapshot(game_slug)})


@app.route("/api/realtime/<game_slug>/bet", methods=["POST"])
def realtime_bet(game_slug):
    if "user" not in session:
        return jsonify({"ok": False, "message": "Login required."}), 401
    if game_slug not in REALTIME_GAME_LOOKUP:
        return jsonify({"ok": False, "message": "Unknown game."}), 404

    payload = request.get_json(silent=True) or request.form
    try:
        amount = int(payload.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Invalid bet amount."}), 400
    choice = (payload.get("choice") or "").strip().lower()

    ok, message = realtime_games[game_slug].place_bet(session["user"], amount, choice, extra={})
    status_code = 200 if ok else 400
    return jsonify(
        {
            "ok": ok,
            "message": message,
            "balance": get_balance(session["user"]),
            "history": my_game_history(game_slug, session["user"]),
        }
    ), status_code


@app.route("/api/realtime/neon-rocket/cashout", methods=["POST"])
def realtime_cashout():
    if "user" not in session:
        return jsonify({"ok": False, "message": "Login required."}), 401
    ok, message = realtime_games["neon-rocket"].cash_out(session["user"])
    return jsonify({"ok": ok, "message": message, "balance": get_balance(session["user"])}), (200 if ok else 400)


def create_room(game_type):
    if "user" not in session:
        return redirect("/")

    try:
        bet = int(request.form.get("bet_amount", 0))
    except (ValueError, TypeError):
        flash("Invalid bet amount.", "danger")
        return redirect("/games")

    if bet <= 0:
        flash("Bet amount must be greater than 0.", "danger")
        return redirect("/games")

    try:
        current_user = User.query.filter_by(username=session["user"]).first()
        if not current_user or bet > get_balance(current_user.username):
            flash("Insufficient balance.", "danger")
            return redirect("/games")

        room = GameRoom(
            game_type=game_type,
            bet_amount=bet,
            max_players=10,
            creator=current_user.username,
        )
        db.session.add(room)
        db.session.flush()

        default_choice = {"coinflip": "heads", "dice": "1", "colorbet": "red"}[game_type]
        adjust_balance(current_user.username, -bet, reason=f"classic:{game_type}:create")
        db.session.add(
            GamePlayer(
                room_id=room.id,
                username=current_user.username,
                bet_amount=bet,
                choice=default_choice,
            )
        )
        db.session.commit()
        flash(
            "Room created! You joined with a default choice and can change it before the game starts.",
            "info",
        )
        return redirect(f"/game/room/{room.id}")
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Database error in create_room: %s", e)
        flash("Database connection error. Please try again.", "danger")
        return redirect("/games")


@app.route("/game/coinflip/create", methods=["POST"])
def coinflip_create():
    return create_room("coinflip")


@app.route("/game/dice/create", methods=["POST"])
def dice_create():
    return create_room("dice")


@app.route("/game/colorbet/create", methods=["POST"])
def colorbet_create():
    return create_room("colorbet")


@app.route("/game/room/<int:room_id>")
def game_room(room_id):
    if "user" not in session:
        return redirect("/")

    try:
        room = GameRoom.query.get(room_id)
        if not room:
            flash("Room not found.", "danger")
            return redirect("/games")

        players = (
            GamePlayer.query.filter_by(room_id=room_id)
            .order_by(GamePlayer.joined_at)
            .all()
        )
        players_data = [
            (player.username, player.bet_amount, player.choice, player.result, player.payout)
            for player in players
        ]
        already_joined = GamePlayer.query.filter_by(
            room_id=room_id, username=session["user"]
        ).first()
        room_data = (
            room.id,
            room.game_type,
            room.status,
            room.max_players,
            room.bet_amount,
            room.result,
            room.created_at,
            room.ended_at,
            None,
            room.creator,
        )

        return render_template(
            "game_room.html",
            room=room_data,
            players=players_data,
            already_joined=already_joined,
            balance=get_balance(session["user"]),
            username=session["user"],
        )
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Database error in game_room: %s", e)
        flash("Database connection error. Please try again.", "danger")
        return redirect("/games")


@app.route("/game/room/<int:room_id>/join", methods=["POST"])
def join_room_route(room_id):
    if "user" not in session:
        return redirect("/")

    choice = request.form.get("choice", "").strip()
    if not choice:
        flash("Please select a choice.", "danger")
        return redirect(f"/game/room/{room_id}")

    try:
        room = GameRoom.query.filter_by(id=room_id, status="waiting").first()
        if not room:
            flash("Room is not available.", "danger")
            return redirect("/games")

        current_user = User.query.filter_by(username=session["user"]).first()
        if not current_user or room.bet_amount > get_balance(current_user.username):
            flash("Insufficient balance.", "danger")
            return redirect(f"/game/room/{room_id}")

        if GamePlayer.query.filter_by(room_id=room_id, username=session["user"]).first():
            flash("You already joined this room.", "warning")
            return redirect(f"/game/room/{room_id}")

        player_count = GamePlayer.query.filter_by(room_id=room_id).count()
        if player_count >= room.max_players:
            flash("Room is full.", "danger")
            return redirect("/games")

        adjust_balance(current_user.username, -room.bet_amount, reason=f"classic:{room.game_type}:join")
        db.session.add(
            GamePlayer(
                room_id=room_id,
                username=current_user.username,
                bet_amount=room.bet_amount,
                choice=choice,
            )
        )
        db.session.commit()
        return redirect(f"/game/room/{room_id}")
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Database error in join_room: %s", e)
        flash("Database connection error. Please try again.", "danger")
        return redirect("/games")


@app.route("/game/room/<int:room_id>/start", methods=["POST"])
def start_game(room_id):
    if "user" not in session:
        return redirect("/")

    try:
        room = GameRoom.query.filter_by(id=room_id, status="waiting").first()
        if not room:
            flash("Cannot start game.", "danger")
            return redirect(f"/game/room/{room_id}")

        if room.creator and room.creator != session["user"]:
            flash("Only the room creator can start the game.", "danger")
            return redirect(f"/game/room/{room_id}")

        players = GamePlayer.query.filter_by(room_id=room_id).all()
        if len(players) < 2:
            flash("Need at least 2 players to start.", "warning")
            return redirect(f"/game/room/{room_id}")

        if room.game_type == "coinflip":
            result = random.choice(["heads", "tails"])
        elif room.game_type == "dice":
            result = str(random.randint(1, 6))
        elif room.game_type == "colorbet":
            result = random.choice(["red", "green", "blue"])
        else:
            result = "unknown"

        winners = [player for player in players if player.choice == result]
        total_pool = sum(player.bet_amount for player in players)

        if winners:
            share = total_pool // len(winners)
            winner_names = {winner.username for winner in winners}
            for player in players:
                if player.username in winner_names:
                    player.result = "won"
                    player.payout = share
                    adjust_balance(player.username, share, reason=f"classic:{room.game_type}:win")
                else:
                    player.result = "lost"
                    player.payout = 0
        else:
            for player in players:
                player.result = "lost"
                player.payout = 0

        room.status = "finished"
        room.result = result
        room.ended_at = datetime.utcnow()
        db.session.commit()
        return redirect(f"/game/room/{room_id}")
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Database error in start_game: %s", e)
        flash("Database connection error. Please try again.", "danger")
        return redirect("/games")


@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
    if "user" not in session:
        return redirect("/")

    balance = get_balance(session["user"])
    if request.method == "POST":
        req_type = request.form.get("type")
        try:
            amount = int(request.form.get("amount", 0))
        except (ValueError, TypeError):
            flash("Invalid amount.", "danger")
            return redirect("/dashboard")

        if amount <= 0:
            flash("Amount must be greater than 0.", "danger")
            return redirect("/dashboard")

        try:
            current_user = User.query.filter_by(username=session["user"]).first()
            if not current_user:
                flash("User not found.", "danger")
                return redirect("/dashboard")

            if req_type == "withdraw":
                ok, message = adjust_balance(current_user.username, -amount, reason="dashboard:withdraw")
                if not ok:
                    flash(message, "danger")
                    return redirect("/dashboard")
                db.session.add(Transaction(username=current_user.username, type=req_type, amount=amount, status="Pending"))
                flash("Withdraw request submitted! Amount has been held pending admin approval.", "info")
            elif req_type == "deposit":
                db.session.add(Transaction(username=current_user.username, type=req_type, amount=amount, status="Pending"))
                flash("Deposit request submitted! Waiting for admin approval.", "info")
            else:
                flash("Invalid request type.", "danger")
                return redirect("/dashboard")

            db.session.commit()
            return redirect("/dashboard")
        except Exception as e:
            db.session.rollback()
            app.logger.exception("Database error in dashboard: %s", e)
            flash("Database connection error. Please try again.", "danger")
            return redirect("/dashboard")

    return render_template("dashboard.html", balance=balance)


@app.route("/profile", methods=["GET", "POST"])
def profile():
    if "user" not in session:
        return redirect("/")

    try:
        current_user = User.query.filter_by(username=session["user"]).first()
        if not current_user:
            flash("User not found.", "danger")
            return redirect("/")

        if request.method == "POST":
            current_user.email = request.form.get("email", "").strip()
            current_user.phone = request.form.get("phone", "").strip()
            db.session.commit()
            flash("Profile updated!", "success")

        stats = (
            db.session.query(
                func.count(GamePlayer.id),
                func.coalesce(func.sum(GamePlayer.payout), 0),
                func.coalesce(func.sum(case((GamePlayer.result == "won", 1), else_=0)), 0),
            )
            .filter(GamePlayer.username == current_user.username)
            .first()
        )

        return render_template(
            "profile.html",
            username=current_user.username,
            balance=get_balance(current_user.username),
            email=current_user.email,
            phone=current_user.phone,
            total_games=stats[0] or 0,
            total_won=stats[2] or 0,
            total_earnings=stats[1] or 0,
        )
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Database error in profile: %s", e)
        flash("Database connection error. Please try again.", "danger")
        return redirect("/")


@app.route("/history")
def history():
    if "user" not in session:
        return redirect("/")

    try:
        transactions = (
            Transaction.query.filter_by(username=session["user"])
            .order_by(Transaction.timestamp.desc())
            .all()
        )
        transaction_data = [
            (txn.type, txn.amount, txn.status, txn.timestamp) for txn in transactions
        ]

        game_history_rows = (
            db.session.query(
                GameRoom.game_type,
                GamePlayer.bet_amount,
                GamePlayer.choice,
                GamePlayer.result,
                GamePlayer.payout,
                GameRoom.result,
                GameRoom.ended_at,
            )
            .join(GameRoom, GamePlayer.room_id == GameRoom.id)
            .filter(GamePlayer.username == session["user"])
            .order_by(GameRoom.created_at.desc())
            .all()
        )
        game_history = [tuple(row) for row in game_history_rows]
        realtime_history = my_game_history("neon-rocket", session["user"], limit=5)

        return render_template(
            "history.html",
            transactions=transaction_data,
            game_history=game_history,
            realtime_history=realtime_history,
            balance=get_balance(session["user"]),
        )
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Database error in history: %s", e)
        flash("Database connection error. Please try again.", "danger")
        return redirect("/games")


@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        user = request.form.get("username", "")
        pw = request.form.get("password", "")
        if user == ADMIN_USERNAME and pw == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect("/admin")
        return render_template("admin.html", error="Invalid admin credentials")

    if "admin" not in session:
        return render_template("admin.html", error=None)

    try:
        requests_list = (
            Transaction.query.filter_by(status="Pending")
            .order_by(Transaction.timestamp.desc())
            .all()
        )
        requests_data = [
            (txn.id, txn.username, txn.type, txn.amount) for txn in requests_list
        ]
        total_balance = db.session.query(func.coalesce(func.sum(User.balance), 0)).scalar()
        total_users = db.session.query(func.count(User.id)).scalar()

        return render_template(
            "admin_panel.html",
            requests=requests_data,
            total_balance=total_balance or 0,
            total_users=total_users or 0,
        )
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Database error in admin panel: %s", e)
        flash("Database connection error. Please try again.", "danger")
        return render_template("admin.html", error="Database error")


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin", None)
    return redirect("/admin")


@app.route("/admin/action/<int:txn_id>/<status>", methods=["POST"])
def admin_action(txn_id, status):
    if "admin" not in session:
        return redirect("/admin")
    if status not in ("Approved", "Rejected"):
        return redirect("/admin")

    try:
        txn = Transaction.query.filter_by(id=txn_id, status="Pending").first()
        if txn:
            if status == "Approved" and txn.type == "deposit":
                adjust_balance(txn.username, txn.amount, reason="admin:deposit-approved")
            elif status == "Rejected" and txn.type == "withdraw":
                adjust_balance(txn.username, txn.amount, reason="admin:withdraw-rejected")

            txn.status = status
            db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Database error in admin action: %s", e)
        flash("Database connection error. Please try again.", "danger")

    return redirect("/admin")


@app.route("/admin/users")
def admin_users():
    if "admin" not in session:
        return redirect("/admin")

    try:
        users = User.query.order_by(User.id).all()
        users_data = [
            (user.id, user.username, user.email, user.phone, get_balance(user.username)) for user in users
        ]
        return render_template("admin_users.html", users=users_data)
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Database error in admin users: %s", e)
        flash("Database connection error. Please try again.", "danger")
        return redirect("/admin")


@app.route("/admin/user/<int:user_id>", methods=["GET", "POST"])
def admin_user_detail(user_id):
    if "admin" not in session:
        return redirect("/admin")

    try:
        user = User.query.get(user_id)
        if not user:
            flash("User not found.", "danger")
            return redirect("/admin/users")

        if request.method == "POST":
            action = request.form.get("action")
            if action == "update_balance":
                try:
                    new_balance = int(request.form.get("balance", 0))
                except (ValueError, TypeError):
                    flash("Invalid balance amount.", "danger")
                    return redirect(url_for("admin_user_detail", user_id=user_id))

                if new_balance < 0:
                    flash("Balance cannot be negative.", "danger")
                    return redirect(url_for("admin_user_detail", user_id=user_id))

                user.balance = new_balance
                ensure_wallet_for_user(user)
                db.session.commit()
                flash("Balance updated!", "success")

            elif action == "update_info":
                user.email = request.form.get("email", "").strip()
                user.phone = request.form.get("phone", "").strip()
                password = request.form.get("password", "").strip()
                if password:
                    user.password = generate_password_hash(password)
                db.session.commit()
                flash("User info updated!", "success")

            elif action == "delete_user":
                Wallet.query.filter_by(user_id=user.id).delete()
                Transaction.query.filter_by(username=user.username).delete()
                GamePlayer.query.filter_by(username=user.username).delete()
                GameBet.query.filter_by(username=user.username).delete()
                BetHistory.query.filter_by(username=user.username).delete()
                GameRoom.query.filter_by(creator=user.username).update({"creator": None})
                db.session.delete(user)
                db.session.commit()
                flash("User deleted.", "warning")
                return redirect("/admin/users")

        transactions = (
            Transaction.query.filter_by(username=user.username)
            .order_by(Transaction.timestamp.desc())
            .all()
        )
        transactions_data = [
            (txn.type, txn.amount, txn.status, txn.timestamp) for txn in transactions
        ]

        game_history_rows = (
            db.session.query(
                GameRoom.game_type,
                GamePlayer.bet_amount,
                GamePlayer.choice,
                GamePlayer.result,
                GamePlayer.payout,
                GameRoom.result,
                GameRoom.created_at,
            )
            .join(GameRoom, GamePlayer.room_id == GameRoom.id)
            .filter(GamePlayer.username == user.username)
            .order_by(GameRoom.created_at.desc())
            .all()
        )
        game_history = [tuple(row) for row in game_history_rows]
        user_data = (user.id, user.username, user.email, user.phone, get_balance(user.username))

        return render_template(
            "admin_user_detail.html",
            user=user_data,
            transactions=transactions_data,
            game_history=game_history,
        )
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Database error in admin_user_detail: %s", e)
        flash("Database connection error. Please try again.", "danger")
        return redirect("/admin/users")


@app.route("/admin/all_transactions")
def admin_all_transactions():
    if "admin" not in session:
        return redirect("/admin")

    try:
        rows = Transaction.query.order_by(Transaction.timestamp.desc()).all()
        data = [(row.username, row.type, row.amount, row.status, row.timestamp) for row in rows]
        total_balance = db.session.query(func.coalesce(func.sum(User.balance), 0)).scalar()
        return render_template(
            "transactions.html", data=data, total_balance=total_balance or 0
        )
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Database error in admin_all_transactions: %s", e)
        flash("Database connection error. Please try again.", "danger")
        return redirect("/admin")


@socketio.on("connect")
def socket_connected():
    if "user" in session:
        join_room(f"user:{session['user']}")
    emit("connected", {"ok": True, "message": "Socket connected."})


@socketio.on("join_game")
def socket_join_game(data):
    game_slug = (data or {}).get("game")
    if game_slug not in REALTIME_GAME_LOOKUP:
        emit("error_message", {"message": "Unknown game room."})
        return
    join_room(f"game:{game_slug}")
    emit("round_state", current_game_snapshot(game_slug))


@socketio.on("leave_game")
def socket_leave_game(data):
    game_slug = (data or {}).get("game")
    if game_slug in REALTIME_GAME_LOOKUP:
        leave_room(f"game:{game_slug}")


init_db()

realtime_games = build_game_registry(
    app,
    socketio,
    db,
    {
        "GameRound": GameRound,
        "GameBet": GameBet,
        "BetHistory": BetHistory,
    },
    {
        "get_balance": get_balance,
        "adjust_balance": adjust_balance,
        "future_time": future_time,
    },
)

for engine in realtime_games.values():
    engine.start()


if __name__ == "__main__":
    socketio.run(app, debug=False)
