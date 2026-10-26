import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { createApi } from "../../api/client";
import { ApiContext } from "../../api/context";
import { sessionToken } from "../../store/game";
import { fakeServer, signInAs } from "../../test-setup";
import { DraftEditor } from "./DraftEditor";
import {
  actOf,
  callsTo,
  deityOf,
  draftOf,
  encounterOf,
  exitOf,
  factionOf,
  featureOf,
  itemOf,
  jobOf,
  locationOf,
  monsterOf,
  npcOf,
  openStep,
  optionValues,
  refsOf,
  regionOf,
  reportOf,
  relationOf,
  renderEditor,
  revelationOf,
  secretOf,
  stepsOf,
} from "./fixtures";
import { ADVENTURE_ID_HINT, ID_HINT } from "./labels";

// Подменяются только таймеры экрана: React, промисы и ответы заглушки работают как обычно.
const FAKE_TIMERS: (
  "setTimeout" | "clearTimeout" | "setInterval" | "clearInterval"
)[] = ["setTimeout", "clearTimeout", "setInterval", "clearInterval"];

const tick = (ms: number) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

const opened = () =>
  screen.findByRole("navigation", { name: "Шаги приключения" });
const stepButton = (name: string) =>
  within(
    screen.getByRole("navigation", { name: "Шаги приключения" }),
  ).getByRole("button", { name: new RegExp(name) });
const panel = (name: string) => screen.getByRole("region", { name });
const button = (name: string | RegExp) => screen.getByRole("button", { name });

const CONCEPT_PATH = "/api/drafts/d1/sections/settings";

