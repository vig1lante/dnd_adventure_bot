"""Вход и сессии (план B6.2): initData Telegram, dev-вход, токен, `/api/me`, `/api/config`.

Подпись initData строит сам тест известным токеном бота по алгоритму Telegram, настоящего Telegram
нет. Приложение работает на настоящей базе SQLite после миграций (фикстуры `settings`, `db`,
`client` из `tests/conftest.py`); `settings` здесь дополнена токеном бота и списком создателей игр.
"""

import asyncio
import hashlib
import hmac
import json
import time
import traceback
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode

import httpx
import itsdangerous.timed
import pytest
import uvicorn
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import func, select
from starlette.websockets import WebSocket

from dnd_server.app import create_app
from dnd_server.auth.deps import (
    can_create_games,
    find_or_create_user,
    is_game_creator_tg_id,
    require_game_creator,
    user_for_token,
    websocket_user,
)
from dnd_server.auth.session import SessionSigner, WeakSessionSecret
from dnd_server.auth.telegram import MAX_CLOCK_SKEW_S, InvalidInitData, validate_init_data
from dnd_server.config import Settings
from dnd_server.db.models import User

BOT_TOKEN = "123456:TEST-bot-token-for-signatures"
SESSION_SECRET = "test-session-secret-0123456789-abcdefghij"
CREATOR_ID = 700
OUTSIDER_ID = 701
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
SECRETS = (BOT_TOKEN, SESSION_SECRET)


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """Dev-настройки из conftest плюс токен бота, секрет сессий и один создатель игр."""
    return settings.model_copy(
        update={
            "telegram_bot_token": SecretStr(BOT_TOKEN),
            "session_secret": SecretStr(SESSION_SECRET),
            "game_creator_tg_ids": (CREATOR_ID,),
        }
    )


def prod_settings(settings: Settings, **changes) -> Settings:
    """Prod на той же базе: нужные настройки заданы, `changes` их переопределяют."""
    values = {
        "app_env": "prod",
        "database_url": settings.database_url,
        "telegram_bot_token": BOT_TOKEN,
        "session_secret": SESSION_SECRET,
        "game_creator_tg_ids": (CREATOR_ID,),
    }
    return Settings(**{**values, **changes})


@asynccontextmanager
async def client_for(app_settings: Settings):
    app = create_app(app_settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        yield http
    await app.state.engine.dispose()


# --- построение initData (алгоритм из документации Telegram) ---


def sign_fields(fields: dict[str, str], bot_token: str = BOT_TOKEN) -> str:
    """Строка initData: поля, отсортированные `ключ=значение` через перевод строки, и их HMAC."""
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})


def init_data(
    *,
    user_id: int = CREATOR_ID,
    first_name: str = "Аня",
    username: str | None = "anya",
    start_param: str | None = None,
    auth_date: datetime | None = None,
    bot_token: str = BOT_TOKEN,
    **extra: str,
) -> str:
    user = {"id": user_id, "first_name": first_name}
    if username:
        user["username"] = username
    moment = auth_date or datetime.now(UTC)
    fields = {"auth_date": str(int(moment.timestamp())), "user": json.dumps(user), **extra}
    if start_param:
        fields["start_param"] = start_param
    return sign_fields(fields, bot_token)


def hash_of(raw: str) -> str:
    return dict(parse_qsl(raw))["hash"]


def with_hash_changed(raw: str) -> str:
    fields = dict(parse_qsl(raw))
    fields["hash"] = ("0" if fields["hash"][0] != "0" else "1") + fields["hash"][1:]
    return urlencode(fields)


def with_field_changed(raw: str, name: str, value: str) -> str:
    """Подмена поля уже подписанной строки: хеш остаётся прежним."""
    return urlencode({**dict(parse_qsl(raw)), name: value})


