from .color_wheel import ColorWheelGame
from .cyber_derby import CyberDerbyGame
from .dice_duel import DiceDuelGame
from .neon_rocket import NeonRocketGame


def build_game_registry(app, socketio, db, models, helpers):
    games = {}
    for game_cls in (
        NeonRocketGame,
        ColorWheelGame,
        CyberDerbyGame,
        DiceDuelGame,
    ):
        engine = game_cls(app, socketio, db, models, helpers)
        games[engine.slug] = engine
    return games