describe("полоска шагов", () => {
  test("двенадцать шагов по порядку со статусами сервера, затем «Проверка» и «Публикация»", async () => {
    renderEditor(
      draftOf({
        steps: stepsOf({
          concept: { status: "ready", updated_at: "2026-10-25T10:00:00Z" },
          world: { status: "draft", updated_at: "2026-10-25T10:01:00Z" },
          locations: { status: "ready", updated_at: "2026-10-25T10:02:00Z" },
          location_contents: {
            status: "draft",
            updated_at: "2026-10-25T10:03:00Z",
          },
          relations: { status: "ready", updated_at: "2026-10-25T10:04:00Z" },
          encounters: { status: "draft", updated_at: "2026-10-25T10:05:00Z" },
          items: { status: "ready", updated_at: "2026-10-25T10:06:00Z" },
        }),
      }),
    );
    await opened();

    expect(
      within(screen.getByRole("navigation", { name: "Шаги приключения" }))
        .getAllByRole("button")
        .map(
          (step) => step.querySelector(".authoring-step__label")?.textContent,
        ),
    ).toEqual([
      "Замысел",
      "Мир",
      "Боги",
      "Фракции",
      "Сюжет",
      "Карта",
      "Наполнение",
      "Персонажи",
      "Связи",
      "Бои и боссы",
      "Предметы и награды",
      "Улики",
      "Проверка",
      "Публикация",
    ]);
    expect(stepButton("Замысел")).toHaveTextContent("готово");
    expect(stepButton("Мир")).toHaveTextContent("черновик");
    expect(stepButton("Боги")).toHaveTextContent("пусто");
    expect(stepButton("Фракции")).toHaveTextContent("пусто");
    expect(stepButton("Сюжет")).toHaveTextContent("пусто");
    // Карта и Наполнение — один раздел locations, но статусы у них свои: шаги генерации разные.
    expect(stepButton("Карта")).toHaveTextContent("готово");
    expect(stepButton("Наполнение")).toHaveTextContent("черновик");
    expect(stepButton("Персонажи")).toHaveTextContent("пусто");
    expect(stepButton("Связи")).toHaveTextContent("готово");
    expect(stepButton("Бои и боссы")).toHaveTextContent("черновик");
    expect(stepButton("Предметы и награды")).toHaveTextContent("готово");
    expect(stepButton("Улики")).toHaveTextContent("пусто");
    // У проверки и публикации своего статуса нет: это не шаги генерации.
    for (const screenName of ["Проверка", "Публикация"]) {
      expect(stepButton(screenName)).not.toHaveTextContent(
        /пусто|черновик|готово/,
      );
    }
  });

  test("замок целого шага блока 6 — у его экрана: step:clues у «Улик»", async () => {
    renderEditor(draftOf({ locks: ["step:clues"] }));
    await opened();

    expect(stepButton("Улики")).toHaveTextContent("закреплён");
    expect(stepButton("Бои и боссы")).not.toHaveTextContent("закреплён");
  });

  test("замок целого шага блока 5 — у его экрана: step:location_contents у «Наполнения»", async () => {
    renderEditor(draftOf({ locks: ["step:location_contents"] }));
    await opened();

    expect(stepButton("Наполнение")).toHaveTextContent("закреплён");
    expect(stepButton("Карта")).not.toHaveTextContent("закреплён");
  });

  test("закреплённый целиком шаг помечен замком", async () => {
    renderEditor(draftOf({ locks: ["step:world"] }));
    await opened();

    expect(stepButton("Мир")).toHaveTextContent("закреплён");
    expect(stepButton("Замысел")).not.toHaveTextContent("закреплён");
  });

  test("сначала открыт Замысел; выбор шага показывает его форму", async () => {
    renderEditor(draftOf());
    await opened();

    expect(panel("Замысел")).toBeInTheDocument();
    expect(stepButton("Замысел")).toHaveAttribute("aria-current", "step");
    expect(screen.queryByRole("region", { name: "Мир" })).toBeNull();

    fireEvent.click(stepButton("Мир"));

    expect(panel("Мир")).toBeInTheDocument();
    expect(stepButton("Мир")).toHaveAttribute("aria-current", "step");
    expect(screen.queryByRole("region", { name: "Замысел" })).toBeNull();
  });

  test("несохранённая правка шага переживает переход на другой шаг", async () => {
    renderEditor(draftOf());
    await opened();

    fireEvent.change(
      within(panel("Замысел")).getByRole("textbox", { name: "Название" }),
      { target: { value: "Гавань" } },
    );
    fireEvent.click(stepButton("Мир"));
    fireEvent.click(stepButton("Замысел"));

    expect(
      within(panel("Замысел")).getByRole("textbox", { name: "Название" }),
    ).toHaveValue("Гавань");
  });

  test("«К списку приключений» закрывает редактор", async () => {
    const { onClose } = renderEditor(draftOf());
    await opened();

    fireEvent.click(button("К списку приключений"));

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test("сбой загрузки черновика: текст сервера и «Повторить»", async () => {
    let attempts = 0;
    renderEditor(draftOf(), {
      "GET /api/drafts/d1": () =>
        ++attempts === 1
          ? { status: 404, json: { detail: "Черновик не найден." } }
          : { json: draftOf() },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Черновик не найден.",
    );
    fireEvent.click(button("Повторить"));

    expect(await opened()).toBeInTheDocument();
  });
});

describe("ответы сервера на запись", () => {
  const saveConcept = () =>
    fireEvent.click(
      within(panel("Замысел")).getByRole("button", {
        name: "Сохранить раздел",
      }),
    );

  test("сохранение уходит с версией черновика, ответ становится новым черновиком", async () => {
    const { server } = renderEditor(draftOf({ version: 7 }), {
      [`PUT ${CONCEPT_PATH}`]: {
        json: draftOf({
          version: 8,
          steps: stepsOf({
            concept: { status: "draft", updated_at: "2026-10-25T10:00:00Z" },
          }),
        }),
      },
    });
    await opened();

    saveConcept();

    await vi.waitFor(() =>
      expect(stepButton("Замысел")).toHaveTextContent("черновик"),
    );
    expect(callsTo(server, "PUT", CONCEPT_PATH)[0].body).toMatchObject({
      version: 7,
    });
  });

  test("409 version_conflict: диалог «Черновик изменился, обновить?»; «Обновить» перечитывает черновик", async () => {
    let reads = 0;
    const { server } = renderEditor(draftOf(), {
      "GET /api/drafts/d1": () => ({
        json: draftOf({
          version: ++reads,
          title: reads > 1 ? "Чужая правка" : "",
        }),
      }),
      [`PUT ${CONCEPT_PATH}`]: {
        status: 409,
        json: { detail: "Черновик изменился.", code: "version_conflict" },
      },
    });
    await opened();

    saveConcept();
    const dialog = await screen.findByRole("alertdialog", {
      name: "Черновик изменился, обновить?",
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Обновить" }));

    await vi.waitFor(() =>
      expect(
        within(panel("Замысел")).getByRole("textbox", { name: "Название" }),
      ).toHaveValue("Чужая правка"),
    );
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(callsTo(server, "GET", "/api/drafts/d1")).toHaveLength(2);
  });

  test("в диалоге конфликта «Отмена» закрывает его и оставляет правки", async () => {
    const { server } = renderEditor(draftOf(), {
      [`PUT ${CONCEPT_PATH}`]: {
        status: 409,
        json: { detail: "Черновик изменился.", code: "version_conflict" },
      },
    });
    await opened();
    fireEvent.change(
      within(panel("Замысел")).getByRole("textbox", { name: "Название" }),
      { target: { value: "Моя правка" } },
    );

    saveConcept();
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Отмена" }));

    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(
      within(panel("Замысел")).getByRole("textbox", { name: "Название" }),
    ).toHaveValue("Моя правка");
    expect(callsTo(server, "GET", "/api/drafts/d1")).toHaveLength(1);
  });

  test("409 job_running — не конфликт версии: текст сервера без диалога", async () => {
    renderEditor(draftOf(), {
      [`PUT ${CONCEPT_PATH}`]: {
        status: 409,
        json: {
          detail: "Идёт генерация: дождитесь её окончания.",
          code: "job_running",
        },
      },
    });
    await opened();

    saveConcept();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Идёт генерация: дождитесь её окончания.",
    );
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  test("422: текст сервера и подробности списком", async () => {
    renderEditor(draftOf(), {
      [`PUT ${CONCEPT_PATH}`]: {
        status: 422,
        json: {
          detail: "Раздел не прошёл проверку.",
          problems: ["party_size: не больше 6", "title: не длиннее 100 знаков"],
          code: "invalid_data",
        },
      },
    });
    await opened();

    saveConcept();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Раздел не прошёл проверку.");
    expect(within(alert).getAllByRole("listitem")).toHaveLength(2);
    expect(alert).toHaveTextContent("party_size: не больше 6");
  });
});

describe("закрепление раздела-одиночки", () => {
  test("«Закрепить раздел» у Замысла шлёт замки с step:concept и версией", async () => {
    const { server } = renderEditor(
      draftOf({ version: 3, locks: ["deity:tiamat"] }),
      {
        "PUT /api/drafts/d1/locks": {
          json: draftOf({
            version: 4,
            locks: ["deity:tiamat", "step:concept"],
          }),
        },
      },
    );
    await opened();
    const lock = within(panel("Замысел")).getByRole("button", {
      name: "Закрепить раздел",
    });
    expect(lock).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(lock);

    await vi.waitFor(() =>
      expect(lock).toHaveAttribute("aria-pressed", "true"),
    );
    expect(callsTo(server, "PUT", "/api/drafts/d1/locks")[0].body).toEqual({
      version: 3,
      locks: ["deity:tiamat", "step:concept"],
    });
    expect(stepButton("Замысел")).toHaveTextContent("закреплён");
  });

  test("повторное нажатие снимает закрепление", async () => {
    const { server } = renderEditor(
      draftOf({ version: 3, locks: ["step:plot", "deity:tiamat"] }),
      {
        "PUT /api/drafts/d1/locks": {
          json: draftOf({ version: 4, locks: ["deity:tiamat"] }),
        },
      },
    );
    await opened();
    fireEvent.click(stepButton("Сюжет"));

    fireEvent.click(
      within(panel("Сюжет")).getByRole("button", { name: "Закрепить раздел" }),
    );

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", "/api/drafts/d1/locks")).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", "/api/drafts/d1/locks")[0].body).toEqual({
      version: 3,
      locks: ["deity:tiamat"],
    });
  });
});

// Закрепление фиксирует то, что знает сервер. Пока в форме есть несохранённые правки, закрепить
// можно было бы не то, что видит автор, а следующая генерация стёрла бы правки (находка reviewer B4).
describe("несохранённые правки шага: закрепление и генерация", () => {
  const SAVE_FIRST =
    "Сначала сохраните раздел: закрепить можно только сохранённое.";
  const concept = () => panel("Замысел");
  const titleBox = () =>
    within(concept()).getByRole("textbox", { name: "Название" });
  const sectionLock = () =>
    within(concept()).getByRole("button", { name: "Закрепить раздел" });
  const generate = () =>
    within(concept()).getByRole("button", { name: "Сгенерировать шаг" });

  test("общая подсказка у генерации: сначала сохранить раздел, затем закрепить", async () => {
    renderEditor(draftOf());
    await opened();

    expect(concept()).toHaveTextContent(
      "Сначала сохраните раздел, затем закрепите то, что хотите сохранить.",
    );
  });

  test("правка без сохранения закрывает «Закрепить раздел» с подсказкой; после «Сохранить раздел» — открыт", async () => {
    const { server } = renderEditor(draftOf({ version: 3 }), {
      [`PUT ${CONCEPT_PATH}`]: {
        json: draftOf({ version: 4, title: "Гавань" }),
      },
    });
    await opened();
    expect(sectionLock()).toBeEnabled();
    expect(within(concept()).queryByText(SAVE_FIRST)).toBeNull();

    fireEvent.change(titleBox(), { target: { value: "Гавань" } });

    expect(sectionLock()).toBeDisabled();
    expect(within(concept()).getByText(SAVE_FIRST)).toBeInTheDocument();

    fireEvent.click(
      within(concept()).getByRole("button", { name: "Сохранить раздел" }),
    );

    await vi.waitFor(() => expect(sectionLock()).toBeEnabled());
    expect(within(concept()).queryByText(SAVE_FIRST)).toBeNull();
    expect(titleBox()).toHaveValue("Гавань");
    expect(callsTo(server, "PUT", CONCEPT_PATH)[0].body).toMatchObject({
      version: 3,
      data: { title: "Гавань" },
    });
  });

  test("правка, возвращённая к сохранённому, «Закрепить» не держит", async () => {
    renderEditor(draftOf({ title: "Маяк" }));
    await opened();

    fireEvent.change(titleBox(), { target: { value: "Маяк!" } });
    expect(sectionLock()).toBeDisabled();
    fireEvent.change(titleBox(), { target: { value: "Маяк" } });

    expect(sectionLock()).toBeEnabled();
  });

  test("при несохранённых правках у «Сгенерировать шаг» — предупреждение, кнопка доступна", async () => {
    renderEditor(draftOf());
    await opened();
    expect(concept()).not.toHaveTextContent(/несохранённые правки/);

    fireEvent.change(titleBox(), { target: { value: "Гавань" } });

    expect(concept()).toHaveTextContent(
      "В шаге есть несохранённые правки: генерация их заменит.",
    );
    expect(generate()).toBeEnabled();
  });
});

// Набранное пожелание — текст автора: пересоздание формы раздела (после «Сохранить раздел») не
// должно его сбрасывать (находка reviewer B4, attempt 002). Сохранённое на сервере пожелание поле
// берёт после генерации и после «Обновить».
describe("пожелание шага не теряется при сохранении раздела", () => {
  const concept = () => panel("Замысел");
  const wishIn = (step: string) =>
    within(panel(step)).getByRole("textbox", { name: "Что хочу увидеть" });
  const saveIn = (step: string) =>
    fireEvent.click(
      within(panel(step)).getByRole("button", { name: "Сохранить раздел" }),
    );

  test("пожелание, набранное до «Сохранить раздел», на месте; генерация шлёт его, затем ставит задание", async () => {
    const { server } = renderEditor(draftOf({ version: 3 }), {
      [`PUT ${CONCEPT_PATH}`]: {
        json: draftOf({ version: 4, title: "Маяк" }),
      },
      "PUT /api/drafts/d1/wishes/concept": {
        json: draftOf({
          version: 5,
          title: "Маяк",
          wishes: { concept: "Морская готика" },
        }),
      },
      "POST /api/drafts/d1/generate": {
        status: 202,
        json: jobOf({ step: "concept" }),
      },
    });
    await opened();
    fireEvent.change(
      within(concept()).getByRole("textbox", { name: "Название" }),
      { target: { value: "Маяк" } },
    );
    fireEvent.change(wishIn("Замысел"), {
      target: { value: "Морская готика" },
    });

    saveIn("Замысел");
    await vi.waitFor(() =>
      expect(
        within(concept()).getByRole("button", { name: "Закрепить раздел" }),
      ).toBeEnabled(),
    );

    expect(wishIn("Замысел")).toHaveValue("Морская готика");
    fireEvent.click(
      within(concept()).getByRole("button", { name: "Сгенерировать шаг" }),
    );
    expect(await screen.findByText("Генерирую…")).toBeInTheDocument();
    expect(server.calls.map((call) => `${call.method} ${call.path}`)).toEqual([
      "GET /api/drafts/d1",
      `PUT ${CONCEPT_PATH}`,
      "PUT /api/drafts/d1/wishes/concept",
      "POST /api/drafts/d1/generate",
    ]);
    expect(server.calls[2].body).toEqual({
      version: 4,
      text: "Морская готика",
    });
    expect(server.calls[3].body).toEqual({ step: "concept" });
  });

  test("несохранённое пожелание другого шага переживает переход между шагами и сохранение текущего раздела", async () => {
    renderEditor(draftOf(), {
      [`PUT ${CONCEPT_PATH}`]: {
        json: draftOf({ version: 2, title: "Маяк" }),
      },
    });
    await opened();
    fireEvent.click(stepButton("Боги"));
    fireEvent.change(wishIn("Боги"), { target: { value: "Боги моря" } });
    fireEvent.click(stepButton("Замысел"));
    fireEvent.change(
      within(concept()).getByRole("textbox", { name: "Название" }),
      { target: { value: "Маяк" } },
    );

    saveIn("Замысел");
    await vi.waitFor(() =>
      expect(
        within(concept()).getByRole("button", { name: "Закрепить раздел" }),
      ).toBeEnabled(),
    );
    fireEvent.click(stepButton("Боги"));

    expect(wishIn("Боги")).toHaveValue("Боги моря");
  });

  test("после «Обновить» поле пожелания берёт сохранённое на сервере", async () => {
    let reads = 0;
    renderEditor(draftOf(), {
      "GET /api/drafts/d1": () => ({
        json: draftOf({
          version: ++reads,
          wishes: reads > 1 ? { concept: "Из другой вкладки" } : {},
        }),
      }),
      [`PUT ${CONCEPT_PATH}`]: VERSION_CONFLICT,
    });
    await opened();
    fireEvent.change(wishIn("Замысел"), { target: { value: "Моё" } });

    saveIn("Замысел");
    const dialog = await screen.findByRole("alertdialog");
    expect(wishIn("Замысел")).toHaveValue("Моё");
    fireEvent.click(within(dialog).getByRole("button", { name: "Обновить" }));

    await vi.waitFor(() =>
      expect(wishIn("Замысел")).toHaveValue("Из другой вкладки"),
    );
  });

  test("после генерации поле пожелания берёт сохранённое на сервере", async () => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
    let reads = 0;
    renderEditor(draftOf(), {
      "GET /api/drafts/d1": () => ({
        json:
          ++reads === 1
            ? draftOf()
            : draftOf({ version: 3, wishes: { concept: "Морская готика" } }),
      }),
      "PUT /api/drafts/d1/wishes/concept": {
        json: draftOf({ version: 2, wishes: { concept: "Морская готика" } }),
      },
      "POST /api/drafts/d1/generate": {
        status: 202,
        json: jobOf({ id: "j1", step: "concept" }),
      },
      "GET /api/drafts/d1/jobs/j1": {
        json: jobOf({ id: "j1", status: "done" }),
      },
    });
    await tick(0);
    // Пробелы по краям сервер обрезает: поле покажет его текст.
    fireEvent.change(wishIn("Замысел"), {
      target: { value: "  Морская готика  " },
    });

    fireEvent.click(
      within(concept()).getByRole("button", { name: "Сгенерировать шаг" }),
    );
    await tick(0);
    await tick(2000);

    expect(screen.queryByText("Генерирую…")).toBeNull();
    expect(wishIn("Замысел")).toHaveValue("Морская готика");
  });
});

describe("генерация шага", () => {
  const wishField = (step: string) =>
    within(panel(step)).getByRole("textbox", { name: "Что хочу увидеть" });
  const generateButton = (step: string) =>
    within(panel(step)).getByRole("button", { name: "Сгенерировать шаг" });

  test("рядом с кнопкой — подсказка: незакреплённые ручные правки шаг заменит", async () => {
    renderEditor(draftOf());
    await opened();

    expect(panel("Замысел")).toHaveTextContent(/незакреплённые ручные правки/);
  });

  test("новое пожелание сначала сохраняется с версией, затем ставится задание шага", async () => {
    const { server } = renderEditor(draftOf({ version: 5 }), {
      "PUT /api/drafts/d1/wishes/concept": {
        json: draftOf({ version: 6, wishes: { concept: "Мрачный порт" } }),
      },
      "POST /api/drafts/d1/generate": {
        status: 202,
        json: jobOf({ step: "concept" }),
      },
    });
    await opened();

    fireEvent.change(wishField("Замысел"), {
      target: { value: "Мрачный порт" },
    });
    fireEvent.click(generateButton("Замысел"));

    expect(await screen.findByText("Генерирую…")).toBeInTheDocument();
    expect(server.calls.map((call) => `${call.method} ${call.path}`)).toEqual([
      "GET /api/drafts/d1",
      "PUT /api/drafts/d1/wishes/concept",
      "POST /api/drafts/d1/generate",
    ]);
    expect(server.calls[1].body).toEqual({ version: 5, text: "Мрачный порт" });
    expect(server.calls[2].body).toEqual({ step: "concept" });
  });

  test("пожелание не менялось — сразу задание, без записи пожелания", async () => {
    const { server } = renderEditor(
      draftOf({ wishes: { pantheon: "Боги моря" } }),
      {
        "POST /api/drafts/d1/generate": {
          status: 202,
          json: jobOf({ step: "pantheon" }),
        },
      },
    );
    await opened();
    fireEvent.click(stepButton("Боги"));

    expect(wishField("Боги")).toHaveValue("Боги моря");
    fireEvent.click(generateButton("Боги"));

    await screen.findByText("Генерирую…");
    expect(
      callsTo(server, "PUT", "/api/drafts/d1/wishes/pantheon"),
    ).toHaveLength(0);
    expect(callsTo(server, "POST", "/api/drafts/d1/generate")[0].body).toEqual({
      step: "pantheon",
    });
  });

  test("шаг, закреплённый целиком, не генерируется", async () => {
    renderEditor(draftOf({ locks: ["step:world"] }));
    await opened();
    fireEvent.click(stepButton("Мир"));

    expect(generateButton("Мир")).toBeDisabled();
    expect(panel("Мир")).toHaveTextContent(/закреплён целиком/);
  });

  test("отказ постановки задания (422 step_locked) показан текстом сервера", async () => {
    renderEditor(draftOf(), {
      "POST /api/drafts/d1/generate": {
        status: 422,
        json: { detail: "Шаг закреплён.", code: "step_locked" },
      },
    });
    await opened();

    fireEvent.click(generateButton("Замысел"));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Шаг закреплён.",
    );
    expect(screen.queryByText("Генерирую…")).toBeNull();
  });

  test("пока задание идёт, правка заблокирована: поля, сохранение, замки и генерация", async () => {
    renderEditor(draftOf({ job: jobOf({ status: "running" }) }));
    await opened();

    expect(screen.getByText("Генерирую…")).toBeInTheDocument();
    const concept = panel("Замысел");
    expect(
      within(concept).getByRole("textbox", { name: "Название" }),
    ).toBeDisabled();
    expect(
      within(concept).getByRole("button", { name: "Сохранить раздел" }),
    ).toBeDisabled();
    expect(
      within(concept).getByRole("button", { name: "Закрепить раздел" }),
    ).toBeDisabled();
    expect(generateButton("Замысел")).toBeDisabled();
    // Смотреть другие шаги во время генерации можно.
    expect(stepButton("Мир")).toBeEnabled();
  });
});

// Часы подменены с самого начала: интервал опроса заводится, как только задание стало активным,
// в том числе сразу при открытии черновика. `vi.waitFor` здесь не годится — с подменёнными часами
// он сам двигает время; ответы заглушки дожидаются `settle()` (сдвиг на 0 мс).
describe("опрос задания раз в 2 секунды", () => {
  const settle = () => tick(0);
  const generateButton = () =>
    within(panel("Замысел")).getByRole("button", { name: "Сгенерировать шаг" });
  const title = () =>
    within(panel("Замысел")).getByRole("textbox", { name: "Название" });

  beforeEach(() => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
  });

  test("на done — остановка и форма из нового черновика", async () => {
    let polls = 0;
    let reads = 0;
    const { server } = renderEditor(draftOf(), {
      // Второе чтение черновика — после done: в нём результат генерации.
      "GET /api/drafts/d1": () => ({
        json:
          ++reads === 1
            ? draftOf()
            : draftOf({ version: 2, title: "Туманная гавань" }),
      }),
      "POST /api/drafts/d1/generate": {
        status: 202,
        json: jobOf({ id: "j1", step: "concept" }),
      },
      "GET /api/drafts/d1/jobs/j1": () => ({
        json: jobOf({ id: "j1", status: ++polls < 2 ? "running" : "done" }),
      }),
    });
    const polled = () => callsTo(server, "GET", "/api/drafts/d1/jobs/j1");
    await settle();

    fireEvent.click(generateButton());
    await settle();
    expect(screen.getByText("Генерирую…")).toBeInTheDocument();

    await tick(1999);
    expect(polled()).toHaveLength(0);
    await tick(1);
    expect(polled()).toHaveLength(1);
    expect(screen.getByText("Генерирую…")).toBeInTheDocument();
    expect(title()).toBeDisabled();

    await tick(2000);
    expect(polled()).toHaveLength(2);
    expect(title()).toHaveValue("Туманная гавань");
    expect(title()).toBeEnabled();
    expect(screen.queryByText("Генерирую…")).toBeNull();

    await tick(10_000);
    expect(polled()).toHaveLength(2);
  });

  test("на failed — остановка, текст ошибки и «Повторить», который ставит задание снова", async () => {
    const { server } = renderEditor(draftOf(), {
      "POST /api/drafts/d1/generate": {
        status: 202,
        json: jobOf({ id: "j1", step: "concept" }),
      },
      "GET /api/drafts/d1/jobs/j1": {
        json: jobOf({
          id: "j1",
          step: "concept",
          status: "failed",
          error: "Нейронка не ответила.",
        }),
      },
    });
    const polled = () => callsTo(server, "GET", "/api/drafts/d1/jobs/j1");
    await settle();

    fireEvent.click(generateButton());
    await settle();
    await tick(2000);

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Не удалось сгенерировать шаг «Замысел»");
    expect(alert).toHaveTextContent("Нейронка не ответила.");
    expect(screen.queryByText("Генерирую…")).toBeNull();
    expect(title()).toBeEnabled();
    await tick(10_000);
    expect(polled()).toHaveLength(1);

    fireEvent.click(button("Повторить"));
    await settle();

    expect(callsTo(server, "POST", "/api/drafts/d1/generate")).toHaveLength(2);
    expect(callsTo(server, "POST", "/api/drafts/d1/generate")[1].body).toEqual({
      step: "concept",
    });
    expect(screen.getByText("Генерирую…")).toBeInTheDocument();
  });

  test("следующий опрос — через 2 с после ответа на предыдущий: запросы не накладываются", async () => {
    signInAs({ id: "u1", name: "Вика", can_create_games: true });
    const server = fakeServer({
      "GET /api/drafts/d1": {
        json: draftOf({ job: jobOf({ id: "j1", status: "running" }) }),
      },
      "GET /api/drafts/d1/jobs/j1": {
        json: jobOf({ id: "j1", status: "running" }),
      },
    });
    // Ответ на первый опрос задерживается, пока тест его не отпустит (медленная связь).
    let release = () => {};
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    let polls = 0;
    const slow: typeof fetch = async (input, init) => {
      if (String(input).includes("/jobs/") && ++polls === 1) await held;
      return server.fetch(input, init);
    };
    render(
      <ApiContext value={createApi(slow, sessionToken)}>
        <DraftEditor draftId="d1" onClose={() => {}} />
      </ApiContext>,
    );
    await settle();

    await tick(2000);
    expect(polls).toBe(1);
    await tick(6000);
    expect(polls).toBe(1);

    release();
    await settle();
    await tick(1999);
    expect(polls).toBe(1);
    await tick(1);
    expect(polls).toBe(2);
  });

  test("черновик открыт во время задания — опрос продолжается", async () => {
    const { server } = renderEditor(
      draftOf({
        job: jobOf({ id: "j7", step: "pantheon", status: "running" }),
      }),
      {
        "GET /api/drafts/d1/jobs/j7": {
          json: jobOf({ id: "j7", step: "pantheon", status: "running" }),
        },
      },
    );
    const polled = () => callsTo(server, "GET", "/api/drafts/d1/jobs/j7");
    await settle();

    expect(screen.getByText(/Шаг «Боги»/)).toBeInTheDocument();
    await tick(2000);
    expect(polled()).toHaveLength(1);
    await tick(2000);
    expect(polled()).toHaveLength(2);
  });

  test("сбой опроса: текст и «Обновить», который перечитывает черновик", async () => {
    let reads = 0;
    const { server } = renderEditor(draftOf(), {
      "GET /api/drafts/d1": () => ({
        json: draftOf({
          job: ++reads === 1 ? jobOf({ id: "j1", status: "running" }) : null,
        }),
      }),
      "GET /api/drafts/d1/jobs/j1": {
        status: 500,
        json: { detail: "Сервер устал." },
      },
    });
    await settle();

    await tick(2000);
    expect(screen.getByRole("alert")).toHaveTextContent("Сервер устал.");
    await tick(10_000);
    expect(callsTo(server, "GET", "/api/drafts/d1/jobs/j1")).toHaveLength(1);

    fireEvent.click(button("Обновить"));
    await settle();

    expect(title()).toBeEnabled();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(callsTo(server, "GET", "/api/drafts/d1")).toHaveLength(2);
  });
});

// --- финальное покрытие блока 4: стыки редактора с сервером ---

const JOB_RUNNING = {
  status: 409,
  json: {
    detail:
      "Идёт генерация шага: дождитесь её окончания, потом правьте черновик.",
    code: "job_running",
  },
};
const VERSION_CONFLICT = {
  status: 409,
  json: {
    detail: "Черновик изменился: обновите его и повторите правку.",
    code: "version_conflict",
  },
};

const jobPolls = (server: { calls: { path: string }[] }) =>
  server.calls.filter((call) => call.path.includes("/jobs/"));

describe("пожелание перед генерацией", () => {
  const wishField = () =>
    within(panel("Замысел")).getByRole("textbox", { name: "Что хочу увидеть" });
  const generateButton = () =>
    within(panel("Замысел")).getByRole("button", { name: "Сгенерировать шаг" });

  test("стёртое пожелание удаляется на сервере пустым текстом, затем ставится задание", async () => {
    const { server } = renderEditor(
      draftOf({ version: 2, wishes: { concept: "Мрачный порт" } }),
      {
        "PUT /api/drafts/d1/wishes/concept": {
          json: draftOf({ version: 3, wishes: {} }),
        },
        "POST /api/drafts/d1/generate": {
          status: 202,
          json: jobOf({ step: "concept" }),
        },
      },
    );
    await opened();
    expect(wishField()).toHaveValue("Мрачный порт");

    fireEvent.change(wishField(), { target: { value: "" } });
    fireEvent.click(generateButton());

    await screen.findByText("Генерирую…");
    expect(
      callsTo(server, "PUT", "/api/drafts/d1/wishes/concept")[0].body,
    ).toEqual({ version: 2, text: "" });
    expect(callsTo(server, "POST", "/api/drafts/d1/generate")[0].body).toEqual({
      step: "concept",
    });
  });

  test("пробелы по краям не делают пожелание новым: записи нет, сразу задание", async () => {
    const { server } = renderEditor(
      draftOf({ wishes: { concept: "Мрачный порт" } }),
      {
        "POST /api/drafts/d1/generate": {
          status: 202,
          json: jobOf({ step: "concept" }),
        },
      },
    );
    await opened();

    fireEvent.change(wishField(), { target: { value: "  Мрачный порт \n" } });
    fireEvent.click(generateButton());

    await screen.findByText("Генерирую…");
    expect(
      callsTo(server, "PUT", "/api/drafts/d1/wishes/concept"),
    ).toHaveLength(0);
  });

  test("конфликт версии при записи пожелания: диалог, задание не ставится", async () => {
    const { server } = renderEditor(draftOf({ version: 5 }), {
      "PUT /api/drafts/d1/wishes/concept": VERSION_CONFLICT,
    });
    await opened();

    fireEvent.change(wishField(), { target: { value: "Морская готика" } });
    fireEvent.click(generateButton());

    expect(
      await screen.findByRole("alertdialog", {
        name: "Черновик изменился, обновить?",
      }),
    ).toBeInTheDocument();
    expect(callsTo(server, "POST", "/api/drafts/d1/generate")).toHaveLength(0);
    expect(screen.queryByText("Генерирую…")).toBeNull();
  });

  test("422 invalid_wish: текст сервера, задание не ставится", async () => {
    const { server } = renderEditor(draftOf(), {
      "PUT /api/drafts/d1/wishes/concept": {
        status: 422,
        json: {
          detail: "Пожелание — от 1 до 2000 знаков.",
          code: "invalid_wish",
        },
      },
    });
    await opened();

    fireEvent.change(wishField(), { target: { value: "очень длинное" } });
    fireEvent.click(generateButton());

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Пожелание — от 1 до 2000 знаков.",
    );
    expect(callsTo(server, "POST", "/api/drafts/d1/generate")).toHaveLength(0);
  });
});