# Hash не из шестнадцатеричных знаков: кириллица, латиница с диакритикой и эмодзи (уже закодированы
# в процентах, как приходят в заголовке).
NON_ASCII_HASHES = [
    pytest.param("%D0%B0%D0%B1", id="кириллица"),
    pytest.param("%C3%A9", id="латиница с диакритикой"),
    pytest.param("%F0%9F%98%80", id="эмодзи"),
]


def with_hash_replaced(raw: str, encoded_hash: str) -> str:
    """Строка initData, у которой hash заменён готовой (уже закодированной) строкой."""
    fields = [(key, value) for key, value in parse_qsl(raw) if key != "hash"]
    return f"{urlencode(fields)}&hash={encoded_hash}"


# --- validate_init_data ---


def validate(raw: str, **options):
    return validate_init_data(raw, BOT_TOKEN, now=NOW, **options)


def test_a_valid_signature_gives_the_telegram_user():
    raw = init_data(start_param="ABC123", auth_date=NOW)

    user = validate(raw)

    assert (user.id, user.first_name, user.username, user.start_param) == (
        CREATOR_ID,
        "Аня",
        "anya",
        "ABC123",
    )


def test_username_and_start_param_are_optional():
    user = validate(init_data(username=None, auth_date=NOW))

    assert (user.username, user.start_param) == (None, None)


def test_the_signature_field_of_new_telegram_versions_is_part_of_the_checked_string():
    # Telegram добавляет к initData поле `signature`; в строку проверки входят все поля, кроме hash.
    assert validate(init_data(auth_date=NOW, signature="abc-DEF_123")).id == CREATOR_ID


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(with_hash_changed, id="чужой hash"),
        pytest.param(
            lambda raw: with_field_changed(raw, "user", '{"id": 1, "first_name": "Хэл"}'),
            id="подменён пользователь",
        ),
        pytest.param(
            lambda raw: with_field_changed(raw, "start_param", "OTHER"), id="подменён start_param"
        ),
    ],
)
def test_forged_data_is_rejected(tamper):
    raw = init_data(start_param="ABC123", auth_date=NOW)

    with pytest.raises(InvalidInitData):
        validate(tamper(raw))


def test_data_signed_with_another_bot_token_is_rejected():
    with pytest.raises(InvalidInitData):
        validate(init_data(auth_date=NOW, bot_token="999999:ANOTHER-bot"))


@pytest.mark.parametrize("raw", ["", "abc", "hash=abc", "user=%7B%7D&auth_date=1"])
def test_garbage_and_data_without_a_hash_are_rejected(raw):
    with pytest.raises(InvalidInitData):
        validate(raw)


@pytest.mark.parametrize("encoded_hash", NON_ASCII_HASHES)
def test_a_non_ascii_hash_is_a_wrong_signature_not_a_server_error(encoded_hash):
    # `hmac.compare_digest` бросает TypeError на не-ASCII строке: для клиента это такая же неверная
    # подпись, как и любая другая, а не сбой сервера.
    raw = with_hash_replaced(init_data(auth_date=NOW), encoded_hash)

    with pytest.raises(InvalidInitData):
        validate(raw)


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"auth_date": "1"}, id="нет пользователя"),
        pytest.param({"user": '{"id": 1, "first_name": "Аня"}'}, id="нет auth_date"),
        pytest.param({"auth_date": "1", "user": "{сломанный json"}, id="сломан json пользователя"),
        pytest.param(
            {"auth_date": "вчера", "user": '{"id": 1, "first_name": "Аня"}'}, id="дата-текст"
        ),
    ],
)
def test_correctly_signed_but_incomplete_data_is_rejected(fields):
    with pytest.raises(InvalidInitData):
        validate(sign_fields(fields))


def test_expired_auth_date_is_rejected():
    default_age = int(timedelta(days=1).total_seconds())

    assert validate(init_data(auth_date=NOW - timedelta(seconds=default_age))).id == CREATOR_ID
    with pytest.raises(InvalidInitData):
        validate(init_data(auth_date=NOW - timedelta(seconds=default_age + 1)))


