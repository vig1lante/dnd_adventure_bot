import { fireEvent, render, screen } from "@testing-library/react";
import { createApi } from "../../api/client";
import { ApiContext } from "../../api/context";
import type { Character } from "../../api/types";
import { sessionToken } from "../../store/game";
import { fakeServer, signInAs } from "../../test-setup";
import type { FakeReply, RecordedCall } from "../../test-setup";
import { OPTIONS, OUTDATED_WORDS, holdRequests, previewHero } from "./fixtures";
import { Random } from "./Random";

const PREVIEW = "POST /api/games/g1/characters/random-preview";
const TAKE = "POST /api/games/g1/characters";

const FIRST_ID = "rp_aaaaaaaaaaaaaaaa";
const SECOND_ID = "rp_bbbbbbbbbbbbbbbb";

const ROGUE = previewHero();
const WIZARD = previewHero({
  id: "pc_2222222222222222",
  species: "dwarf",
  class_id: "wizard",
  background: "acolyte",
  scores: { str: 8, dex: 14, con: 14, int: 16, wis: 12, cha: 10 },
  skill_proficiencies: ["insight", "religion", "arcana", "history"],
  expertise: [],
  save_proficiencies: ["int", "wis"],
  cantrips: ["fire_bolt", "light"],
  prepared_spells: ["shield", "sleep"],
  hp_max: 17,
  hp: 17,
  inventory: [
    { item_id: "quarterstaff", qty: 1, equipped: true },
    { item_id: "spellbook", qty: 1, equipped: false },
  ],
});

type Route = FakeReply | ((call: RecordedCall) => FakeReply);

const preview = (id: string, character: Character): FakeReply => ({
  json: { preview_id: id, character },
});
const taken = (flavor_error: string | null = null): FakeReply => ({
  status: 201,
  json: {
    character: previewHero({ name: "Тень", persona: { ...ROGUE.persona } }),
    flavor_error,
  },
});

interface Setup {
  /** Ответы на последовательные запросы предпросмотра. */
  previews?: Route[];
  take?: Route;
}

function renderRandom({ previews, take }: Setup = {}) {
  signInAs();
  let asked = 0;
  const queue = previews ?? [
    preview(FIRST_ID, ROGUE),
    preview(SECOND_ID, WIZARD),
  ];
  const server = fakeServer({
    [PREVIEW]: (call) => {
      const next = queue[Math.min(asked++, queue.length - 1)];
      return typeof next === "function" ? next(call) : next;
    },
    [TAKE]: take ?? taken(),
  });
  const gated = holdRequests(server.fetch);
  const onBack = vi.fn();
  const onCreated = vi.fn();
  render(
    <ApiContext value={createApi(gated.fetch, sessionToken)}>
      <Random
        gameId="g1"
        options={OPTIONS}
        onBack={onBack}
        onCreated={onCreated}
      />
    </ApiContext>,
  );
  return { server, gated, onBack, onCreated };
}

const click = (name: string) =>
  fireEvent.click(screen.getByRole("button", { name }));
const button = (name: string) => screen.getByRole("button", { name });

const sheet = () => screen.getByRole("region", { name: "Лист героя" });

/** Нажимает «Бросить судьбу» и ждёт листа героя. */
async function rollFirst(setup: Setup = {}) {
  const rendered = renderRandom(setup);
  click("Бросить судьбу");
  await screen.findByRole("region", { name: "Лист героя" });
  return rendered;
}

const calls = (server: { calls: RecordedCall[] }, route: string) =>
  server.calls.filter((call) => `${call.method} ${call.path}` === route);

