"""Маршруты хода игрока и кнопок хоста `/api/games/{id}/...` (план B7.4): тонкая обёртка над `flow`.

Ход игрока: `PUT .../action` (черновик), `POST .../action/submit` («Готов»), `POST .../action/pass`
(«Пас»). В бою «Готов» и «Пас» сразу отдают ход мастеру. Реплика героя у костра — `POST
.../camp/line {text}` (любой игрок, пока идёт привал). Кнопки хоста:
`POST .../host/{collect,extend,close,pause,resume,retry,regenerate,hurry,camp-extend,camp-end}`
(«Поторопить» — только в бою, `camp-*` — только на привале); ответ кнопки — фаза игры (тот же блок
`phase`, что в снимке и событии). Кнопки, которые запускают мастера, отвечают, когда ход
зарезервирован, а не когда готов рассказ: итог приходит событиями по сокету.

Привалы по кнопке хоста (DND-2, блок B9): `POST .../host/rest {kind: "short"|"long"}` начинает
привал (ответ — фаза), `POST .../host/rest-extend` и `.../host/rest-end` — те же «+1 минута» и
«Закончить привал», что `camp-*`, для привала любого вида. Кость хитов на коротком привале —
`POST .../rest/hit-dice` без тела (любой игрок с героем): бросает сервер, ответ — остаток костей и
хиты героя.

Ошибки правил отдаёт общий обработчик `games.api.handle_game_error` с кодами игр: нет игры или
игрока в ней — 404, кнопка не хоста — 403, не то состояние игры (разбор идёт, пауза, сбор закрыт) —
409, сдавать нечего — 422. Клиент присылает только текст и две отметки; статус хода, сроки и
автор не берутся из тела запроса. Черновики сохраняются с задержкой (debounce клиента), поэтому
клиент отправляет последний черновик до «Готов»: правка после сдачи возвращает ход в черновик.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.requests import HTTPConnection

from dnd_server.db.models import ActionStatus
from dnd_server.dm.camp_flow import CAMP_LINE_MAX_CHARS
from dnd_server.games.api import UserDep
from dnd_server.realtime.views import feed_item
from dnd_server.turns import flow as game_flow
from dnd_server.turns.flow import Flow

router = APIRouter(prefix="/api/games")

# Длина хода игрока: хватает рассказа на несколько абзацев; текст уходит в базу и в запрос к модели.
ACTION_MAX_CHARS = 3000


def get_flow(connection: HTTPConnection) -> Flow:
    return Flow.from_app(connection.app)


FlowDep = Annotated[Flow, Depends(get_flow)]


class ActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(default="", max_length=ACTION_MAX_CHARS)
    secret: StrictBool = False
    inspiration: StrictBool = False


class CampLineIn(BaseModel):
    """Реплика героя у костра: только текст; автор — игрок запроса, а не поле тела."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=CAMP_LINE_MAX_CHARS)