def test_max_age_is_a_parameter():
    raw = init_data(auth_date=NOW - timedelta(seconds=61))

    with pytest.raises(InvalidInitData):
        validate(raw, max_age_s=60)
    assert validate(raw, max_age_s=61).id == CREATOR_ID


def test_auth_date_from_the_future_is_rejected_beyond_the_clock_skew():
    # Допуск на расхождение часов сервера и Telegram: пять минут. Дальше — подозрительно.
    assert MAX_CLOCK_SKEW_S == 300
    assert validate(init_data(auth_date=NOW + timedelta(seconds=MAX_CLOCK_SKEW_S))).id == CREATOR_ID
    with pytest.raises(InvalidInitData):
        validate(init_data(auth_date=NOW + timedelta(seconds=MAX_CLOCK_SKEW_S + 1)))


def test_a_rejection_does_not_repeat_the_hash_the_token_or_the_data():
    raw = with_hash_changed(init_data(auth_date=NOW))

    with pytest.raises(InvalidInitData) as refusal:
        validate(raw)

    story = "".join(traceback.format_exception(refusal.value)) + repr(refusal.value)
    for secret in (hash_of(raw), BOT_TOKEN, raw, "anya"):
        assert secret not in story


# --- POST /api/auth/telegram ---


async def telegram_login(client: httpx.AsyncClient, raw: str | None, scheme: str = "tma"):
    headers = {"Authorization": f"{scheme} {raw}"} if raw is not None else {}
    return await client.post("/api/auth/telegram", headers=headers)


async def users_in(db) -> list[User]:
    async with db() as session:
        return list((await session.scalars(select(User))).all())