describe("до броска", () => {
  test("видна только «Бросить судьбу», запросов нет", () => {
    const { server } = renderRandom();

    expect(button("Бросить судьбу")).toBeEnabled();
    expect(screen.queryByRole("region", { name: "Лист героя" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Беру" })).toBeNull();
    expect(server.calls).toHaveLength(0);
  });

  test("пояснение до броска говорит о расе, устаревших слов нет", () => {
    renderRandom();

    expect(
      screen.getByText(/Судьба бросит кубики: раса, класс/),
    ).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(OUTDATED_WORDS);
  });

  test("«Назад» возвращает к выбору способа", () => {
    const { onBack } = renderRandom();

    click("Назад");

    expect(onBack).toHaveBeenCalledTimes(1);
  });
});

describe("предпросмотр героя", () => {
  test("«Бросить судьбу» просит сервер: POST без тела, с токеном и без query", async () => {
    const { server } = await rollFirst();

    const [call] = calls(server, PREVIEW);
    expect(calls(server, PREVIEW)).toHaveLength(1);
    expect(call.body).toBeUndefined();
    expect(call.headers.get("Authorization")).toBe("Bearer tok-u1");
    expect(call.url).toBe("/api/games/g1/characters/random-preview");
  });

  test("лист героя по-русски: класс, раса, предыстория, числа, навыки, снаряжение", async () => {
    await rollFirst();

    expect(screen.getByRole("heading", { name: "Плут" })).toBeInTheDocument();
    expect(sheet()).toHaveTextContent("Эльф · Преступник · уровень 3");
    expect(sheet()).toHaveTextContent("Ловкость");
    expect(sheet()).toHaveTextContent("16");
    expect(sheet()).toHaveTextContent("Внимательность");
    expect(sheet()).toHaveTextContent("Скрытность");
    expect(sheet()).toHaveTextContent("Кинжал × 3");
    expect(sheet()).toHaveTextContent("Воровские инструменты");
    expect(screen.getByRole("meter", { name: "Здоровье" })).toHaveAttribute(
      "aria-valuenow",
      "21",
    );
  });

  test("имени и описания в предпросмотре нет: их придумает мастер после «Беру»", async () => {
    await rollFirst();

    expect(screen.queryByText(/Безымянный странник/)).toBeNull();
    expect(sheet()).toHaveTextContent(/имя и описание придумает мастер/i);
  });

  test("клиент ничего не бросает сам: ни при броске, ни при «Ещё раз», ни при «Беру»", async () => {
    const random = vi.spyOn(Math, "random");
    const { onCreated } = await rollFirst();
    click("Ещё раз");
    await screen.findByRole("heading", { name: "Волшебник" });
    click("Беру");
    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));

    expect(random).not.toHaveBeenCalled();
  });
});

describe("«Ещё раз»", () => {
  test("новый предпросмотр заменяет героя на экране", async () => {
    const { server } = await rollFirst();

    click("Ещё раз");

    expect(
      await screen.findByRole("heading", { name: "Волшебник" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Плут" })).toBeNull();
    expect(sheet()).toHaveTextContent("Огненная стрела");
    expect(sheet()).toHaveTextContent("Книга заклинаний");
    expect(calls(server, PREVIEW)).toHaveLength(2);
  });

  test("пока запрос идёт, обе кнопки заблокированы и второй запрос не уходит", async () => {
    const { gated, server } = await rollFirst();
    const release = gated.hold("/random-preview");

    click("Ещё раз");

    expect(await screen.findByRole("status")).toHaveTextContent("Бросаем");
    expect(button("Ещё раз")).toBeDisabled();
    expect(button("Беру")).toBeDisabled();
    expect(button("Назад")).toBeDisabled();
    fireEvent.click(button("Ещё раз"));
    expect(
      gated.started.filter((line) => line.endsWith("/random-preview")),
    ).toHaveLength(2);

    release();
    expect(
      await screen.findByRole("heading", { name: "Волшебник" }),
    ).toBeInTheDocument();
    expect(button("Ещё раз")).toBeEnabled();
    expect(calls(server, PREVIEW)).toHaveLength(2);
  });

  test("первый бросок тоже сериализован: «Бросить судьбу» блокируется на время запроса", async () => {
    const { gated } = renderRandom();
    const release = gated.hold("/random-preview");

    click("Бросить судьбу");

    expect(await screen.findByRole("status")).toBeInTheDocument();
    expect(button("Бросить судьбу")).toBeDisabled();
    fireEvent.click(button("Бросить судьбу"));
    expect(
      gated.started.filter((line) => line.endsWith("/random-preview")),
    ).toHaveLength(1);

    release();
    await screen.findByRole("region", { name: "Лист героя" });
  });

  test("429: текст сервера показан, прежний герой остаётся и его можно взять", async () => {
    const { server, onCreated } = await rollFirst({
      previews: [
        preview(FIRST_ID, ROGUE),
        {
          status: 429,
          json: {
            detail:
              "Слишком много бросков подряд: подождите 12 с и бросьте снова.",
          },
        },
      ],
    });

    click("Ещё раз");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Слишком много бросков подряд: подождите 12 с",
    );
    expect(screen.getByRole("heading", { name: "Плут" })).toBeInTheDocument();
    click("Беру");
    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(calls(server, TAKE)[0].body).toEqual({
      mode: "random",
      preview_id: FIRST_ID,
    });
  });

  test("игра уже началась (409 на предпросмотр): текст сервера, героя взять нельзя", async () => {
    await rollFirst({
      previews: [
        preview(FIRST_ID, ROGUE),
        {
          status: 409,
          json: {
            detail: "Игра уже началась: персонажа больше нельзя менять.",
          },
        },
      ],
    });

    click("Ещё раз");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Игра уже началась",
    );
  });
});

describe("«Беру»", () => {
  test("шлёт mode random и preview_id показанного героя", async () => {
    const { server, onCreated } = await rollFirst();

    click("Беру");

    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(calls(server, TAKE)).toHaveLength(1);
    expect(calls(server, TAKE)[0].body).toEqual({
      mode: "random",
      preview_id: FIRST_ID,
    });
    expect(calls(server, TAKE)[0].headers.get("Authorization")).toBe(
      "Bearer tok-u1",
    );
  });

  test("после «Ещё раз» уходит id нового героя, а не прежнего", async () => {
    const { server, onCreated } = await rollFirst();
    click("Ещё раз");
    await screen.findByRole("heading", { name: "Волшебник" });

    click("Беру");

    await vi.waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect(calls(server, TAKE)[0].body).toEqual({
      mode: "random",
      preview_id: SECOND_ID,
    });
  });

  test("пока мастер описывает героя, обе кнопки заблокированы, второй запрос не уходит", async () => {
    const { gated, server, onCreated } = await rollFirst();
    const release = gated.hold("/api/games/g1/characters");

    click("Беру");

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Мастер придумывает",
    );
    expect(button("Беру")).toBeDisabled();
    expect(button("Ещё раз")).toBeDisabled();
    fireEvent.click(button("Беру"));
    expect(
      gated.started.filter((line) => line === "POST /api/games/g1/characters"),
    ).toHaveLength(1);

    release();
    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(calls(server, TAKE)).toHaveLength(1);
  });

  test("описание не получилось (flavor_error): герой сохранён, текст показан, в лобби — по кнопке", async () => {
    const { onCreated } = await rollFirst({
      take: taken(
        "Нейронка не ответила. Персонаж сохранён: описание можно написать самому.",
      ),
    });

    click("Беру");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Персонаж сохранён: описание можно написать самому.",
    );
    expect(onCreated).not.toHaveBeenCalled();
    click("В лобби");
    expect(onCreated).toHaveBeenCalledTimes(1);
  });

  test("409 «герой больше не доступен»: текст сервера и предложение бросить ещё раз", async () => {
    const stale =
      "Этот герой больше не доступен: его заменил новый бросок, он уже взят или устарел. Бросьте судьбу ещё раз.";
    const { onCreated } = await rollFirst({
      take: { status: 409, json: { detail: stale } },
    });

    click("Беру");

    expect(await screen.findByRole("alert")).toHaveTextContent(stale);
    expect(onCreated).not.toHaveBeenCalled();
    // Устаревшего героя взять нельзя, а «Ещё раз» доступна и выделена основной.
    expect(button("Беру")).toBeDisabled();
    expect(button("Ещё раз")).toBeEnabled();
    expect(button("Ещё раз")).toHaveClass("ui-button--primary");
  });

  test("пока герой годится, основная кнопка — «Беру»; после 409 основной становится «Ещё раз»", async () => {
    await rollFirst({
      take: { status: 409, json: { detail: "Этот герой больше не доступен." } },
    });
    expect(button("Беру")).toHaveClass("ui-button--primary");
    expect(button("Ещё раз")).toHaveClass("ui-button--secondary");

    click("Беру");
    await screen.findByRole("alert");

    expect(button("Ещё раз")).toHaveClass("ui-button--primary");
    expect(button("Беру")).toHaveClass("ui-button--secondary");
  });

  test("после 409 «Ещё раз» приносит нового героя, ошибка пропадает, «Беру» снова доступна", async () => {
    const { server, onCreated } = await rollFirst({
      take: (call) =>
        (call.body as { preview_id: string }).preview_id === FIRST_ID
          ? {
              status: 409,
              json: {
                detail: "Этот герой больше не доступен. Бросьте ещё раз.",
              },
            }
          : taken(),
    });
    click("Беру");
    await screen.findByRole("alert");

    click("Ещё раз");

    await screen.findByRole("heading", { name: "Волшебник" });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(button("Беру")).toBeEnabled();
    click("Беру");
    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(calls(server, TAKE).map((call) => call.body)).toEqual([
      { mode: "random", preview_id: FIRST_ID },
      { mode: "random", preview_id: SECOND_ID },
    ]);
  });

  test("429 (нейронка занята): текст сервера, герой цел, повтор шлёт тот же id", async () => {
    let attempts = 0;
    const { server, onCreated } = await rollFirst({
      take: () =>
        ++attempts === 1
          ? {
              status: 429,
              json: { detail: "Нейронка ещё отвечает на ваш прошлый запрос." },
            }
          : taken(),
    });

    click("Беру");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Нейронка ещё отвечает на ваш прошлый запрос.",
    );
    expect(button("Беру")).toBeEnabled();
    click("Беру");
    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(calls(server, TAKE).map((call) => call.body)).toEqual([
      { mode: "random", preview_id: FIRST_ID },
      { mode: "random", preview_id: FIRST_ID },
    ]);
  });
});
