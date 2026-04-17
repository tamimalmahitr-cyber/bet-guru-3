import json
import random
import time

from .base import BaseRealtimeGame


class ColorWheelGame(BaseRealtimeGame):
    slug = "color-wheel"
    title = "Color Wheel Fortune"
    betting_duration = 45
    running_duration = 8
    result_duration = 7
    choices = ["red", "blue", "green", "gold"]

    color_weights = [0.4, 0.3, 0.2, 0.1]
    color_multipliers = {"red": 2, "blue": 2, "green": 3, "gold": 10}

    def seed_state(self):
        winning_color = random.choices(self.choices, weights=self.color_weights, k=1)[0]
        return {"winning_color": winning_color, "wheel_angle": 0, "status_text": "Place your color bet"}

    def run_live_round(self):
        for step in range(1, 17):
            angle = step * 45
            with self.lock, self.app.app_context():
                game_round = self.get_current_round()
                if not game_round:
                    return
                state = json.loads(game_round.state_json or "{}")
                state["wheel_angle"] = angle
                state["status_text"] = "Wheel spinning"
                self._update_round_state(game_round, state=state)
                self.emit_state(extra={"phase": "running", "wheel_angle": angle})
            time.sleep(self.running_duration / 16)

    def finish_round(self, game_round):
        state = json.loads(game_round.state_json or "{}")
        return {
            "winning_color": state["winning_color"],
            "wheel_angle": 720 + self.choices.index(state["winning_color"]) * 90,
            "status_text": f"{state['winning_color'].title()} wins",
        }

    def compute_payout(self, bet, result_payload):
        won = bet.choice == result_payload["winning_color"]
        payout = bet.amount * self.color_multipliers[bet.choice] if won else 0
        return payout, ("won" if won else "lost"), result_payload
