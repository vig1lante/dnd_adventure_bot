"""Клиент сокета игры в тестах — так же, как его открывает Mini App.

Браузер не умеет слать заголовки при открытии сокета, но передаёт подпротоколы
(`Sec-WebSocket-Protocol`). Поэтому токен сессии идёт вторым подпротоколом после постоянного имени
`dnd-game.v1`, а адрес сокета токена не содержит. Модуль лежит рядом с `conftest.py`, поэтому тесты
из подпапок берут его как `from game_socket import …`.
"""

from typing import Any

from websockets.asyncio.client import connect

# Имя подпротокола сокета игры — договорённость с Mini App, поэтому здесь оно записано строкой,
# а не взято из кода сервера.
SOCKET_PROTOCOL = "dnd-game.v1"


def open_game_socket(url: str, token: str | None, **options: Any) -> connect:
    """`connect` к сокету игры с подпротоколами `[dnd-game.v1, токен]`; без токена — без них."""
    subprotocols = None if token is None else [SOCKET_PROTOCOL, token]
    return connect(url, subprotocols=subprotocols, **options)