async def test_telegram_login_creates_the_user_and_gives_a_working_token(client, db):
    response = await telegram_login(client, init_data(start_param="ABC123"))

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"token", "user", "start_param"}
    assert body["start_param"] == "ABC123"
    assert body["user"]["name"] == "Аня"
    [stored] = await users_in(db)
    assert (stored.tg_id, stored.dev_login, stored.name) == (CREATOR_ID, None, "Аня")
    assert body["user"]["id"] == stored.id
    me = await client.get("/api/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200
    assert me.json() == body["user"]


async def test_telegram_login_without_start_param_gives_null(client):
    response = await telegram_login(client, init_data())

    assert response.json()["start_param"] is None


async def test_repeated_telegram_login_is_the_same_user(client, db):
    first = (await telegram_login(client, init_data())).json()
    second = (await telegram_login(client, init_data())).json()

    assert first["user"]["id"] == second["user"]["id"]
    assert len(await users_in(db)) == 1


async def test_a_changed_telegram_name_is_stored_on_the_next_login(client, db):
    await telegram_login(client, init_data(first_name="Аня"))
    body = (await telegram_login(client, init_data(first_name="Анна"))).json()

    [stored] = await users_in(db)
    assert stored.name == "Анна"
    assert body["user"]["name"] == "Анна"


async def test_different_telegram_users_are_different_users(client, db):
    first = (await telegram_login(client, init_data(user_id=CREATOR_ID))).json()
    second = (await telegram_login(client, init_data(user_id=OUTSIDER_ID))).json()

    assert first["user"]["id"] != second["user"]["id"]
    assert len(await users_in(db)) == 2


async def test_parallel_first_logins_of_one_user_make_one_row_and_no_server_error(client, db):
    # Оба запроса не находят пользователя и вставляют его; уникальность tg_id отвергает второго,
    # и тот берёт строку победителя вместо ответа 500.
    raw = init_data()

    responses = await asyncio.gather(*(telegram_login(client, raw) for _ in range(6)))

    assert [response.status_code for response in responses] == [200] * 6
    assert len({response.json()["user"]["id"] for response in responses}) == 1
    assert len(await users_in(db)) == 1


@pytest.mark.parametrize(
    "request_headers",
    [
        pytest.param(None, id="нет заголовка"),
        pytest.param({"Authorization": "tma"}, id="нет данных"),
        pytest.param({"Authorization": "tma "}, id="пустые данные"),
        pytest.param({"Authorization": "Bearer abc"}, id="другая схема"),
        pytest.param({"Authorization": "tma not-init-data"}, id="мусор"),
    ],
)
async def test_missing_or_broken_header_is_401(client, db, request_headers):
    response = await client.post("/api/auth/telegram", headers=request_headers)

    assert response.status_code == 401
    assert isinstance(response.json()["detail"], str)
    assert await users_in(db) == []


@pytest.mark.parametrize(
    "make_raw",
    [
        pytest.param(lambda: with_hash_changed(init_data()), id="поддельный hash"),
        pytest.param(lambda: init_data(bot_token="999999:ANOTHER-bot"), id="чужой бот"),
        pytest.param(
            lambda: init_data(auth_date=datetime.now(UTC) - timedelta(days=2)), id="просрочен"
        ),
        pytest.param(
            lambda: init_data(auth_date=datetime.now(UTC) + timedelta(hours=1)), id="из будущего"
        ),
    ],
)
async def test_invalid_init_data_is_401_without_a_user_or_a_leak(client, db, make_raw, caplog):
    raw = make_raw()

    with caplog.at_level("DEBUG"):
        response = await telegram_login(client, raw)

    assert response.status_code == 401
    assert await users_in(db) == []
    assert "Вход через Telegram отклонён" in caplog.text  # причина видна владельцу в журнале
    seen = response.text + str(response.headers) + caplog.text
    for secret in (*SECRETS, hash_of(raw), raw):
        assert secret not in seen


@pytest.mark.parametrize("encoded_hash", NON_ASCII_HASHES)
async def test_a_non_ascii_hash_is_401_and_leaves_no_traceback_in_the_log(
    client, db, encoded_hash, caplog
):
    raw = with_hash_replaced(init_data(), encoded_hash)

    with caplog.at_level("DEBUG"):
        response = await telegram_login(client, raw)

    assert response.status_code == 401
    assert isinstance(response.json()["detail"], str)
    assert await users_in(db) == []
    # Любой посторонний мог бы засорять журнал трассировками на каждый такой запрос.
    assert [record for record in caplog.records if record.exc_info] == []
    assert "Traceback" not in caplog.text


async def test_a_successful_login_leaves_the_session_token_out_of_the_log(client, caplog):
    with caplog.at_level("DEBUG"):
        response = await telegram_login(client, init_data())

    assert response.json()["token"] not in caplog.text
    for secret in SECRETS:
        assert secret not in caplog.text


async def test_telegram_login_without_a_bot_token_is_a_clear_refusal(settings, db):
    async with client_for(settings.model_copy(update={"telegram_bot_token": None})) as client:
        response = await telegram_login(client, init_data())

    assert response.status_code == 503
    assert "Telegram" in response.json()["detail"]
    for secret in (*SECRETS, hash_of(init_data())):
        assert secret not in response.text
    assert await users_in(db) == []


# --- POST /api/auth/dev ---


async def dev_login(client: httpx.AsyncClient, login: str):
    return await client.post("/api/auth/dev", json={"login": login})


@pytest.mark.parametrize("login", ["dev-1", "dev-3", "dev-6"])
async def test_dev_login_gives_a_token_in_dev(client, db, login):
    response = await dev_login(client, login)

    assert response.status_code == 200
    body = response.json()
    assert body["start_param"] is None
    assert body["user"]["name"] == login
    [stored] = await users_in(db)
    assert (stored.dev_login, stored.tg_id) == (login, None)
    me = await client.get("/api/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.json()["id"] == stored.id


async def test_repeated_dev_login_is_the_same_user(client, db):
    first = (await dev_login(client, "dev-2")).json()
    second = (await dev_login(client, "dev-2")).json()

    assert first["user"]["id"] == second["user"]["id"]
    assert len(await users_in(db)) == 1


async def test_dev_logins_are_different_users(client, db):
    ids = {
        (await dev_login(client, f"dev-{number}")).json()["user"]["id"] for number in range(1, 7)
    }

    assert len(ids) == 6


async def test_parallel_first_dev_logins_make_one_row(client, db):
    responses = await asyncio.gather(*(dev_login(client, "dev-4") for _ in range(6)))

    assert [response.status_code for response in responses] == [200] * 6
    assert len(await users_in(db)) == 1


@pytest.mark.parametrize("login", ["dev-0", "dev-7", "dev-10", "Dev-1", "dev-1 ", "admin", ""])
async def test_unknown_dev_login_is_422_and_makes_no_user(client, db, login):
    response = await dev_login(client, login)

    assert response.status_code == 422
    # Общий контракт блока: `detail` — строка на русском, а не список ошибок FastAPI с эхом ввода.
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    if login:
        assert login not in detail
    assert await users_in(db) == []


@pytest.mark.parametrize("body", [{}, {"login": None}, {"login": 1}, {"user": "dev-1"}])
async def test_dev_login_needs_a_text_login(client, body):
    response = await client.post("/api/auth/dev", json=body)

    assert response.status_code == 422


async def test_dev_login_does_not_exist_in_prod(settings, db):
    async with client_for(prod_settings(settings)) as client:
        response = await dev_login(client, "dev-1")
        wrong_login = await dev_login(client, "admin")
        no_body = await client.post("/api/auth/dev")
        wrong_method = await client.get("/api/auth/dev")

    # Маршрута нет вовсе: ни по форме тела, ни по логину не видно, что он когда-то был.
    assert [response.status_code, wrong_login.status_code, no_body.status_code] == [404, 404, 404]
    assert wrong_method.status_code == 404
    assert await users_in(db) == []


# --- GET /api/me и сессионный токен ---


async def token_for(client, login: str = "dev-1") -> str:
    return (await dev_login(client, login)).json()["token"]


async def test_me_without_a_token_is_401(client):
    response = await client.get("/api/me")

    assert response.status_code == 401
    assert isinstance(response.json()["detail"], str)
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "authorization", ["Bearer", "Bearer ", "Bearer garbage", "Basic abc", "garbage", "tma abc"]
)
async def test_me_with_a_wrong_authorization_is_401(client, authorization):
    response = await client.get("/api/me", headers={"Authorization": authorization})

    assert response.status_code == 401


async def test_me_with_a_token_signed_by_another_secret_is_401(client):
    token = SessionSigner("z" * 40).issue("any-user")

    response = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


async def test_me_with_a_token_of_a_user_who_is_gone_is_401(client, db):
    token = SessionSigner(SESSION_SECRET).issue("no-such-user")

    response = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


async def test_me_with_an_expired_token_is_401(client, monkeypatch):
    token = await token_for(client)
    later = time.time() + 7 * 24 * 3600 + 5
    monkeypatch.setattr(itsdangerous.timed, "time", SimpleNamespace(time=lambda: later))

    response = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


async def test_the_token_works_after_a_restart_of_the_app(settings, db):
    # Токен самодостаточен: новое приложение с теми же секретом и базой его принимает.
    async with client_for(settings) as first:
        token = await token_for(first, "dev-5")
    async with client_for(settings) as second:
        response = await second.get("/api/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["name"] == "dev-5"


# --- кто может создавать игры ---


async def me_of(client, token: str) -> dict:
    return (await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})).json()


