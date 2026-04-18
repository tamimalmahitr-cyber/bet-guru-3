import json
import random
import time

from .base import BaseRealtimeGame


class DiceDuelGame(BaseRealtimeGame):
    slug = "dice-duel"
    title = "Dice Duel"
    betting_duration = 20
    running_duration = 5
    result_duration = 5
    choices = ["low", "high"]

    def seed_state(self):
        die_one = random.randint(1, 6)
        die_two = random.randint(1, 6)
        return {
            "dice": [die_one, die_two],
            "sum": die_one + die_two,
            "status_text": "Choose high or low",
        }

    def run_live_round(self):
        for _ in range(10):
            with self.lock, self.app.app_context():
                game_round = self.get_current_round()
                if not game_round:
                    return
                state = self.safe_json_loads(game_round.state_json)
                state["dice"] = [random.randint(1, 6), random.randint(1, 6)]
                state["status_text"] = "Dice rolling"
                self._update_round_state(game_round, state=state, persist=False)
                self.emit_state(
                    extra={"phase": "running", "dice": state["dice"]},
                    refresh_players=False,
                )
            time.sleep(self.running_duration / 10)

    def finish_round(self, game_round):
        state = self.safe_json_loads(game_round.state_json)
        total = state["sum"]
        if 2 <= total <= 6:
            outcome = "low"
        elif 8 <= total <= 12:
            outcome = "high"
        else:
            outcome = "house"
        return {
            "dice": state["dice"],
            "sum": total,
            "winning_side": outcome,
            "status_text": f"Total {total} -> {outcome.title()}",
        }

    def compute_payout(self, bet, result_payload):
        won = bet.choice == result_payload["winning_side"]
        payout = bet.amount * 2 if won else 0
        return payout, ("won" if won else "lost"), result_payload