describe("ошибки записи: правки остаются", () => {
  const title = () =>
    within(panel("Замысел")).getByRole("textbox", { name: "Название" });
  const saveConcept = () =>
    fireEvent.click(
      within(panel("Замысел")).getByRole("button", {
        name: "Сохранить раздел",
      }),
    );

  test("обрыв связи при сохранении: понятный текст, правка в форме, можно сохранить снова", async () => {
    renderEditor(draftOf(), {
      [`PUT ${CONCEPT_PATH}`]: () => {
        throw new TypeError("Failed to fetch");
      },
    });
    await opened();
    fireEvent.change(title(), { target: { value: "Гавань" } });

    saveConcept();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Нет связи с сервером. Проверьте интернет и попробуйте ещё раз.",
    );
    expect(title()).toHaveValue("Гавань");
    expect(
      within(panel("Замысел")).getByRole("button", {
        name: "Сохранить раздел",
      }),
    ).toBeEnabled();
  });

  test("422 invalid_data: подробности у раздела, введённое в форме не пропало", async () => {
    renderEditor(draftOf(), {
      [`PUT ${CONCEPT_PATH}`]: {
        status: 422,
        json: {
          detail: "Данные раздела не прошли проверку.",
          code: "invalid_data",
          problems: [
            "settings.title: String should have at most 100 characters",
          ],
        },
      },
    });
    await opened();
    fireEvent.change(title(), { target: { value: "Очень длинное название" } });

    saveConcept();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Данные раздела не прошли проверку.");
    expect(alert).toHaveTextContent("settings.title:");
    expect(title()).toHaveValue("Очень длинное название");
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });
});