async def test_a_creator_from_the_list_may_create_games(client):
    login = await telegram_login(client, init_data(user_id=CREATOR_ID))

    assert login.json()["user"]["can_create_games"] is True
    assert (await me_of(client, login.json()["token"]))["can_create_games"] is True


async def test_a_telegram_user_outside_the_list_may_not(client):
    login = await telegram_login(client, init_data(user_id=OUTSIDER_ID))

    assert login.json()["user"]["can_create_games"] is False
    assert (await me_of(client, login.json()["token"]))["can_create_games"] is False


async def test_dev_logins_may_create_games_in_dev(client):
    login = await dev_login(client, "dev-1")

    assert login.json()["user"]["can_create_games"] is True
    assert (await me_of(client, login.json()["token"]))["can_create_games"] is True


async def test_nobody_creates_games_in_prod_while_the_list_is_empty(settings, db):
    async with client_for(prod_settings(settings, game_creator_tg_ids=())) as client:
        login = await telegram_login(client, init_data(user_id=CREATOR_ID))

    assert login.status_code == 200
    assert login.json()["user"]["can_create_games"] is False


def test_the_creator_check_for_a_telegram_id_alone():
    # Бот (`/newgame`) проверяет id до того, как заведёт пользователя.
    configured = Settings(game_creator_tg_ids=(CREATOR_ID,))

    assert is_game_creator_tg_id(configured, CREATOR_ID) is True
    assert is_game_creator_tg_id(configured, OUTSIDER_ID) is False
    assert is_game_creator_tg_id(Settings(), CREATOR_ID) is False


