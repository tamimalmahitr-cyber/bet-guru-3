import json
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError


class BaseRealtimeGame:
    slug = "base"
    title = "Base Game"
    max_players = 10
    betting_duration = 15
    running_duration = 10
    result_duration = 5
    supports_cashout = False
    room_name = None
    choices = []

    def __init__(self, app, socketio, db, models, helpers):
        self.app = app
        self.socketio = socketio
        self.db = db
        self.models = models
        self.helpers = helpers
        self.lock = threading.RLock()
        self.room_name = f"game:{self.slug}"
        self.current_round_id = None
        self.current_state = {"phase": "booting"}
        self._started = False
        self._thread = None

    def start(self):
        with self.lock:
            if self._thread and self._thread.is_alive():
                self._started = True
                return
            self._thread = threading.Thread(
                target=self._game_loop,
                name=f"rt-game-{self.slug}",
                daemon=True,
            )
            self._thread.start()
            self._started = True

    def safe_json_loads(self, raw_value, default=None):
        fallback = {} if default is None else default
        if raw_value in (None, ""):
            return deepcopy(fallback)
        try:
            return json.loads(raw_value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self.app.logger.warning(
                "Invalid JSON payload for %s realtime state: %s",
                self.slug,
                exc,
            )
            return deepcopy(fallback)

    def serialize_bet(self, bet):
        return {
            "id": bet.id,
            "username": bet.username,
            "choice": bet.choice,
            "amount": bet.amount,
            "status": bet.status,
            "payout": bet.payout,
            "cashout_multiplier": bet.cashout_multiplier,
        }

    def serialize_round(self, game_round, extra=None):
        payload = {
            "id": game_round.id,
            "game_slug": game_round.game_slug,
            "phase": game_round.phase,
            "round_code": game_round.round_code,
            "started_at": game_round.started_at.isoformat() if game_round.started_at else None,
            "betting_ends_at": game_round.betting_ends_at.isoformat()
            if game_round.betting_ends_at
            else None,
            "running_started_at": game_round.running_started_at.isoformat()
            if game_round.running_started_at
            else None,
            "result_at": game_round.result_at.isoformat() if game_round.result_at else None,
            "state": self.safe_json_loads(game_round.state_json),
        }
        if extra:
            payload.update(extra)
        return payload

    def _replace_snapshot(self, game_round, *, extra=None, players=None):
        payload = self.serialize_round(game_round, extra=extra)
        if players is None:
            players = self.current_state.get("players", [])
        payload["players"] = players
        payload["player_count"] = len(players)
        self.current_state = payload
        return payload

    def emit_state(self, event="round_state", extra=None, refresh_players=True):
        with self.app.app_context():
            game_round = self.get_current_round()
            if not game_round:
                return
            players = self.list_players(game_round.id) if refresh_players else None
            payload = self._replace_snapshot(game_round, extra=extra, players=players)
            self.socketio.emit(event, payload, room=self.room_name)

    def emit_wallet(self, username):
        balance = self.helpers["get_balance"](username)
        self.socketio.emit(
            "wallet_update",
            {"username": username, "balance": balance},
            room=f"user:{username}",
        )

    def list_players(self, round_id):
        bets = (
            self.models["GameBet"]
            .query.filter_by(round_id=round_id)
            .order_by(self.models["GameBet"].created_at.asc())
            .all()
        )
        return [self.serialize_bet(bet) for bet in bets]

    def get_current_round(self):
        if self.current_round_id is None:
            return None
        return self.models["GameRound"].query.get(self.current_round_id)

    def _load_latest_open_round(self):
        GameRound = self.models["GameRound"]
        latest_open = (
            GameRound.query.filter(
                GameRound.game_slug == self.slug,
                GameRound.phase.in_(("betting", "running")),
            )
            .order_by(GameRound.id.desc())
            .first()
        )
        if latest_open:
            self.current_round_id = latest_open.id
            self._replace_snapshot(
                latest_open,
                players=self.list_players(latest_open.id),
            )
        return latest_open

    def ensure_active_round(self):
        self.start()
        with self.lock, self.app.app_context():
            game_round = self.get_current_round()
            if game_round:
                self._replace_snapshot(
                    game_round,
                    players=self.list_players(game_round.id),
                )
                return game_round
            game_round = self._load_latest_open_round()
            if game_round:
                return game_round
            seed_state = self.seed_state()
            game_round = self._create_round_record(seed_state)
            self.emit_state(refresh_players=False)
            return game_round

    def _create_round_record(self, seed_state):
        GameRound = self.models["GameRound"]
        now = datetime.utcnow()
        game_round = GameRound(
            game_slug=self.slug,
            round_code=str(uuid.uuid4())[:8],
            phase="betting",
            started_at=now,
            betting_ends_at=self.helpers["future_time"](self.betting_duration),
            state_json=json.dumps(seed_state),
        )
        self.db.session.add(game_round)
        self.db.session.commit()
        self.current_round_id = game_round.id
        self._replace_snapshot(game_round, players=[])
        return game_round

    def _update_round_state(
        self,
        game_round,
        *,
        phase=None,
        state=None,
        running=False,
        result=False,
        persist=True,
    ):
        if phase:
            game_round.phase = phase
        if state is not None:
            game_round.state_json = json.dumps(state)
        if running and not game_round.running_started_at:
            game_round.running_started_at = datetime.utcnow()
        if result:
            game_round.result_at = datetime.utcnow()
        if persist:
            self.db.session.commit()
        self._replace_snapshot(game_round)

    def _refund_bet(self, bet, reason):
        self.helpers["adjust_balance"](bet.username, bet.amount, reason=reason)
        bet.status = "refunded"
        bet.payout = bet.amount
        self.db.session.add(
            self.models["BetHistory"](
                username=bet.username,
                game_slug=self.slug,
                round_id=bet.round_id,
                bet_id=bet.id,
                amount=bet.amount,
                payout=bet.amount,
                outcome="refund",
                details_json=json.dumps({"reason": reason, "choice": bet.choice}),
            )
        )

    def place_bet(self, username, amount, choice, extra=None):
        with self.lock, self.app.app_context():
            self.ensure_active_round()
            game_round = self.get_current_round()
            GameBet = self.models["GameBet"]
            if not game_round or game_round.phase != "betting":
                return False, "Betting window is closed."
            if amount <= 0:
                return False, "Bet amount must be greater than zero."
            existing_bet = GameBet.query.filter_by(
                round_id=game_round.id, username=username
            ).first()
            if existing_bet:
                return False, "You already placed a bet this round."
            total_players = (
                self.db.session.query(func.count(GameBet.id))
                .filter(GameBet.round_id == game_round.id)
                .scalar()
            )
            if total_players >= self.max_players:
                return False, "Room is full for this round."
            if not self.validate_choice(choice):
                return False, "Invalid bet selection."
            ok, message = self.helpers["adjust_balance"](
                username, -amount, reason=f"{self.slug}:bet"
            )
            if not ok:
                return False, message

            bet = GameBet(
                round_id=game_round.id,
                game_slug=self.slug,
                username=username,
                amount=amount,
                choice=choice,
                extra_json=json.dumps(extra or {}),
                status="placed",
            )
            self.db.session.add(bet)
            try:
                self.db.session.commit()
            except IntegrityError:
                self.db.session.rollback()
                self.helpers["adjust_balance"](
                    username, amount, reason=f"{self.slug}:bet-refund"
                )
                return False, "You already placed a bet this round."
            except SQLAlchemyError as exc:
                self.db.session.rollback()
                self.helpers["adjust_balance"](
                    username, amount, reason=f"{self.slug}:bet-refund"
                )
                self.app.logger.exception(
                    "Failed to place bet for %s in %s: %s", username, self.slug, exc
                )
                return False, "Unable to place the bet right now. Please try again."
            self.on_bet_placed(game_round, bet)
            self.emit_wallet(username)
            self.emit_state("bet_update")
            return True, "Bet placed successfully."

    def on_bet_placed(self, game_round, bet):
        return None

    def validate_choice(self, choice):
        return not self.choices or choice in self.choices

    def settle_round(self, game_round, result_payload):
        GameBet = self.models["GameBet"]
        BetHistory = self.models["BetHistory"]
        bets = GameBet.query.filter_by(round_id=game_round.id).all()
        for bet in bets:
            payout, outcome, history_details = self.compute_payout(bet, result_payload)
            bet.status = outcome
            bet.payout = payout
            if payout > 0:
                self.helpers["adjust_balance"](
                    bet.username, payout, reason=f"{self.slug}:{outcome}"
                )
                self.emit_wallet(bet.username)
            self.db.session.add(
                BetHistory(
                    username=bet.username,
                    game_slug=self.slug,
                    round_id=game_round.id,
                    bet_id=bet.id,
                    amount=bet.amount,
                    payout=payout,
                    outcome=outcome,
                    details_json=json.dumps(history_details),
                )
            )
        self.db.session.commit()

    def compute_payout(self, bet, result_payload):
        raise NotImplementedError

    def get_public_snapshot(self):
        return deepcopy(self.current_state)

    def get_player_view(self, username):
        snapshot = dict(self.current_state)
        snapshot["wallet_balance"] = self.helpers["get_balance"](username)
        return snapshot

    def cash_out(self, username, auto_target=None):
        return False, "Cash out is not available for this game."

    def sleep_and_emit_countdown(self, duration, phase):
        for remaining in range(duration, 0, -1):
            self.emit_state(
                extra={"countdown": remaining, "phase": phase},
                refresh_players=False,
            )
            time.sleep(1)

    def _seconds_until(self, target_time):
        if not target_time:
            return 0
        remaining = int((target_time - datetime.utcnow()).total_seconds())
        return max(0, remaining)

    def _game_loop(self):
        while True:
            try:
                with self.lock, self.app.app_context():
                    game_round = self.get_current_round() or self._load_latest_open_round()
                    if not game_round:
                        seed_state = self.seed_state()
                        game_round = self._create_round_record(seed_state)
                    elif game_round.phase == "result":
                        self.current_round_id = None
                        continue
                if game_round.phase == "betting":
                    remaining = self._seconds_until(game_round.betting_ends_at)
                    if remaining > 0:
                        self.sleep_and_emit_countdown(remaining, "betting")
                with self.lock, self.app.app_context():
                    game_round = self.get_current_round()
                    if not game_round:
                        game_round = self.ensure_active_round()
                    if game_round.phase == "betting":
                        state = self.safe_json_loads(game_round.state_json)
                        self._update_round_state(
                            game_round, phase="running", state=state, running=True
                        )
                if game_round.phase in ("betting", "running"):
                    self.run_live_round()
                with self.lock, self.app.app_context():
                    game_round = self.get_current_round()
                    if not game_round:
                        game_round = self.ensure_active_round()
                    if game_round.phase != "result":
                        result_payload = self.finish_round(game_round)
                        self._update_round_state(
                            game_round,
                            phase="result",
                            state=result_payload,
                            result=True,
                        )
                        self.settle_round(game_round, result_payload)
                        self.emit_state(extra={"result": result_payload, "phase": "result"})
                with self.lock, self.app.app_context():
                    game_round = self.get_current_round()
                    wait_seconds = self.result_duration
                    if game_round and game_round.result_at:
                        elapsed = int((datetime.utcnow() - game_round.result_at).total_seconds())
                        wait_seconds = max(0, self.result_duration - elapsed)
                if wait_seconds > 0:
                    time.sleep(wait_seconds)
                with self.lock:
                    self.current_round_id = None
                    self.current_state = {"phase": "booting"}
            except Exception as exc:
                self.db.session.rollback()
                self.app.logger.exception("Realtime game loop failed for %s: %s", self.slug, exc)
                time.sleep(2)

    def seed_state(self):
        return {}

    def run_live_round(self):
        time.sleep(self.running_duration)

    def finish_round(self, game_round):
        return self.safe_json_loads(game_round.state_json)