describe("конфликт версии на другом шаге", () => {
  test("закрепление бога по чужой версии: «Обновить» перечитывает черновик и оставляет автора на шаге «Боги»", async () => {
    let reads = 0;
    const tiamat = deityOf({ id: "tiamat", name: "Тиамат" });
    const bahamut = deityOf({ id: "bahamut", name: "Бахамут" });
    const { server } = renderEditor(draftOf(), {
      "GET /api/drafts/d1": () => ({
        json:
          ++reads === 1
            ? draftOf({ version: 1, deities: [tiamat] })
            : draftOf({ version: 3, deities: [tiamat, bahamut] }),
      }),
      "PUT /api/drafts/d1/locks": VERSION_CONFLICT,
    });
    await opened();
    fireEvent.click(stepButton("Боги"));

    fireEvent.click(
      within(panel("Боги")).getByRole("button", { name: "Закрепить: Тиамат" }),
    );
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Обновить" }));

    expect(
      await within(
        await screen.findByRole("region", { name: "Боги" }),
      ).findByRole("group", { name: "Бахамут" }),
    ).toBeInTheDocument();
    expect(stepButton("Боги")).toHaveAttribute("aria-current", "step");
    expect(callsTo(server, "PUT", "/api/drafts/d1/locks")[0].body).toEqual({
      version: 1,
      locks: ["deity:tiamat"],
    });
  });
});