@pytest.mark.parametrize(
    ("app_env", "user", "expected"),
    [
        pytest.param("prod", User(tg_id=CREATOR_ID, name="Аня"), True, id="prod: из списка"),
        pytest.param("prod", User(tg_id=OUTSIDER_ID, name="Боря"), False, id="prod: вне списка"),
        pytest.param("dev", User(tg_id=CREATOR_ID, name="Аня"), True, id="dev: из списка"),
        pytest.param("dev", User(tg_id=OUTSIDER_ID, name="Боря"), False, id="dev: вне списка"),
        pytest.param("dev", User(dev_login="dev-1", name="d"), True, id="dev: dev-логин"),
        # Строка dev-входа в базе, а сервер уже в prod: прав она не даёт.
        pytest.param("prod", User(dev_login="dev-1", name="d"), False, id="prod: dev-логин"),
    ],
)
def test_can_create_games(app_env, user, expected):
    settings = Settings(app_env=app_env, game_creator_tg_ids=(CREATOR_ID,))

    assert can_create_games(settings, user) is expected


async def test_require_game_creator_passes_a_creator_and_gives_403_to_the_rest():
    settings = Settings(game_creator_tg_ids=(CREATOR_ID,))
    creator = User(tg_id=CREATOR_ID, name="Аня")

    assert await require_game_creator(creator, settings) is creator
    with pytest.raises(HTTPException) as refusal:
        await require_game_creator(User(tg_id=OUTSIDER_ID, name="Боря"), settings)
    assert refusal.value.status_code == 403
    assert isinstance(refusal.value.detail, str)


# --- GET /api/config ---


async def test_config_in_dev_offers_dev_login(client):
    response = await client.get("/api/config")

    assert response.status_code == 200
    assert response.json() == {"env": "dev", "dev_login": True, "bot_username": None}


async def test_config_in_prod_has_no_dev_login_and_gives_the_bot_name(settings, db):
    changes = {"telegram_bot_username": "dnd_test_bot"}
    async with client_for(prod_settings(settings, **changes)) as client:
        response = await client.get("/api/config")

    assert response.json() == {"env": "prod", "dev_login": False, "bot_username": "dnd_test_bot"}


async def test_config_needs_no_token_and_shows_no_secret(settings, db):
    changes = {"telegram_bot_username": "dnd_test_bot"}
    async with client_for(prod_settings(settings, **changes)) as client:
        response = await client.get("/api/config")

    for secret in SECRETS:
        assert secret not in response.text


# --- вход для WebSocket: токен из query ---


async def test_user_for_token_finds_the_user_and_refuses_everything_else(settings, db):
    signer = SessionSigner(SESSION_SECRET)
    async with db() as session:
        user = await find_or_create_user(session, name="Игрок", dev_login="dev-1")

        assert (await user_for_token(session, signer, signer.issue(user.id))).id == user.id
        assert await user_for_token(session, signer, signer.issue("no-such-user")) is None
        assert await user_for_token(session, signer, "garbage") is None
        assert await user_for_token(session, signer, "") is None
        assert await user_for_token(session, signer, None) is None
        assert await user_for_token(session, SessionSigner("z" * 40), signer.issue(user.id)) is None