class RestIn(BaseModel):
    """Начало привала: только его вид; срок берётся из настроек игры."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["short", "long"]


class HitDieOut(BaseModel):
    """Итог кости хитов для кнопки героя: остаток костей и хиты после лечения."""

    hit_dice: int
    hp: int
    hp_max: int


class ActionOut(BaseModel):
    """Ход игрока, как его видит сам автор (тот же вид, что `action` снимка)."""

    model_config = ConfigDict(from_attributes=True)

    text: str
    secret: bool
    inspiration: bool
    status: ActionStatus


# --- ход игрока ---


@router.put("/{game_id}/action")
async def save_draft(game_id: str, body: ActionIn, user: UserDep, flow: FlowDep) -> ActionOut:
    row = await game_flow.save_draft(
        flow,
        game_id,
        user.id,
        text=body.text,
        secret=body.secret,
        inspiration=body.inspiration,
    )
    return ActionOut.model_validate(row)


@router.post("/{game_id}/action/submit")
async def submit_action(game_id: str, user: UserDep, flow: FlowDep) -> ActionOut:
    return ActionOut.model_validate(await game_flow.submit_action(flow, game_id, user.id))


@router.post("/{game_id}/action/pass")
async def pass_action(game_id: str, user: UserDep, flow: FlowDep) -> ActionOut:
    return ActionOut.model_validate(await game_flow.pass_action(flow, game_id, user.id))


# --- привал ---


@router.post("/{game_id}/camp/line")
async def camp_line(game_id: str, body: CampLineIn, user: UserDep, flow: FlowDep) -> dict[str, Any]:
    """Реплика героя у костра; ответ — запись ленты (`camp_line`), как её увидят все."""
    return feed_item(await game_flow.camp_line(flow, game_id, user.id, body.text))


@router.post("/{game_id}/rest/hit-dice")
async def spend_hit_die(game_id: str, user: UserDep, flow: FlowDep) -> HitDieOut:
    """Кость хитов героя игрока на коротком привале; бросок делает сервер."""
    hero = await game_flow.spend_hit_die(flow, game_id, user.id)
    return HitDieOut(hit_dice=hero.hit_dice, hp=hero.hp, hp_max=hero.hp_max)


# --- кнопки хоста ---


@router.post("/{game_id}/host/collect")
async def collect(game_id: str, user: UserDep, flow: FlowDep) -> dict[str, Any]:
    return await game_flow.collect(flow, game_id, user.id)


@router.post("/{game_id}/host/extend")
async def extend(game_id: str, user: UserDep, flow: FlowDep) -> dict[str, Any]:
    return await game_flow.extend(flow, game_id, user.id)


@router.post("/{game_id}/host/close")
async def close(game_id: str, user: UserDep, flow: FlowDep) -> dict[str, Any]:
    return await game_flow.close(flow, game_id, user.id)


@router.post("/{game_id}/host/pause")
async def pause(game_id: str, user: UserDep, flow: FlowDep) -> dict[str, Any]:
    return await game_flow.pause(flow, game_id, user.id)


@router.post("/{game_id}/host/resume")
async def resume(game_id: str, user: UserDep, flow: FlowDep) -> dict[str, Any]:
    return await game_flow.resume(flow, game_id, user.id)


@router.post("/{game_id}/host/retry")
async def retry(game_id: str, user: UserDep, flow: FlowDep) -> dict[str, Any]:
    return await game_flow.retry(flow, game_id, user.id)


@router.post("/{game_id}/host/regenerate")
async def regenerate(game_id: str, user: UserDep, flow: FlowDep) -> dict[str, Any]:
    return await game_flow.regenerate(flow, game_id, user.id)


@router.post("/{game_id}/host/hurry")
async def hurry(game_id: str, user: UserDep, flow: FlowDep) -> dict[str, Any]:
    return await game_flow.hurry(flow, game_id, user.id)


@router.post("/{game_id}/host/camp-extend")
async def camp_extend(game_id: str, user: UserDep, flow: FlowDep) -> dict[str, Any]:
    return await game_flow.camp_extend(flow, game_id, user.id)


@router.post("/{game_id}/host/camp-end")
async def camp_end(game_id: str, user: UserDep, flow: FlowDep) -> dict[str, Any]:
    return await game_flow.camp_end(flow, game_id, user.id)


@router.post("/{game_id}/host/rest")
async def start_rest(game_id: str, body: RestIn, user: UserDep, flow: FlowDep) -> dict[str, Any]:
    return await game_flow.start_rest(flow, game_id, user.id, body.kind)


# Те же кнопки привала, что `camp-*`, под путями привала любого вида (дополнение контракта B9 §1
# п. 3).
@router.post("/{game_id}/host/rest-extend")
async def rest_extend(game_id: str, user: UserDep, flow: FlowDep) -> dict[str, Any]:
    return await game_flow.camp_extend(flow, game_id, user.id)


@router.post("/{game_id}/host/rest-end")
async def rest_end(game_id: str, user: UserDep, flow: FlowDep) -> dict[str, Any]:
    return await game_flow.camp_end(flow, game_id, user.id)
