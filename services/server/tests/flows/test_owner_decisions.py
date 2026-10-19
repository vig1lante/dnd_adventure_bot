"""Бот и REST: решения владельца B6 сквозь слои и ссылка-приглашение.

Три решения принимались для всего блока, а не для одного места, поэтому каждое проверено в обоих
входах в игру (запросы REST и команды бота) и там, где игрок видит результат (сокет):
- создавать игры могут только игроки из списка (`GAME_CREATOR_TG_IDS`, в dev ещё dev-логины);
- игра хранит копию приключения, правка или удаление папки действуют только на новые игры;
- длительности отсчётов берутся из env, а хост может задать свои в пределах.

Ссылка-приглашение бота доводит друга до игры: код из ссылки приходит в Mini App как `start_param`
подписанных данных входа, и по нему друг входит в игру, а хост слышит об этом по сокету.
"""

import re
import shutil
from contextlib import AsyncExitStack

from sqlalchemy import func, select

from dnd_engine.adventure.loader import load_adventure
from dnd_engine.content import load_srd
from dnd_server.adventures import load_catalog
from dnd_server.bot.handlers import NOT_CREATOR_TEXT, BotDeps, open_lobby
from dnd_server.db.models import Game, GamePlayer, User

CREATOR_TG_ID = 700  # из GAME_CREATOR_TG_IDS стенда
OUTSIDER_TG_ID = 701
BOT_LINK = re.compile(r"https://t\.me/dnd_flows_bot\?startapp=([A-Z0-9]{6})")


def bot_deps(stand) -> BotDeps:
    return BotDeps(
        settings=stand.settings,
        sessionmaker=stand.db,
        catalog=stand.app.state.adventures,
        turn_tokens=getattr(stand.app.state, "turn_tokens", None),
    )


async def bot_creates(stand, tg_id: int, name: str):
    """Игра от имени пользователя Telegram так, как её создаёт бот по нажатию кнопки."""
    async with stand.db() as session:
        return await open_lobby(
            session, bot_deps(stand), tg_id=tg_id, name=name, adventure_id="demo-crypt"
        )


async def stored_games(stand) -> list[Game]:
    async with stand.db() as session:
        return list(await session.scalars(select(Game).order_by(Game.created_at)))


async def test_a_user_outside_the_list_is_refused_by_rest_and_by_the_bot_alike(stand):
    outsider = await stand.telegram_player(OUTSIDER_TG_ID, "Посторонний")

    rest = await stand.call(outsider, "POST", "/api/games", json={"adventure_id": "demo-crypt"})
    bot = await bot_creates(stand, OUTSIDER_TG_ID, "Посторонний")

    assert rest.status_code == 403
    assert (await stand.call(outsider, "GET", "/api/me")).json()["can_create_games"] is False
    assert bot.created is False and bot.link is None and bot.text == NOT_CREATOR_TEXT
    # Отказ один и по смыслу: создавать могут только игроки из списка владельца.
    assert "только игроки из списка владельца" in rest.json()["detail"]
    assert "только игроки из списка владельца" in bot.text
    assert await stored_games(stand) == []
    # Отказ в праве не мешает войти в чужую игру по коду: так друзья и играют.
    creator = await stand.telegram_player(CREATOR_TG_ID, "Создатель")
    game = await stand.new_game(creator)
    assert (await stand.join(outsider, game["code"]))["id"] == game["id"]
    stand.assert_clean()


async def test_the_creator_makes_the_same_game_through_rest_and_through_the_bot(stand):
    creator = await stand.telegram_player(CREATOR_TG_ID, "Создатель")
    assert (await stand.call(creator, "GET", "/api/me")).json()["can_create_games"] is True

    rest_game = await stand.new_game(creator)
    reply = await bot_creates(stand, CREATOR_TG_ID, "Создатель")

    assert reply.created is True
    [from_rest, from_bot] = await stored_games(stand)
    assert from_rest.id == rest_game["id"]
    assert (
        BOT_LINK.fullmatch(reply.link) and BOT_LINK.fullmatch(reply.link).group(1) == from_bot.code
    )
    # Тот же человек: бот нашёл пользователя, созданного входом через REST, а не завёл второго.
    async with stand.db() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 1
        hosts = (await session.scalars(select(GamePlayer).where(GamePlayer.is_host))).all()
    assert {host.user_id for host in hosts} == {creator.id}
    # Одинаковая игра: копия приключения, длительности из env, провайдер по умолчанию, лобби.
    for column in (
        "adventure_id",
        "adventure_data",
        "collect_seconds",
        "hurry_seconds",
        "camp_seconds",
        "provider",
        "model",
        "phase",
        "host_user_id",
    ):
        assert getattr(from_rest, column) == getattr(from_bot, column), column
    assert from_rest.code != from_bot.code
    assert [g["id"] for g in (await stand.call(creator, "GET", "/api/games")).json()] == [
        from_bot.id,
        from_rest.id,
    ]
    stand.assert_clean()