def websocket_with(app, query: bytes) -> WebSocket:
    """Объект WebSocket Starlette без сети: для проверки того, что читают из рукопожатия."""
    scope = {"type": "websocket", "app": app, "query_string": query, "headers": []}
    return WebSocket(scope, receive=None, send=None)  # type: ignore[arg-type]


async def test_websocket_user_reads_the_token_from_the_query(settings, db):
    app = create_app(settings)
    async with db() as session:
        user = await find_or_create_user(session, name="Игрок", dev_login="dev-2")
    token = SessionSigner(SESSION_SECRET).issue(user.id)

    found = await websocket_user(websocket_with(app, f"token={token}".encode()))
    no_token = await websocket_user(websocket_with(app, b""))
    bad_token = await websocket_user(websocket_with(app, b"token=garbage"))
    # Токен в заголовке Authorization для WS не используется: браузер не может его передать.
    await app.state.engine.dispose()

    assert found is not None and found.id == user.id
    assert no_token is None
    assert bad_token is None


# --- find_or_create_user ---


async def test_find_or_create_user_finds_the_row_it_created(settings, db):
    async with db() as session:
        created = await find_or_create_user(session, name="Аня", tg_id=CREATOR_ID)
    async with db() as session:
        found = await find_or_create_user(session, name="Аня", tg_id=CREATOR_ID)
        count = await session.scalar(select(func.count()).select_from(User))

    assert found.id == created.id
    assert count == 1


# --- секрет сессий в prod ---


WEAK_SECRETS = [
    pytest.param(None, id="не задан"),
    pytest.param("change-me", id="заглушка"),
    pytest.param("weak-sentinel-secret", id="короче 32"),
]


@pytest.mark.parametrize("secret", WEAK_SECRETS)
async def test_prod_with_a_weak_secret_issues_no_sessions(settings, db, secret):
    weak = prod_settings(settings, session_secret=secret)
    async with client_for(weak) as client:
        login = await telegram_login(client, init_data())
        me = await client.get("/api/me", headers={"Authorization": "Bearer anything"})

    assert login.status_code == 503
    assert me.status_code == 503
    assert "token" not in login.text
    assert secret is None or secret not in login.text + me.text
    assert await users_in(db) == []


@pytest.mark.parametrize("secret", WEAK_SECRETS)
async def test_prod_with_a_weak_secret_does_not_start(settings, secret):
    app = create_app(prod_settings(settings, session_secret=secret))

    with pytest.raises(WeakSessionSecret) as refusal:
        async with app.router.lifespan_context(app):
            pass

    assert secret is None or secret not in str(refusal.value)
    await app.state.engine.dispose()


@pytest.mark.parametrize("secret", [None, "change-me"])
async def test_dev_starts_with_a_missing_or_placeholder_secret(settings, secret):
    weak = SecretStr(secret) if secret else None
    app = create_app(settings.model_copy(update={"session_secret": weak}))

    async with app.router.lifespan_context(app):
        pass

    await app.state.engine.dispose()


async def test_prod_with_a_strong_secret_starts(settings):
    app = create_app(prod_settings(settings))

    async with app.router.lifespan_context(app):
        pass

    await app.state.engine.dispose()


async def test_a_real_server_in_prod_refuses_to_start_with_a_weak_secret(settings, caplog):
    app = create_app(prod_settings(settings, session_secret="weak-sentinel-secret"))
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="on", log_config=None)
    )

    with caplog.at_level("INFO"), pytest.raises(SystemExit) as stopped:
        await asyncio.wait_for(server.serve(), timeout=10)
    await app.state.engine.dispose()

    assert stopped.value.code == 3  # код uvicorn «приложение не запустилось»
    assert server.started is False
    assert "SESSION_SECRET" in caplog.text  # причина названа
    assert "weak-sentinel-secret" not in caplog.text  # а значения секрета нет
