import uvicorn

# Только localhost: наружу сервер выставляет туннель cloudflared, а не привязка к интерфейсу.
HOST = "127.0.0.1"
PORT = 8000


def main() -> None:
    # Фабрика без аргументов: create_app сам берёт кэшированные настройки.
    # Журнал доступа выключен: путь /internal/dm-tools/<токен>/… содержит одноразовый токен хода,
    # а токены в журналы не пишутся.
    uvicorn.run("dnd_server.app:create_app", factory=True, host=HOST, port=PORT, access_log=False)


if __name__ == "__main__":
    main()