async def test_countdowns_default_to_the_environment_in_both_entrances_and_stay_in_limits(stand):
    creator = await stand.telegram_player(CREATOR_TG_ID, "Создатель")
    env = stand.settings
    defaults = (env.collect_seconds, env.hurry_seconds, env.camp_seconds)

    rest = await stand.new_game(creator)
    reply = await bot_creates(stand, CREATOR_TG_ID, "Создатель")

    [_, from_bot] = await stored_games(stand)
    assert (rest["collect_seconds"], rest["hurry_seconds"], rest["camp_seconds"]) == defaults
    assert (from_bot.collect_seconds, from_bot.hurry_seconds, from_bot.camp_seconds) == defaults
    assert reply.created
    # Значения из env лежат в пределах, которые REST разрешает хосту: игру с такими же числами
    # хост мог бы создать и сам.
    explicit = await stand.new_game(
        creator, collect_seconds=defaults[0], hurry_seconds=defaults[1], camp_seconds=defaults[2]
    )
    assert (explicit["collect_seconds"], explicit["hurry_seconds"], explicit["camp_seconds"]) == (
        defaults
    )
    # Свои значения хост задаёт только в пределах: 30-600, 15-300 и 30-900 секунд.
    for body in (
        {"collect_seconds": 29},
        {"collect_seconds": 601},
        {"hurry_seconds": 14},
        {"hurry_seconds": 301},
        {"camp_seconds": 29},
        {"camp_seconds": 901},
    ):
        response = await stand.call(
            creator, "POST", "/api/games", json={"adventure_id": "demo-crypt", **body}
        )
        assert response.status_code == 422, body
    assert len(await stored_games(stand)) == 3
    stand.assert_clean()


async def test_a_game_keeps_its_adventure_for_rest_and_the_socket_while_new_games_follow_the_folder(
    stand, content_dir
):
    host, friend = [await stand.dev_player(n) for n in (1, 2)]
    first = await stand.new_game(host)
    original = load_adventure(content_dir / "demo-crypt")

    # Папку исправили, и каталог перечитан (как после перезапуска): новая игра получит новый текст.
    manifest = content_dir / "demo-crypt" / "adventure.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            original.title, "Склеп, исправленный автором", 1
        ),
        encoding="utf-8",
    )
    stand.app.state.adventures = load_catalog(content_dir, load_srd())
    second = await stand.new_game(host)

    assert first["adventure_title"] == original.title
    assert second["adventure_title"] == "Склеп, исправленный автором"
    for game, title in ((first, original.title), (second, "Склеп, исправленный автором")):
        assert (await stand.game(host, game["id"]))["adventure_title"] == title
        assert (await stand.snapshot(game["id"], host))["game"]["adventure_title"] == title

    # Папку убрали совсем: старые игры идут дальше по своей копии, новую игру создать нельзя.
    shutil.rmtree(content_dir / "demo-crypt")
    stand.app.state.adventures = load_catalog(content_dir, load_srd())
    await stand.join(friend, first["code"])
    for game, title in ((first, original.title), (second, "Склеп, исправленный автором")):
        assert (await stand.snapshot(game["id"], host))["game"]["adventure_title"] == title
    gone = await stand.call(host, "POST", "/api/games", json={"adventure_id": "demo-crypt"})
    assert gone.status_code == 404
    assert (await bot_creates(stand, CREATOR_TG_ID, "Создатель")).created is False
    async with stand.db() as session:
        copies = {g.id: g.adventure_data["title"] for g in await session.scalars(select(Game))}
    assert copies == {first["id"]: original.title, second["id"]: "Склеп, исправленный автором"}
    stand.assert_clean()


async def test_the_invite_link_of_the_bot_brings_a_friend_in_through_the_mini_app(stand):
    reply = await bot_creates(stand, CREATOR_TG_ID, "Создатель")
    code = BOT_LINK.fullmatch(reply.link).group(1)
    host = await stand.telegram_player(CREATOR_TG_ID, "Создатель")
    [game] = (await stand.call(host, "GET", "/api/games")).json()

    async with AsyncExitStack() as stack:
        listening = await stand.watch(stack, game["id"], host)
        # Друг открывает Mini App по ссылке: Telegram кладёт код в start_param данных входа.
        login = await stand.telegram_login(OUTSIDER_TG_ID, "Друг", start_param=code)
        friend = stand.player_from(login)
        joined = await stand.join(friend, login["start_param"])
        [event] = await listening.drain()

    assert login["start_param"] == code == game["code"]
    assert [p["name"] for p in joined["players"]] == ["Создатель", "Друг"]
    assert event["type"] == "lobby" and event["game"] == joined
    # Друг не из списка создателей: войти он может, начать свою игру — нет.
    assert (await stand.call(friend, "GET", "/api/me")).json()["can_create_games"] is False
    stand.assert_clean()
