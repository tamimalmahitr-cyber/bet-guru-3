import json
import random
import time

from .base import BaseRealtimeGame


class CyberDerbyGame(BaseRealtimeGame):
    slug = "cyber-derby"
    title = "Cyber Horse Derby"
    betting_duration = 20
    running_duration = 12
    result_duration = 8
    choices = [str(number) for number in range(1, 9)]

    horse_weights = {
        "1": 1.2,
        "2": 1.1,
        "3": 1.0,
        "4": 0.95,
        "5": 0.9,
        "6": 0.85,
        "7": 0.8,
        "8": 0.75,
    }

    def seed_state(self):
        return {
            "positions": {horse: 0 for horse in self.choices},
            "winner": None,
            "status_text": "Robostable gates open",
        }

    def run_live_round(self):
        finish_line = 100
        winner = None
        while not winner:
            with self.lock, self.app.app_context():
                game_round = self.get_current_round()
                if not game_round:
                    return
                state = json.loads(game_round.state_json or "{}")
                for horse in self.choices:
                    increment = random.uniform(2.5, 8.0) * self.horse_weights[horse]
                    state["positions"][horse] = min(
                        finish_line, round(state["positions"][horse] + increment, 2)
                    )
                    if state["positions"][horse] >= finish_line and not winner:
                        winner = horse
                state["winner"] = winner
                state["status_text"] = "Race in progress"
                self._update_round_state(game_round, state=state)
                self.emit_state(extra={"phase": "running", "positions": state["positions"]})
            time.sleep(0.45)

    def finish_round(self, game_round):
        state = json.loads(game_round.state_json or "{}")
        return {
            "winner": state["winner"],
            "positions": state["positions"],
            "status_text": f"Horse #{state['winner']} wins",
        }

    def compute_payout(self, bet, result_payload):
        won = bet.choice == result_payload["winner"]
        payout = bet.amount * 5 if won else 0
        return payout, ("won" if won else "lost"), result_payload
