import { fireEvent, within } from "@testing-library/react";
import type { Draft } from "../../api/types";
import type { RecordedCall } from "../../test-setup";
import {
  callsTo,
  choose,
  draftOf,
  exitOf,
  group,
  locationOf,
  openStep,
  optionValues,
  press,
  typeIn,
} from "./fixtures";

const PATH = "/api/drafts/d1/sections/locations";
const LOCKS = "/api/drafts/d1/locks";

const HALL = locationOf({
  id: "hall",
  name: "Зал",
  exits: [exitOf({ id: "down", to: "crypt", direction: "вниз" })],
});
const CRYPT = locationOf({ id: "crypt", name: "Склеп" });

function openMap(draft: Draft, lockedReply?: Draft) {
  return openStep(draft, "Карта", {
    [`PUT ${PATH}`]: {
      json: draftOf({ ...draft, version: draft.version + 1 }),
    },
    [`PUT ${LOCKS}`]: { json: lockedReply ?? draft },
  });
}

const save = (form: HTMLElement) => press(form, "Сохранить раздел");
const savedBody = async (server: { calls: RecordedCall[] }) => {
  await vi.waitFor(() => expect(callsTo(server, "PUT", PATH)).toHaveLength(1));
  return callsTo(server, "PUT", PATH)[0].body;
};

describe("шаг «Карта»", () => {
  test("локации из черновика: выходы «откуда → куда», старт и финал", async () => {
    const { form } = await openMap(
      draftOf({
        locations: [HALL, CRYPT],
        start_location: "hall",
        finale_location: "crypt",
      }),
    );

    const hall = group(form, "Зал");
    const exit = group(hall, "вниз → Склеп");
    expect(within(exit).getByRole("combobox", { name: "Куда" })).toHaveValue(
      "crypt",
    );
    expect(within(form).getByRole("combobox", { name: "Старт" })).toHaveValue(
      "hall",
    );
    expect(within(form).getByRole("combobox", { name: "Финал" })).toHaveValue(
      "crypt",
    );
  });

  test("правка карты уходит объектом раздела с версией: локации, выходы, старт и финал", async () => {
    const { server, form } = await openMap(
      draftOf({ version: 3, locations: [HALL, CRYPT], start_location: "hall" }),
    );

    press(form, "Добавить локацию");
    const fresh = group(form, "Локация 3");
    typeIn(fresh, "Идентификатор", "lighthouse");
    typeIn(fresh, "Название", "Маяк");
    typeIn(fresh, "Регион", "cape");
    typeIn(fresh, "Что знают игроки", "Старый маяк на мысу");
    fireEvent.click(
      within(fresh).getByRole("switch", { name: "Известно с начала" }),
    );
    // Выход ведёт в локацию, которой сервер ещё не знает: «Куда» берёт локации из формы.
    const hall = group(form, "Зал");
    press(hall, "Добавить выход");
    const up = group(hall, "Выход 2");
    typeIn(up, "Идентификатор", "up");
    choose(up, "Куда", "lighthouse");
    typeIn(up, "Направление", "вверх");
    typeIn(up, "Описание", "Винтовая лестница");
    typeIn(up, "Замок", "item:crypt-key");
    choose(up, "Скрытность", "15");
    choose(form, "Финал", "lighthouse");
    save(form);

    expect(await savedBody(server)).toEqual({
      version: 3,
      data: {
        locations: [
          {
            ...HALL,
            exits: [
              HALL.exits[0],
              {
                id: "up",
                to: "lighthouse",
                direction: "вверх",
                description: "Винтовая лестница",
                locked_by: "item:crypt-key",
                hidden_dc: 15,
              },
            ],
          },
          CRYPT,
          {
            public: "Старый маяк на мысу",
            known_at_start: true,
            id: "lighthouse",
            name: "Маяк",
            region: "cape",
            read_aloud: "",
            dm_notes: "",
            exits: [],
            features: [],
            secrets: [],
            encounter_id: null,
            zones: [{ id: "here", name: "здесь" }],
            links: [],
          },
        ],
        start_location: "hall",
        finale_location: "lighthouse",
      },
    });
  });

  test("наполнение локации, которого карта не показывает, сохраняется как было", async () => {
    const filled = locationOf({
      id: "crypt",
      name: "Склеп",
      dm_notes: "Здесь засада",
      features: [],
      zones: [
        { id: "here", name: "здесь" },
        { id: "altar", name: "у алтаря" },
      ],
      links: [{ a: "here", b: "altar", distance: "near" }],
      encounter_id: "crypt-ghouls",
    });
    const { server, form } = await openMap(draftOf({ locations: [filled] }));

    typeIn(group(form, "Склеп"), "Название", "Старый склеп");
    save(form);

    expect(await savedBody(server)).toMatchObject({
      data: { locations: [{ ...filled, name: "Старый склеп" }] },
    });
  });

  test("пустой замок, «не скрыт», снятые старт и финал уходят как null", async () => {
    const locked = locationOf({
      id: "hall",
      name: "Зал",
      exits: [exitOf({ locked_by: "flag:gate-open", hidden_dc: 10 })],
    });
    const { server, form } = await openMap(
      draftOf({
        locations: [locked],
        start_location: "hall",
        finale_location: "hall",
      }),
    );

    const exit = group(group(form, "Зал"), "вниз → crypt");
    typeIn(exit, "Замок", "");
    choose(exit, "Скрытность", "");
    choose(form, "Старт", "");
    choose(form, "Финал", "");
    save(form);

    expect(await savedBody(server)).toMatchObject({
      data: {
        locations: [{ exits: [{ locked_by: null, hidden_dc: null }] }],
        start_location: null,
        finale_location: null,
      },
    });
  });

  test("скрытность выхода — только Сл из таблицы: 5, 10, 15, 20, 25, 30", async () => {
    const { form } = await openMap(draftOf({ locations: [HALL, CRYPT] }));

    expect(
      optionValues(group(group(form, "Зал"), "вниз → Склеп"), "Скрытность"),
    ).toEqual(["", "5", "10", "15", "20", "25", "30"]);
  });

  test("«Куда», старт и финал — локации формы; удалённая локация видна как висящая, а не подменяется", async () => {
    const { form } = await openMap(
      draftOf({ locations: [HALL, CRYPT], finale_location: "crypt" }),
    );

    expect(optionValues(form, "Старт")).toEqual(["", "hall", "crypt"]);
    press(form, "Удалить: Склеп");

    const exit = group(group(form, "Зал"), "вниз → crypt");
    expect(within(exit).getByRole("combobox", { name: "Куда" })).toHaveValue(
      "crypt",
    );
    expect(optionValues(form, "Финал")).toEqual(["", "hall", "crypt"]);
    expect(
      within(within(form).getByRole("combobox", { name: "Финал" })).getByRole(
        "option",
        { name: "crypt — нет в списке" },
      ),
    ).toBeInTheDocument();
  });

  test("«Закрепить» у сохранённой локации шлёт location:<id>; у новой его нет", async () => {
    const { server, form } = await openMap(
      draftOf({ version: 5, locations: [HALL], locks: ["step:plot"] }),
      draftOf({
        version: 6,
        locations: [HALL],
        locks: ["step:plot", "location:hall"],
      }),
    );

    press(form, "Добавить локацию");
    expect(
      within(group(form, "Локация 2")).queryByRole("button", {
        name: /Закрепить/,
      }),
    ).toBeNull();
    press(form, "Удалить: Локация 2");
    press(form, "Закрепить: Зал");

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", LOCKS)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", LOCKS)[0].body).toEqual({
      version: 5,
      locks: ["step:plot", "location:hall"],
    });
  });

  test("при несохранённых правках «Закрепить» закрыт; правка, возвращённая к сохранённому, — открыт", async () => {
    const { form } = await openMap(draftOf({ locations: [HALL] }));
    const lock = () =>
      within(form).getByRole("button", { name: "Закрепить: Зал" });

    typeIn(group(form, "Зал"), "Регион", "cape");
    expect(lock()).toBeDisabled();

    typeIn(group(form, "Зал"), "Регион", "");
    expect(lock()).toBeEnabled();
  });
});

