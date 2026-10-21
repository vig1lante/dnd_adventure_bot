"""Клиент Mini App и сервер говорят об одном и том же (финальное покрытие блока B8).

У клиента и сервера свои контрактные тесты, и каждый идёт на заглушке другой стороны: тесты
клиента — на заглушке сервера, тесты сервера — на своих запросах. Сквозной тест Playwright
проходит только те маршруты, которых касается сценарий. Поэтому переименование или удаление
маршрута на сервере (как «roll-abilities» в B8.s3) или новое поле кадра сокета могли бы пройти
мимо всех тестов, пока игрок не нажмёт кнопку. Здесь исходники клиента (`api/client.ts`,
`api/ws.ts`, `api/types.ts`) читаются как данные и сверяются с настоящим приложением:

- каждому вызову клиента отвечает маршрут сервера с тем же методом, включая кнопки хоста
  из типа `HostAction` и сокет игры;
- виды кадров сокета и поля каждого кадра у сервера и в типе `WsEvent` клиента совпадают;
- поля ответов REST у моделей сервера и у интерфейсов клиента с теми же именами совпадают.
"""

import importlib
import re
from pathlib import Path

import pytest

from dnd_server.app import create_app
from dnd_server.config import Settings

# tests/flows/test_client_routes.py -> flows -> tests -> server -> services
SERVICES = Path(__file__).resolve().parents[3]
CLIENT_DIR = SERVICES / "webapp" / "src" / "api"

# В адресах клиента параметром пути становятся `${gamePath(gameId)}` и `${encodeURIComponent(x)}`.
GAME_PATH = "${gamePath(gameId)}"
PARAM = "{}"


def client_text(name: str) -> str:
    return (CLIENT_DIR / name).read_text(encoding="utf-8")


def host_actions() -> list[str]:
    """Кнопки хоста из типа `HostAction`: по маршруту на каждую."""
    declaration = re.search(r"export type HostAction\s*=\s*([^;]+);", client_text("types.ts"))
    assert declaration is not None, "тип HostAction в types.ts не найден"
    actions = re.findall(r'"([a-z-]+)"', declaration.group(1))
    assert actions, "в типе HostAction нет ни одной кнопки"
    return actions


def normalized(path: str) -> str:
    """Адрес без имён параметров: `/api/games/{game_id}/ready` и `/api/games/{}/ready` совпадают."""
    return re.sub(r"\{[^}]*\}", PARAM, path)


def client_calls() -> list[tuple[str, str]]:
    """Пары (метод, адрес) из `createApi`: вызовы `get`, `post` и `request` с явным методом."""
    text = client_text("client.ts")
    body = text[text.index("return {") :]
    pattern = re.compile(
        r"\b(get|post|request)<[^()]*?>\(\s*(?:\"(GET|POST|PUT|DELETE)\",\s*)?([`\"])(.+?)\3",
        re.DOTALL,
    )
    calls: list[tuple[str, str]] = []
    for helper, explicit, _quote, raw in pattern.findall(body):
        method = explicit or helper.upper()
        path = raw.replace(GAME_PATH, "/api/games/" + PARAM)
        path = re.sub(r"\$\{encodeURIComponent\([^)]*\)\}", PARAM, path)
        if "${action}" in path:
            calls += [(method, path.replace("${action}", action)) for action in host_actions()]
            continue
        assert "${" not in path, f"непонятный параметр в адресе клиента: {raw}"
        calls.append((method, path))
    return calls


@pytest.fixture
def served(settings: Settings) -> dict[str, set[str]]:
    """Маршруты настоящего приложения: адрес без имён параметров -> методы (нижний регистр)."""
    paths = create_app(settings).openapi()["paths"]
    served: dict[str, set[str]] = {}
    for path, operations in paths.items():
        served.setdefault(normalized(path), set()).update(operations)
    return served


def test_the_parser_finds_the_whole_api_of_the_client():
    calls = client_calls()

    # Опорные вызовы разных видов: простой, с параметром, с телом, с явным методом, кнопки хоста.
    assert ("GET", "/api/config") in calls
    assert ("POST", "/api/games/{}/ready") in calls
    assert ("POST", "/api/games/{}/players/{}/kick") in calls
    assert ("PUT", "/api/games/{}/action") in calls
    assert ("POST", "/api/games/{}/host/camp-end") in calls
    assert ("POST", "/api/games/{}/characters/random-abilities") in calls
    assert ("GET", "/api/games/{}/characters/mine") in calls
    assert len(calls) >= 30, f"клиент разобран не целиком: {len(calls)} вызовов"
    assert len(host_actions()) == 10


def missing_routes(served: dict[str, set[str]]) -> list[str]:
    """Вызовы клиента, на которые у сервера нет маршрута с тем же методом."""
    return [
        f"{method} {path}"
        for method, path in client_calls()
        if method.lower() not in served.get(normalized(path), set())
    ]


def test_every_route_the_client_calls_exists_on_the_server_with_the_same_method(served):
    assert missing_routes(served) == [], "клиент вызывает маршруты, которых нет на сервере"