// Часы подменены до открытия редактора, как в блоке опроса выше.
describe("задание генерации: стыки с сервером", () => {
  const settle = () => tick(0);
  const generateIn = (step: string) =>
    within(panel(step)).getByRole("button", { name: "Сгенерировать шаг" });

  beforeEach(() => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
  });

  test("генерация «Богов»: на done форма — боги нового черновика, их можно закрепить, шаг «готово»", async () => {
    let reads = 0;
    const { server } = renderEditor(draftOf(), {
      "GET /api/drafts/d1": () => ({
        json:
          ++reads === 1
            ? draftOf()
            : draftOf({
                version: 2,
                deities: [deityOf({ id: "tiamat", name: "Тиамат" })],
                steps: stepsOf({
                  pantheon: {
                    status: "ready",
                    updated_at: "2026-10-25T10:00:02+00:00",
                  },
                }),
                job: jobOf({ id: "j1", step: "pantheon", status: "done" }),
              }),
      }),
      "POST /api/drafts/d1/generate": {
        status: 202,
        json: jobOf({ id: "j1", step: "pantheon" }),
      },
      "GET /api/drafts/d1/jobs/j1": {
        json: jobOf({ id: "j1", step: "pantheon", status: "done" }),
      },
    });
    await settle();
    fireEvent.click(stepButton("Боги"));
    expect(panel("Боги")).toHaveTextContent("Пока пусто.");

    fireEvent.click(generateIn("Боги"));
    await settle();
    expect(screen.getByText(/Шаг «Боги»/)).toBeInTheDocument();
    await tick(2000);

    const gods = panel("Боги");
    expect(
      within(gods).getByRole("group", { name: "Тиамат" }),
    ).toBeInTheDocument();
    expect(
      within(gods).getByRole("button", { name: "Закрепить: Тиамат" }),
    ).toBeEnabled();
    expect(stepButton("Боги")).toHaveTextContent("готово");
    expect(screen.queryByText("Генерирую…")).toBeNull();
    expect(callsTo(server, "POST", "/api/drafts/d1/generate")[0].body).toEqual({
      step: "pantheon",
    });
  });

  test("409 job_running при постановке: текст сервера, без диалога и без опроса", async () => {
    const { server } = renderEditor(draftOf(), {
      "POST /api/drafts/d1/generate": JOB_RUNNING,
    });
    await settle();

    fireEvent.click(generateIn("Замысел"));
    await settle();

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Идёт генерация шага: дождитесь её окончания",
    );
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(screen.queryByText("Генерирую…")).toBeNull();
    await tick(10_000);
    expect(jobPolls(server)).toHaveLength(0);
    expect(generateIn("Замысел")).toBeEnabled();
  });

  test("после нажатия «Сгенерировать шаг» кнопка недоступна: одно задание, один опрос раз в 2 секунды", async () => {
    const { server } = renderEditor(draftOf(), {
      "POST /api/drafts/d1/generate": {
        status: 202,
        json: jobOf({ id: "j1", step: "concept" }),
      },
      "GET /api/drafts/d1/jobs/j1": {
        json: jobOf({ id: "j1", step: "concept", status: "running" }),
      },
    });
    await settle();

    fireEvent.click(generateIn("Замысел"));
    // Запрос постановки ещё в пути: правка и генерация уже закрыты.
    expect(generateIn("Замысел")).toBeDisabled();
    await settle();
    expect(generateIn("Замысел")).toBeDisabled();
    expect(callsTo(server, "POST", "/api/drafts/d1/generate")).toHaveLength(1);

    await tick(2000);
    expect(jobPolls(server)).toHaveLength(1);
    await tick(2000);
    expect(jobPolls(server)).toHaveLength(2);
    await tick(6000);
    expect(jobPolls(server)).toHaveLength(5);
  });

  test("черновик с завершённым последним заданием: опроса и «Генерирую…» нет, правка открыта", async () => {
    const { server } = renderEditor(
      draftOf({
        job: jobOf({
          id: "j1",
          status: "done",
          finished_at: "2026-10-25T10:00:05+00:00",
        }),
      }),
    );
    await settle();

    expect(screen.queryByText("Генерирую…")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(generateIn("Замысел")).toBeEnabled();
    await tick(10_000);
    expect(jobPolls(server)).toHaveLength(0);
  });

  test("черновик с упавшим заданием цели: текст сервера, «Повторить» шлёт шаг и цель, опрашивается только новое задание", async () => {
    const failed = jobOf({
      id: "j1",
      step: "pantheon",
      target: "deity:tiamat",
      status: "failed",
      error: "Генерация не удалась. Попробуйте ещё раз.",
      finished_at: "2026-10-25T10:00:05+00:00",
    });
    const { server } = renderEditor(
      draftOf({ deities: [deityOf()], job: failed }),
      {
        "POST /api/drafts/d1/generate": {
          status: 202,
          json: jobOf({ id: "j2", step: "pantheon", target: "deity:tiamat" }),
        },
        "GET /api/drafts/d1/jobs/j2": {
          json: jobOf({ id: "j2", step: "pantheon", status: "running" }),
        },
      },
    );
    await settle();

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Не удалось сгенерировать шаг «Боги»");
    expect(alert).toHaveTextContent(
      "Генерация не удалась. Попробуйте ещё раз.",
    );
    await tick(4000);
    expect(jobPolls(server)).toHaveLength(0);

    fireEvent.click(button("Повторить"));
    await settle();

    expect(callsTo(server, "POST", "/api/drafts/d1/generate")[0].body).toEqual({
      step: "pantheon",
      target: "deity:tiamat",
    });
    expect(screen.getByText("Генерирую…")).toBeInTheDocument();
    await tick(2000);
    expect(callsTo(server, "GET", "/api/drafts/d1/jobs/j2")).toHaveLength(1);
    expect(callsTo(server, "GET", "/api/drafts/d1/jobs/j1")).toHaveLength(0);
  });
});

