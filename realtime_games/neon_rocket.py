import json
import math
import random
import time

from .base import BaseRealtimeGame


class NeonRocketGame(BaseRealtimeGame):
    slug = "neon-rocket"
    title = "Neon Rocket"
    betting_duration = 12
    running_duration = 18
    result_duration = 6
    supports_cashout = True
    choices = ["launch"]

    def seed_state(self):
        crash_point = round(random.uniform(1.2, 8.5), 2)
        return {
            "crash_point": crash_point,
            "multiplier": 1.0,
            "status_text": "Launch bay open",
        }

    def run_live_round(self):
        started_at = time.time()
        crashed = False
        while time.time() - started_at < self.running_duration:
            elapsed = time.time() - started_at
            multiplier = round(1 + 0.08 * elapsed + 0.025 * math.exp(elapsed / 4), 2)
            with self.lock, self.app.app_context():
                game_round = self.get_current_round()
                if not game_round:
                    return
                state = json.loads(game_round.state_json or "{}")
                state["multiplier"] = multiplier
                state["elapsed"] = round(elapsed, 2)
                state["status_text"] = "Rocket climbing"
                self._update_round_state(game_round, state=state)
                self.emit_state(extra={"phase": "running", "live_multiplier": multiplier})
                if multiplier >= state["crash_point"]:
                    state["multiplier"] = state["crash_point"]
                    state["status_text"] = "Rocket crashed"
                    self._update_round_state(game_round, state=state)
                    crashed = True
                    break
            time.sleep(0.25)
        if not crashed:
            with self.lock, self.app.app_context():
                game_round = self.get_current_round()
                if game_round:
                    state = json.loads(game_round.state_json or "{}")
                    state["status_text"] = "Rocket escaped orbit"
                    self._update_round_state(game_round, state=state)

    def cash_out(self, username, auto_target=None):
        with self.lock, self.app.app_context():
            game_round = self.get_current_round()
            GameBet = self.models["GameBet"]
            if not game_round or game_round.phase != "running":
                return False, "Cash out is only available while the rocket is flying."
            bet = GameBet.query.filter_by(
                round_id=game_round.id, username=username, status="placed"
            ).first()
            if not bet:
                return False, "No active rocket bet found."
            state = json.loads(game_round.state_json or "{}")
            current_multiplier = state.get("multiplier", 1.0)
            crash_point = state.get("crash_point", 1.0)
            if current_multiplier >= crash_point:
                return False, "Too late. The rocket already crashed."
            payout = int(round(bet.amount * current_multiplier))
            bet.status = "cashed_out"
            bet.payout = payout
            bet.cashout_multiplier = current_multiplier
            self.db.session.add(
                self.models["BetHistory"](
                    username=bet.username,
                    game_slug=self.slug,
                    round_id=game_round.id,
                    bet_id=bet.id,
                    amount=bet.amount,
                    payout=payout,
                    outcome="cashed_out",
                    details_json=json.dumps(
                        {
                            "cashout_multiplier": current_multiplier,
                            "crash_point": crash_point,
                        }
                    ),
                )
            )
            self.db.session.commit()
            self.helpers["adjust_balance"](
                username, payout, reason=f"{self.slug}:cashed_out"
            )
            self.emit_wallet(username)
            self.emit_state("cashout_update")
            return True, f"Cashed out at {current_multiplier}x."

    def finish_round(self, game_round):
        state = json.loads(game_round.state_json or "{}")
        return {
            "crash_point": state.get("crash_point", 1.0),
            "multiplier": state.get("crash_point", 1.0),
            "status_text": "Round settled",
        }

    def compute_payout(self, bet, result_payload):
        if bet.status == "cashed_out":
            return 0, "cashed_out", {
                "cashout_multiplier": bet.cashout_multiplier,
                "crash_point": result_payload["crash_point"],
            }
        return 0, "lost", {
            "crash_point": result_payload["crash_point"],
            "cashout_multiplier": bet.cashout_multiplier,
        }