def test_the_comparison_notices_a_renamed_route_and_a_changed_method(served):
    # Контроль сверки: без него «пропусков нет» ничего бы не доказывало.
    renamed = {path: set(methods) for path, methods in served.items()}
    renamed["/api/games/{}/ready-now"] = renamed.pop("/api/games/{}/ready")
    changed = {path: set(methods) for path, methods in served.items()}
    changed["/api/games/{}/action"] = {"post"}  # у клиента здесь PUT

    assert missing_routes(renamed) == ["POST /api/games/{}/ready"]
    assert missing_routes(changed) == ["PUT /api/games/{}/action"]


def test_the_game_socket_the_client_opens_exists_on_the_server(settings):
    ws_source = client_text("ws.ts")
    opened = re.search(r"/ws/games/\$\{encodeURIComponent\(gameId\)\}", ws_source)
    assert opened is not None, "адрес сокета игры в ws.ts не найден"

    # Маршрут сокета ищется по имени обработчика: так он находится и внутри подключённых роутеров.
    assert create_app(settings).url_path_for("game_socket", game_id="G1") == "/ws/games/G1"


def test_the_old_roll_route_is_not_called_by_the_client(served):
    # Прежний бросок характеристик убран по решению владельца R9: ни на сервере, ни у клиента.
    assert not any("roll-abilities" in path for _method, path in client_calls())
    assert "/api/games/{}/characters/roll-abilities" not in served


# --- события сокета: что присылает сервер и что описывает клиент ---

SERVER_DIR = SERVICES / "server" / "src" / "dnd_server"
# Где сервер собирает кадры сокета; `ping` — единственный кадр в обратную сторону.
EVENT_SOURCES = ("realtime/views.py", "realtime/ws.py", "games/service.py")
CLIENT_TO_SERVER = {"ping"}


def top_level_keys(text: str, start: int) -> set[str]:
    """Ключи словаря верхнего уровня, открывающегося фигурной скобкой в позиции `start`."""
    depth = 0
    keys: set[str] = set()
    index = start
    while index < len(text):
        char = text[index]
        if char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
            if depth == 0:
                return keys
        elif char == '"' and depth == 1:
            literal = re.match(r'"(\w+)"\s*:', text[index:])
            if literal is not None:
                keys.add(literal.group(1))
                index += literal.end() - 1
        index += 1
    raise AssertionError("словарь события не закрыт")


def server_events() -> dict[str, set[str]]:
    """Тип кадра сокета -> ключи его словаря (без `type`), как они записаны в исходниках сервера."""
    events: dict[str, set[str]] = {}
    for name in EVENT_SOURCES:
        text = (SERVER_DIR / name).read_text(encoding="utf-8")
        for found in re.finditer(r'"type":\s*"(\w+)"', text):
            if found.group(1) in CLIENT_TO_SERVER:
                continue
            opening = text.rindex("{", 0, found.start())
            events[found.group(1)] = top_level_keys(text, opening) - {"type"}
    return events


def client_events() -> dict[str, set[str]]:
    """Тип кадра -> поля из `WsEvent` в `types.ts` (снимок — из интерфейса `Snapshot`)."""
    text = client_text("types.ts")
    events: dict[str, set[str]] = {}
    union = text[text.index("export type WsEvent") :]
    for line in union.splitlines():
        found = re.match(r'\s*\|\s*\{\s*type:\s*"(\w+)";?([^}]*)\}', line)
        if found:
            events[found.group(1)] = set(re.findall(r"(\w+)\??:", found.group(2)))
    snapshot = re.search(r"export interface Snapshot \{(.*?)\n\}", text, re.DOTALL)
    assert snapshot is not None, "интерфейс Snapshot в types.ts не найден"
    fields = set(re.findall(r"^\s{2}(\w+)\??:", snapshot.group(1), re.MULTILINE))
    events["snapshot"] = fields - {"type"}
    return events


def test_the_key_reader_takes_only_the_top_level_of_a_dict():
    sample = 'x = {"type": "t", "a": {"b": 1}, "c": [1, {"d": 2}], "e": f("g", {"h": 3})}'

    assert top_level_keys(sample, sample.index("{")) == {"type", "a", "c", "e"}


def test_the_event_parsers_see_every_kind_of_frame():
    server, client = server_events(), client_events()

    for events in (server, client):
        assert {"snapshot", "phase", "feed_item", "combat", "character", "lobby"} <= set(events)
        assert events["phase"] == {"phase"}
        assert events["player_status"] == {"user_id", "status"}
    assert len(server["snapshot"]) >= 10


def test_the_client_describes_exactly_the_kinds_of_frames_the_server_sends():
    server, client = server_events(), client_events()

    assert set(client) == set(server), "виды кадров сокета у сервера и клиента разошлись"


def test_every_frame_has_the_same_fields_on_both_sides():
    server, client = server_events(), client_events()

    different = {
        kind: {"сервер": sorted(server[kind]), "клиент": sorted(client[kind])}
        for kind in sorted(server)
        if kind in client and server[kind] != client[kind]
    }

    assert different == {}, "поля кадров сокета у сервера и клиента разошлись"