// --- финальное покрытие блока 5 ---

test("удалённая локация уходит разделом без неё; замки удалённых клиент не трогает — их снимает сервер", async () => {
  const { server, form } = await openMap(
    draftOf({
      version: 2,
      locations: [HALL, CRYPT],
      start_location: "hall",
      locks: ["location:crypt"],
    }),
  );

  press(form, "Удалить: Склеп");
  save(form);

  expect(await savedBody(server)).toEqual({
    version: 2,
    data: {
      locations: [HALL],
      start_location: "hall",
      finale_location: null,
    },
  });
  expect(callsTo(server, "PUT", LOCKS)).toHaveLength(0);
});

// Fix-раунд 1 B5 (§9, minor 1 webapp и nit 4): сервер требует старт и финал из этой же карты (B5 §1);
// клиент говорит об этом у поля до записи, а не после круга 422.
test.each([
  ["Старт", { start_location: "crypt" }],
  ["Финал", { finale_location: "crypt" }],
] as const)(
  "поле «%s» с локацией, которой нет на карте формы: ошибка у поля и «Сохранить раздел» закрыт",
  async (field, ends) => {
    const { server, form } = await openMap(
      draftOf({ locations: [HALL, CRYPT], ...ends }),
    );
    const saveButton = () =>
      within(form).getByRole("button", { name: "Сохранить раздел" });
    expect(saveButton()).toBeEnabled();

    press(form, "Удалить: Склеп");

    const select = within(form).getByRole("combobox", { name: field });
    expect(select).toHaveValue("crypt");
    expect(select).toHaveAccessibleDescription(
      expect.stringContaining(
        "Такой локации нет на карте: выберите другую или «не выбран».",
      ),
    );
    expect(saveButton()).toBeDisabled();

    choose(form, field, "hall");
    expect(saveButton()).toBeEnabled();
    save(form);
    expect(await savedBody(server)).toMatchObject({
      data: { locations: [HALL] },
    });
  },
);
