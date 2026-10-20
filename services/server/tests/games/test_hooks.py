"""Заглушки и вызовы лобби (`games.hooks`): сбой слушателя пишется в журнал и запроса не касается.

Оба вызова идут после коммита, поэтому исключение наружу не выходит. В журнал сервера оно попадает
без значений входа pydantic: в тексте ошибки лежат куски данных игры.
"""

import logging
from typing import Any

from dnd_server.games import hooks as hooks_module
from dnd_server.games.hooks import run_game_started, send_to_game

GAME = "game-1"


async def test_a_broken_broadcast_is_logged_without_the_input_of_the_error(caplog, leak):
    class BrokenBroadcaster:
        async def send(self, game_id: str, payload: dict[str, Any], audience: str = "all") -> None:
            raise leak.error

    with caplog.at_level(logging.ERROR, logger=hooks_module.__name__):
        await send_to_game(BrokenBroadcaster(), GAME, {"type": "phase"})  # не бросает

    [record] = caplog.records
    assert record.exc_info is None  # трассировку формирует сервер, а не logging
    assert GAME in record.getMessage() and "ValidationError" in record.getMessage()
    assert leak.value not in caplog.text and "input_value" not in caplog.text


async def test_a_broken_start_hook_is_logged_without_the_input_of_the_error(caplog, leak):
    class BrokenHooks:
        async def on_game_started(self, game_id: str) -> None:
            raise leak.error

    with caplog.at_level(logging.ERROR, logger=hooks_module.__name__):
        await run_game_started(BrokenHooks(), GAME)  # не бросает

    [record] = caplog.records
    assert record.exc_info is None
    assert GAME in record.getMessage() and "ValidationError" in record.getMessage()
    assert leak.value not in caplog.text and "input_value" not in caplog.text