# --- поля ответов REST: модель сервера и интерфейс клиента ---


def interface_fields(name: str) -> set[str]:
    """Имена полей интерфейса `name` из `types.ts` (верхний уровень, без комментариев)."""
    # Сначала строки `//`: в них встречается «characters/*», похожее на начало блочного комментария.
    text = re.sub(r"(?m)^\s*//.*$", "", client_text("types.ts"))
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    found = re.search(rf"export interface {name}(?:\s+extends\s+\w+)?\s*\{{", text)
    assert found is not None, f"интерфейс {name} в types.ts не найден"
    fields: set[str] = set()
    depth = 0
    for line in text[found.end() - 1 :].splitlines():
        if depth == 1:
            field = re.match(r"\s*(\w+)\??:", line)
            if field:
                fields.add(field.group(1))
        depth += sum(line.count(char) for char in "{[(") - sum(line.count(char) for char in "}])")
        if depth <= 0:
            return fields
    raise AssertionError(f"интерфейс {name} не закрыт")


def model_fields(path: str) -> set[str]:
    module, _, name = path.rpartition(".")
    model = getattr(importlib.import_module(module), name)
    return set(model.model_fields)


# (модель сервера, интерфейс клиента, поля, которые клиент добавляет к модели сервера, и наоборот)
RESPONSES = [
    ("dnd_server.auth.api.UserOut", "User", set(), set()),
    ("dnd_server.auth.api.LoginOut", "LoginResult", set(), set()),
    ("dnd_server.auth.api.ConfigOut", "AppConfig", set(), set()),
    ("dnd_server.games.schemas.PlayerOut", "PlayerSummary", set(), set()),
    ("dnd_server.games.schemas.GameOut", "GameSummary", set(), set()),
    ("dnd_server.adventures.AdventureOut", "AdventureSummary", set(), set()),
    ("dnd_server.games.providers.ProviderOut", "ProviderOption", set(), set()),
    ("dnd_server.games.providers.ProvidersOut", "ProvidersList", set(), set()),
    ("dnd_server.characters.service.CreatedOut", "CreatedCharacter", set(), set()),
    ("dnd_server.characters.service.RandomPreviewOut", "RandomPreview", set(), set()),
    ("dnd_server.characters.service.RandomAbilitiesOut", "RandomAbilities", set(), set()),
    ("dnd_server.characters.service.CreationOptions", "CreationOptions", set(), set()),
    ("dnd_server.characters.service.ClassOption", "ClassOption", set(), set()),
    ("dnd_server.characters.service.SpeciesOption", "SpeciesOption", set(), set()),
    ("dnd_server.characters.service.BackgroundOption", "BackgroundOption", set(), set()),
    ("dnd_server.characters.service.SpellcastingOption", "SpellcastingOption", set(), set()),
    ("dnd_server.characters.service.SpellOption", "SpellOption", set(), set()),
    ("dnd_server.characters.service.SkillOption", "SkillOption", set(), set()),
    ("dnd_server.characters.service.AbilityOption", "AbilityOption", set(), set()),
    ("dnd_server.characters.service.FeatureOption", "FeatureOption", set(), set()),
    ("dnd_server.characters.service.ItemLine", "ItemLine", set(), set()),
    ("dnd_server.characters.service.QuickChoices", "QuickChoices", set(), set()),
    ("dnd_engine.creation.CharacterChoices", "CharacterChoices", set(), set()),
    ("dnd_engine.character.Persona", "Persona", set(), set()),
    # Лист героя клиент получает с блоком `derived`, который добавляет `character_view`.
    ("dnd_engine.character.Character", "Character", set(), {"derived"}),
    # У предмета приключения сервер добавляет `name` (B8.s2).
    ("dnd_engine.character.InventoryItem", "InventoryItem", set(), {"name"}),
]


@pytest.mark.parametrize(
    ("model", "interface", "only_server", "only_client"),
    RESPONSES,
    ids=[interface for _model, interface, _server, _client in RESPONSES],
)
def test_the_fields_of_a_response_are_the_same_on_the_server_and_in_the_client(
    model, interface, only_server, only_client
):
    server, client = model_fields(model), interface_fields(interface)

    assert server | only_client == client | only_server, (
        f"{model} и {interface}: только на сервере {sorted(server - client - only_server)}, "
        f"только в клиенте {sorted(client - server - only_client)}"
    )


def test_the_field_reader_takes_nested_objects_as_one_field():
    assert interface_fields("Snapshot") >= {"type", "server_time", "feed"}
    assert interface_fields("PlayerAction") == {"text", "secret", "inspiration", "status"}
    # Вложенные объекты `scores: { … }` не раскрываются в отдельные поля.
    assert "str" not in interface_fields("Character")


def test_the_comparison_notices_a_field_that_one_side_does_not_have():
    # Контроль сверки полей: разные по составу модель и интерфейс не выдают себя за одинаковые.
    assert model_fields("dnd_server.auth.api.UserOut") != interface_fields("LoginResult")
    assert interface_fields("User") == {"id", "name", "can_create_games"}