describe("справочник ссылок (GET /api/drafts/{id}/refs)", () => {
  const REFS = "/api/drafts/d1/refs";

  test("экраны без выбора ссылок справочник не просят", async () => {
    const { server } = renderEditor(draftOf());
    await opened();

    for (const name of ["Мир", "Боги", "Фракции", "Сюжет", "Карта"]) {
      fireEvent.click(stepButton(name));
    }
    await act(async () => {});

    expect(callsTo(server, "GET", REFS)).toHaveLength(0);
  });

  test("на экране со ссылками справочник грузится, после записи — заново: в нём сохранённое", async () => {
    const saved = draftOf({
      version: 2,
      locations: [locationOf({ id: "crypt", name: "Склеп" })],
    });
    let asked = 0;
    const { server } = renderEditor(draftOf(), {
      [`GET ${REFS}`]: () =>
        ++asked === 1 ? { json: [] } : { json: refsOf(saved) },
      "PUT /api/drafts/d1/sections/npcs": { json: saved },
    });
    await opened();
    fireEvent.click(stepButton("Персонажи"));
    const form = panel("Персонажи");
    await vi.waitFor(() =>
      expect(callsTo(server, "GET", REFS)).toHaveLength(1),
    );
    fireEvent.click(
      within(form).getByRole("button", { name: "Добавить персонажа" }),
    );
    expect(optionValues(form, "Где находится")).toEqual([""]);

    fireEvent.click(
      within(form).getByRole("button", { name: "Сохранить раздел" }),
    );

    await vi.waitFor(() =>
      expect(callsTo(server, "GET", REFS)).toHaveLength(2),
    );
    await vi.waitFor(() =>
      expect(optionValues(panel("Персонажи"), "Где находится")).toEqual([
        "",
        "location:crypt",
      ]),
    );
  });

  test("сбой справочника — текст у поля выбора, остальная правка открыта", async () => {
    renderEditor(draftOf({ npcs: [npcOf()] }), {
      [`GET ${REFS}`]: { status: 500, json: { detail: "Сервер устал." } },
    });
    await opened();
    fireEvent.click(stepButton("Персонажи"));

    const marta = within(panel("Персонажи")).getByRole("group", {
      name: "Марта",
    });
    expect(await within(marta).findByRole("alert")).toHaveTextContent(
      "Список ссылок не загрузился",
    );
    expect(within(marta).getByRole("textbox", { name: "Имя" })).toBeEnabled();
  });
});

// --- финальное покрытие блока 5 ---

/** Черновик блока 5: локация, два персонажа и связь между ними. */
const PEOPLE = draftOf({
  version: 4,
  locations: [locationOf({ id: "crypt", name: "Склеп" })],
  npcs: [
    npcOf({ id: "marta", name: "Марта" }),
    npcOf({ id: "fabian", name: "Фабиан" }),
  ],
  relations: [relationOf()],
});

/** Кнопка «Перегенерировать» сущности; подпись связи берёт имена из справочника — ждём его. */
const regenerateButton = (form: HTMLElement, name: string) =>
  within(form).findByRole("button", { name: `Перегенерировать: ${name}` });

/** Правка поля на экране: форма шага получает несохранённые правки. */
const editIn = (scope: HTMLElement, field: string, value: string) =>
  fireEvent.change(within(scope).getByRole("textbox", { name: field }), {
    target: { value },
  });

