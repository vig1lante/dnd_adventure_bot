"""Справочник ссылок черновика `GET /api/drafts/{id}/refs` (план B5.1; дополнение dispatcher B5 §6).

По нему RefPicker webapp выбирает концы связей, держателей предметов, бой локации, лидера фракции и
злодея: `[{ref, kind, name}]` по сущностям черновика автора — виды и порядок по контракту, имя без
тайн. Доступ — как у черновика: 403 не-создателю и 404 чужим (ещё и обход всех маршрутов черновика
в `test_drafts_api.py`). Живых моделей нет: полный черновик собирает `FakeProvider`.
"""

import json
from typing import Any

from test_drafts_api import (
    CREATOR_TG_ID,
    Author,
    Stand,
    assert_error,
    complete_draft,
    npcs_data,
    saved,
)

REF_FIELDS = {"ref", "kind", "name"}


async def refs(stand: Stand, who: Author, draft: dict[str, Any]) -> list[dict[str, str]]:
    response = await stand.call(who, "GET", f"/api/drafts/{draft['id']}/refs")
    assert response.status_code == 200, response.text
    return response.json()


async def test_a_new_draft_has_no_refs(stand):
    author = await stand.author()
    draft = await stand.new_draft(author)

    assert await refs(stand, author, draft) == []


async def test_refs_name_every_entity_by_kind_in_the_order_of_the_contract(stand):
    author = await stand.author()
    draft = await complete_draft(stand, author)

    found = await refs(stand, author, draft)

    assert all(set(entry) == REF_FIELDS for entry in found)
    # Виды по порядку: боги, фракции, NPC, локации, предметы, бои; внутри вида — порядок раздела.
    assert [(entry["ref"], entry["name"]) for entry in found] == [
        ("deity:selmara", "Сельмара"),
        ("deity:ashen-one", "Пепельный"),
        ("faction:smugglers", "Контрабандисты Вороньего мыса"),
        ("faction:ash-circle", "Пепельный круг"),
        ("npc:ilsa", "Ильза"),
        ("npc:bram", "Брам"),
        ("npc:veran", "Веран"),
        ("location:harbor", "Гавань Вороньего мыса"),
        ("location:cliff-path", "Тропа над обрывом"),
        ("location:lighthouse", "Комнаты смотрителя"),
        ("location:lamp-room", "Фонарная"),
        ("item:lamp-key", "Ключ от фонарной"),
        ("item:keeper-ring", "Кольцо смотрителя"),
        ("item:ash-tome", "Пепельный требник"),
        ("encounter:drowned-ambush", "drowned-ambush"),  # у боя имени нет — его id
        ("encounter:ash-rite", "ash-rite"),
    ]
    assert all(entry["ref"].startswith(f"{entry['kind']}:") for entry in found)
    # Связи, свои статблоки и открытия ссылками полей не выбираются.
    assert draft["relations"] and draft["monsters"] and draft["revelations"]


async def test_refs_tell_no_secrets_of_the_draft(stand):
    author = await stand.author()
    draft = await complete_draft(stand, author)
    secrets = [
        *(deity["secret"] for deity in draft["deities"]),
        *(faction["secret"] for faction in draft["factions"]),
        *(npc["secret"] for npc in draft["npcs"]),
        *(location["dm_notes"] for location in draft["locations"]),
        *(secret["text"] for location in draft["locations"] for secret in location["secrets"]),
        *(f["description"] for location in draft["locations"] for f in location["features"]),
        *(encounter["trigger"] for encounter in draft["encounters"]),
        *(encounter["tactics"] for encounter in draft["encounters"]),
    ]
    assert len([text for text in secrets if text]) >= 10

    response = await stand.call(author, "GET", f"/api/drafts/{draft['id']}/refs")

    shown = json.dumps(response.json(), ensure_ascii=False)
    for text in filter(None, secrets):
        assert text not in shown, text


async def test_an_empty_name_is_shown_as_the_id(stand):
    author = await stand.author()
    draft = await stand.new_draft(author)
    ilsa, bram, *_ = npcs_data()
    draft = await saved(stand, author, draft, "npcs", [ilsa | {"name": ""}, bram | {"name": "  "}])

    found = await refs(stand, author, draft)

    assert found == [
        {"ref": "npc:ilsa", "kind": "npc", "name": "ilsa"},
        {"ref": "npc:bram", "kind": "npc", "name": "bram"},
    ]


async def test_refs_hold_only_the_entities_of_the_asked_draft(stand):
    author, other = await stand.author(), await stand.author()
    draft = await stand.new_draft(author)
    draft = await saved(stand, author, draft, "npcs", npcs_data()[:1])
    await complete_draft(stand, author)  # другой черновик того же автора
    await complete_draft(stand, other)

    found = await refs(stand, author, draft)

    assert found == [{"ref": "npc:ilsa", "kind": "npc", "name": "Ильза"}]


async def test_refs_follow_the_written_sections_and_are_not_a_write(stand):
    author = await stand.author()
    draft = await stand.new_draft(author)
    draft = await saved(stand, author, draft, "npcs", npcs_data())

    draft = await saved(stand, author, draft, "npcs", npcs_data()[1:])
    found = await refs(stand, author, draft)

    assert [entry["ref"] for entry in found] == ["npc:bram", "npc:veran"]
    assert (await stand.draft(author, draft["id"]))["version"] == draft["version"]


async def test_refs_of_another_creators_draft_are_404_and_a_player_gets_403(stand):
    author, other = await stand.author(), await stand.author()
    outsider = await stand.author(tg_id=CREATOR_TG_ID + 1)
    draft = await complete_draft(stand, author)
    path = f"/api/drafts/{draft['id']}/refs"

    foreign = await stand.call(other, "GET", path)
    refused = await stand.call(outsider, "GET", path)

    body = assert_error(foreign, 404, None)
    assert set(body) == {"detail"} and "selmara" not in foreign.text
    assert_error(refused, 403, None)
    assert "selmara" not in refused.text