// Экран → шаг генерации → цель одной сущности (дополнение B5, §2 и §7); `edit` — любая правка экрана.
describe.each([
  {
    label: "Наполнение",
    step: "location_contents",
    target: "location:crypt",
    name: "Склеп",
    edit: (form: HTMLElement) => editIn(form, "Текст вслух", "Другой текст"),
  },
  {
    label: "Персонажи",
    step: "npcs",
    target: "npc:marta",
    name: "Марта",
    edit: (form: HTMLElement) =>
      editIn(
        within(form).getByRole("group", { name: "Фабиан" }),
        "Мотив",
        "уехать",
      ),
  },
  {
    label: "Связи",
    step: "relations",
    target: "relation:fabian-debtor-marta",
    name: "Фабиан — должник — Марта",
    edit: (form: HTMLElement) => editIn(form, "Пояснение", "долг чести"),
  },
] as const)(
  "«Перегенерировать» на экране «$label»",
  ({ label, step, target, name, edit }) => {
    // Fix-раунд 1 B5 (§9, minor 2): после генерации форма начинается заново с черновиком
    // сервера, поэтому при несохранённых правках кнопка закрыта, как «Закрепить» (B4 §10).
    test("при несохранённых правках экрана закрыта с подсказкой «Сначала сохраните раздел»", async () => {
      const { form } = await openStep(PEOPLE, label);
      const regenerate = await regenerateButton(form, name);
      expect(regenerate).toBeEnabled();

      edit(form);

      expect(regenerate).toBeDisabled();
      expect(regenerate).toHaveAccessibleDescription(
        "Сначала сохраните раздел",
      );
      expect(form).toHaveTextContent(
        "Сначала сохраните раздел: закрепить и перегенерировать можно только сохранённое.",
      );
    });

    test("новое пожелание шага сохраняется первым, затем задание {step, target}", async () => {
      const { server, form } = await openStep(PEOPLE, label, {
        [`PUT /api/drafts/d1/wishes/${step}`]: {
          json: { ...PEOPLE, version: 5, wishes: { [step]: "Мрачнее" } },
        },
        "POST /api/drafts/d1/generate": {
          status: 202,
          json: jobOf({ step, target }),
        },
      });
      fireEvent.change(
        within(form).getByRole("textbox", { name: "Что хочу увидеть" }),
        { target: { value: "Мрачнее" } },
      );
      fireEvent.click(await regenerateButton(form, name));

      expect(await screen.findByText("Генерирую…")).toBeInTheDocument();
      expect(
        server.calls
          .filter((call) => call.method !== "GET")
          .map((call) => [call.method, call.path, call.body]),
      ).toEqual([
        [
          "PUT",
          `/api/drafts/d1/wishes/${step}`,
          { version: 4, text: "Мрачнее" },
        ],
        ["POST", "/api/drafts/d1/generate", { step, target }],
      ]);
    });

    test("закреплённую сущность не перегенерировать", async () => {
      const { form } = await openStep({ ...PEOPLE, locks: [target] }, label);

      expect(await regenerateButton(form, name)).toBeDisabled();
    });

    test("шаг закреплён целиком — кнопка закрыта и у незакреплённой сущности", async () => {
      const { form } = await openStep(
        { ...PEOPLE, locks: [`step:${step}`] },
        label,
      );

      expect(await regenerateButton(form, name)).toBeDisabled();
    });
  },
);

test.each([
  ["Карта", "locations"],
  ["Наполнение", "location_contents"],
  ["Персонажи", "npcs"],
  ["Связи", "relations"],
] as const)(
  "«Сгенерировать шаг» на экране «%s» ставит задание шага %s без цели",
  async (label, step) => {
    const { server, form } = await openStep(PEOPLE, label, {
      "POST /api/drafts/d1/generate": { status: 202, json: jobOf({ step }) },
    });

    fireEvent.click(
      within(form).getByRole("button", { name: "Сгенерировать шаг" }),
    );

    expect(await screen.findByText("Генерирую…")).toBeInTheDocument();
    expect(
      callsTo(server, "POST", "/api/drafts/d1/generate").map(
        (call) => call.body,
      ),
    ).toEqual([{ step }]);
  },
);

test("сбой перезагрузки справочника: прежний список на месте, у поля — текст сбоя", async () => {
  const REFS = "/api/drafts/d1/refs";
  const draft = draftOf({
    locations: [locationOf({ id: "crypt", name: "Склеп" })],
    npcs: [npcOf({ location_id: "crypt" })],
  });
  let asked = 0;
  const { server } = renderEditor(draft, {
    [`GET ${REFS}`]: () =>
      ++asked === 1
        ? { json: refsOf(draft) }
        : { status: 500, json: { detail: "Сервер устал." } },
    "PUT /api/drafts/d1/sections/npcs": { json: { ...draft, version: 2 } },
  });
  await opened();
  fireEvent.click(stepButton("Персонажи"));
  await vi.waitFor(() =>
    expect(optionValues(panel("Персонажи"), "Где находится")).toEqual([
      "",
      "location:crypt",
    ]),
  );

  fireEvent.click(
    within(panel("Персонажи")).getByRole("button", {
      name: "Сохранить раздел",
    }),
  );

  await vi.waitFor(() => expect(callsTo(server, "GET", REFS)).toHaveLength(2));
  const marta = within(panel("Персонажи")).getByRole("group", {
    name: "Марта",
  });
  expect(await within(marta).findByRole("alert")).toHaveTextContent(
    "Список ссылок не загрузился",
  );
  expect(optionValues(marta, "Где находится")).toEqual(["", "location:crypt"]);
  expect(
    within(marta).getByRole("combobox", { name: "Где находится" }),
  ).toHaveValue("location:crypt");
});

// --- финальное покрытие блока 6: одна сущность на экранах «Бои и боссы», «Предметы», «Улики» ---

const FIGHTS = draftOf({
  version: 4,
  locations: [
    locationOf({ id: "crypt", name: "Склеп", encounter_id: "crypt-ghouls" }),
  ],
  encounters: [encounterOf()],
  items: [itemOf()],
  revelations: [revelationOf()],
});
// Справочник SRD нужен только экрану боёв; пустой список — чтобы не было текста сбоя.
const SRD_ROUTE = { "GET /api/srd/monsters": { json: [] } };

// Экран → шаг генерации → цель одной сущности (дополнение B6, §1: цели по `steps.target_id`).
describe.each([
  {
    label: "Бои и боссы",
    step: "encounters",
    target: "encounter:crypt-ghouls",
    name: "Бой crypt-ghouls",
    edit: (form: HTMLElement) => editIn(form, "Тактика", "Бегут"),
  },
  {
    label: "Предметы и награды",
    step: "items",
    target: "item:crypt-key",
    name: "Ключ от склепа",
    edit: (form: HTMLElement) => editIn(form, "Описание", "Ржавый ключ"),
  },
  {
    label: "Улики",
    step: "clues",
    target: "revelation:smuggler",
    name: "Откровение smuggler",
    edit: (form: HTMLElement) => editIn(form, "Вывод", "Фабиан лжёт"),
  },
] as const)(
  "одна сущность на экране «$label»",
  ({ label, step, target, name, edit }) => {
    test("несохранённые правки закрывают «Закрепить» и «Перегенерировать» с подсказкой", async () => {
      const { form } = await openStep(FIGHTS, label, SRD_ROUTE);
      const regenerate = await regenerateButton(form, name);
      const lock = within(form).getByRole("button", {
        name: `Закрепить: ${name}`,
      });
      expect(regenerate).toBeEnabled();
      expect(lock).toBeEnabled();

      edit(form);

      expect(regenerate).toBeDisabled();
      expect(regenerate).toHaveAccessibleDescription(
        "Сначала сохраните раздел",
      );
      expect(lock).toBeDisabled();
      expect(form).toHaveTextContent(
        "Сначала сохраните раздел: закрепить и перегенерировать можно только сохранённое.",
      );
    });

    test("новое пожелание шага сохраняется первым, затем задание {step, target}", async () => {
      const { server, form } = await openStep(FIGHTS, label, {
        ...SRD_ROUTE,
        [`PUT /api/drafts/d1/wishes/${step}`]: {
          json: { ...FIGHTS, version: 5, wishes: { [step]: "Страшнее" } },
        },
        "POST /api/drafts/d1/generate": {
          status: 202,
          json: jobOf({ step, target }),
        },
      });
      fireEvent.change(
        within(form).getByRole("textbox", { name: "Что хочу увидеть" }),
        { target: { value: "Страшнее" } },
      );
      fireEvent.click(await regenerateButton(form, name));

      expect(await screen.findByText("Генерирую…")).toBeInTheDocument();
      expect(
        server.calls
          .filter((call) => call.method !== "GET")
          .map((call) => [call.method, call.path, call.body]),
      ).toEqual([
        [
          "PUT",
          `/api/drafts/d1/wishes/${step}`,
          { version: 4, text: "Страшнее" },
        ],
        ["POST", "/api/drafts/d1/generate", { step, target }],
      ]);
    });

    test("закреплённую сущность не перегенерировать; «Закрепить» нажат", async () => {
      const { form } = await openStep(
        { ...FIGHTS, locks: [target] },
        label,
        SRD_ROUTE,
      );

      expect(await regenerateButton(form, name)).toBeDisabled();
      expect(
        within(form).getByRole("button", { name: `Закрепить: ${name}` }),
      ).toHaveAttribute("aria-pressed", "true");
    });

    test("шаг закреплён целиком — «Перегенерировать» закрыта и у незакреплённой сущности", async () => {
      const { form } = await openStep(
        { ...FIGHTS, locks: [`step:${step}`] },
        label,
        SRD_ROUTE,
      );

      expect(await regenerateButton(form, name)).toBeDisabled();
    });
  },
);

test.each([
  ["Бои и боссы", "encounters"],
  ["Предметы и награды", "items"],
  ["Улики", "clues"],
] as const)(
  "«Сгенерировать шаг» на экране «%s» ставит задание шага %s без цели",
  async (label, step) => {
    const { server, form } = await openStep(FIGHTS, label, {
      ...SRD_ROUTE,
      "POST /api/drafts/d1/generate": { status: 202, json: jobOf({ step }) },
    });

    fireEvent.click(
      within(form).getByRole("button", { name: "Сгенерировать шаг" }),
    );

    expect(await screen.findByText("Генерирую…")).toBeInTheDocument();
    expect(
      callsTo(server, "POST", "/api/drafts/d1/generate").map(
        (call) => call.body,
      ),
    ).toEqual([{ step }]);
  },
);

// --- fix-раунд 1 блока 6 (дополнение B6, §11: R2, R5, R12) ---

describe("подсказка id — правило шаблона сервера на всех полях id (R2)", () => {
  // Сущность каждого вида с полем id: так на каждом экране видны все его поля id.
  const CRYPT = locationOf({
    id: "crypt",
    name: "Склеп",
    exits: [exitOf()],
    features: [featureOf()],
    secrets: [secretOf()],
  });
  const ALL = draftOf({
    world: { history: "", regions: [regionOf()], rumors: [] },
    deities: [deityOf()],
    factions: [factionOf()],
    plot: { acts: [actOf()], endings: [], villain: null },
    locations: [CRYPT],
    npcs: [npcOf()],
    relations: [relationOf()],
    items: [itemOf()],
    encounters: [encounterOf()],
    monsters: [monsterOf()],
    revelations: [revelationOf()],
  });
  // id зоны и находки — строки, а не EntityId движка (у них свои подсказки); id действия
  // статблока подписан «Идентификатор действия» и сюда не попадает.
  const NOT_ENTITY_ID = new Set(["Зоны", "Что раскрывает"]);
  const entityIdFields = (region: HTMLElement) =>
    within(region)
      .getAllByRole("textbox", { name: /^Идентификатор( улики)?$/ })
      .filter(
        (field) =>
          !NOT_ENTITY_ID.has(
            field.closest("fieldset.form-group")?.querySelector("legend")
              ?.textContent ?? "",
          ),
      );
  const description = (field: HTMLElement) =>
    (field.getAttribute("aria-describedby") ?? "")
      .split(" ")
      .map((id) => document.getElementById(id)?.textContent ?? "")
      .join(" ");

  test("текст правила — шаблон EntityId движка (строчная латиница, первый знак — буква или цифра)", () => {
    expect(ID_HINT).toBe(
      "Строчная латиница, цифры, «-» и «_»; начинается с буквы или цифры.",
    );
    expect(ADVENTURE_ID_HINT).toBe(
      "Строчная латиница, цифры, «-» и «_»; начинается с буквы или цифры; до 64 знаков.",
    );
  });

  test.each([
    ["Мир", 1],
    ["Боги", 1],
    ["Фракции", 1],
    ["Сюжет", 1],
    ["Карта", 2],
    ["Наполнение", 3],
    ["Персонажи", 1],
    ["Связи", 1],
    ["Бои и боссы", 2],
    ["Предметы и награды", 1],
    ["Улики", 2],
  ] as const)(
    "«%s»: у каждого поля id (%i) подсказка — правило сервера",
    async (step, count) => {
      const { form } = await openStep(ALL, step, {
        "GET /api/srd/monsters": { json: [] },
      });

      const fields = entityIdFields(form);
      expect(fields).toHaveLength(count);
      for (const field of fields) {
        expect(description(field)).toContain(ID_HINT);
      }
    },
  );

  test("«Замысел»: у id приключения — правило и «до 64 знаков»", async () => {
    renderEditor(ALL);
    await opened();

    expect(
      description(
        within(panel("Замысел")).getByRole("textbox", {
          name: "id приключения",
        }),
      ),
    ).toContain(ADVENTURE_ID_HINT);
  });
});

describe("генерация Замысла пишет и откровения «Улик» (R5)", () => {
  const SMUGGLER = revelationOf();
  const fromClues =
    "На экране «Улики» есть несохранённые правки: они пропадут.";
  const fromConcept =
    "На экране «Замысел» есть несохранённые правки: они пропадут.";

  test("у «Сгенерировать шаг» Замысла — подсказка: заменит и незакреплённые откровения с уликами", async () => {
    renderEditor(draftOf());
    await opened();

    const note = within(panel("Замысел")).getByText(
      "Генерация Замысла заменит и незакреплённые откровения с уликами на шаге «Улики»: закрепите нужные.",
    );
    expect(note.closest(".authoring-generate")).not.toBeNull();
  });

  test("на «Уликах» — строка, что откровения пишет и генерация Замысла", async () => {
    const { form } = await openStep(draftOf(), "Улики");

    expect(form).toHaveTextContent(
      "Откровения пишет и генерация Замысла: незакреплённые она заменит вместе с уликами — закрепите нужные.",
    );
  });

  test("несохранённые правки «Улик»: Замысел предупреждает у генерации, но не у сохранения", async () => {
    renderEditor(draftOf({ revelations: [SMUGGLER] }));
    await opened();
    fireEvent.click(stepButton("Улики"));
    fireEvent.change(
      within(panel("Улики")).getByRole("textbox", { name: "Вывод" }),
      { target: { value: "Фабиан лжёт" } },
    );

    fireEvent.click(stepButton("Замысел"));

    const warnings = within(panel("Замысел")).getAllByText(fromClues);
    expect(warnings).toHaveLength(1);
    expect(warnings[0].closest(".authoring-generate")).not.toBeNull();
  });

  test("несохранённые правки Замысла «Улики» не задевают: предупреждения там нет", async () => {
    renderEditor(draftOf({ revelations: [SMUGGLER] }));
    await opened();
    fireEvent.change(
      within(panel("Замысел")).getByRole("textbox", { name: "Название" }),
      { target: { value: "Маяк" } },
    );

    fireEvent.click(stepButton("Улики"));

    expect(panel("Улики")).not.toHaveTextContent(fromConcept);
  });
});

describe("проверка и публикация говорят о несохранённых правках других экранов (R12)", () => {
  const typeDeityName = (value: string) => {
    fireEvent.click(stepButton("Боги"));
    fireEvent.change(
      within(
        within(panel("Боги")).getByRole("group", { name: "Тиамат" }),
      ).getByRole("textbox", { name: "Имя" }),
      { target: { value } },
    );
  };

  test.each(["Проверка", "Публикация"])(
    "«%s»: строка с экранами, где правки не сохранены; без правок её нет",
    async (screenName) => {
      renderEditor(draftOf({ deities: [deityOf()] }), {
        "POST /api/drafts/d1/validate": { json: reportOf() },
      });
      await opened();

      fireEvent.click(stepButton(screenName));
      expect(panel(screenName)).not.toHaveTextContent(
        "есть несохранённые правки",
      );

      typeDeityName("Бахамут");
      fireEvent.click(stepButton("Замысел"));
      fireEvent.change(
        within(panel("Замысел")).getByRole("textbox", { name: "Название" }),
        { target: { value: "Маяк" } },
      );
      fireEvent.click(stepButton(screenName));

      expect(panel(screenName)).toHaveTextContent(
        "На экранах «Замысел», «Боги» есть несохранённые правки: проверка и публикация их не видят.",
      );
    },
  );

  test("одна несохранённая правка — «На экране «Боги»…»", async () => {
    renderEditor(draftOf({ deities: [deityOf()] }), {
      "POST /api/drafts/d1/validate": { json: reportOf() },
    });
    await opened();

    typeDeityName("Бахамут");
    fireEvent.click(stepButton("Проверка"));

    expect(panel("Проверка")).toHaveTextContent(
      "На экране «Боги» есть несохранённые правки: проверка и публикация их не видят.",
    );
  });
});
