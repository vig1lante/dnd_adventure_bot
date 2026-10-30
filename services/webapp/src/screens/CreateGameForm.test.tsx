import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { ApiContext } from "../api/context";
import { createApi } from "../api/client";
import type { AdventureSummary, ProviderOption } from "../api/types";
import { sessionToken, useGame } from "../store/game";
import { fakeServer, gameSummary, signInAs } from "../test-setup";
import type { FakeReply, RecordedCall } from "../test-setup";
import { CreateGameForm } from "./CreateGameForm";

// --- данные сервера ---

const CRYPT: AdventureSummary = {
  id: "demo-crypt",
  title: "Склеп забытого короля",
  pitch: "Отряд спускается в старый склеп под деревней.",
  target_hours: 2,
  score: 92,
  blocking: false,
};
const TOWER: AdventureSummary = {
  id: "broken-tower",
  title: "Сломанная башня",
  pitch: "Башня, которую не доделали.",
  target_hours: 3,
  score: 41,
  blocking: true,
};
const FOREST: AdventureSummary = {
  id: "forest-path",
  title: "Лесная тропа",
  pitch: "Дорога через старый лес.",
  target_hours: 1.5,
  score: 80,
  blocking: false,
};

const ANTHROPIC: ProviderOption = {
  id: "anthropic_api",
  title: "Claude через API Anthropic",
  available: true,
  default_model: "claude-opus-5-5",
  models: ["claude-opus-5-5"],
  accepts_model: true,
  is_default: true,
};
const OPENAI: ProviderOption = {
  id: "openai_compatible",
  title: "Сервер, совместимый с OpenAI",
  available: false,
  default_model: null,
  models: [],
  accepts_model: true,
  is_default: false,
};
// Модель Claude Code выбирается только из списка сервера (псевдонимы CLI).
const CLI: ProviderOption = {
  id: "claude_code_cli",
  title: "Claude Code на этом компьютере (только dev)",
  available: true,
  default_model: null,
  models: ["opus", "sonnet", "haiku"],
  accepts_model: true,
  is_default: false,
};

// Заводской .env: провайдер по умолчанию — заглушка без сценария, и она не настроена.
const FAKE_NO_SCENARIO: ProviderOption = {
  id: "fake",
  title: "Заглушка по сценарию (только dev)",
  available: false,
  default_model: null,
  models: [],
  accepts_model: false,
  is_default: true,
};

interface Setup {
  adventures?: AdventureSummary[];
  providers?: ProviderOption[];
  create?: FakeReply | ((call: RecordedCall) => FakeReply);
  adventuresReply?: FakeReply;
  providersReply?: FakeReply;
  onCancel?: () => void;
}

function renderForm(setup: Setup = {}) {
  signInAs({ id: "u1", name: "Вика", can_create_games: true });
  const server = fakeServer({
    "GET /api/adventures": setup.adventuresReply ?? {
      json: setup.adventures ?? [CRYPT, TOWER, FOREST],
    },
    "GET /api/providers": setup.providersReply ?? {
      json: { providers: setup.providers ?? [ANTHROPIC, OPENAI, CLI] },
    },
    "POST /api/games": setup.create ?? { status: 201, json: gameSummary() },
  });
  const onCancel = setup.onCancel ?? vi.fn();
  render(
    <ApiContext value={createApi(server.fetch, sessionToken)}>
      <CreateGameForm onCancel={onCancel} />
    </ApiContext>,
  );
  return { server, onCancel };
}

/** Форма показана: оба списка загружены. */
async function loadedForm(setup: Setup = {}) {
  const rendered = renderForm(setup);
  await screen.findByRole("radiogroup", { name: "Приключение" });
  return rendered;
}

const submit = () =>
  fireEvent.click(screen.getByRole("button", { name: "Создать" }));

const posted = (server: { calls: RecordedCall[] }) =>
  server.calls.filter(
    (call) => call.method === "POST" && call.path === "/api/games",
  );

const typeInto = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });

describe("загрузка списков", () => {
  test("пока списки идут — загрузка, затем форма", async () => {
    renderForm();

    expect(screen.getByRole("status")).toHaveTextContent("Загружаем");
    expect(
      await screen.findByRole("radiogroup", { name: "Приключение" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("status")).toBeNull();
  });

  test("приключения и провайдеры запрашиваются с токеном сессии и без query", async () => {
    const { server } = await loadedForm();

    for (const path of ["/api/adventures", "/api/providers"]) {
      const call = server.calls.find(
        (c) => c.method === "GET" && c.path === path,
      );
      expect(call, path).toBeDefined();
      expect(call?.headers.get("Authorization")).toBe("Bearer tok-u1");
      expect(call?.url).toBe(path);
    }
  });
});

describe("сбой загрузки", () => {
  test("ошибка списка приключений показана, «Повторить» запрашивает заново", async () => {
    let attempts = 0;
    signInAs({ id: "u1", name: "Вика", can_create_games: true });
    const server = fakeServer({
      "GET /api/adventures": () =>
        ++attempts === 1
          ? { status: 500, json: { detail: "Сервер сломался." } }
          : { json: [CRYPT] },
      "GET /api/providers": { json: { providers: [ANTHROPIC] } },
    });
    render(
      <ApiContext value={createApi(server.fetch, sessionToken)}>
        <CreateGameForm onCancel={() => {}} />
      </ApiContext>,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Сервер сломался.",
    );
    fireEvent.click(screen.getByRole("button", { name: "Повторить" }));

    expect(
      await screen.findByRole("radiogroup", { name: "Приключение" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("ошибка списка провайдеров тоже показана", async () => {
    renderForm({
      providersReply: {
        status: 403,
        json: { detail: "Создавать игры могут только игроки из списка." },
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Создавать игры могут только игроки из списка.",
    );
    expect(screen.queryByRole("radiogroup")).toBeNull();
  });
});

describe("выбор приключения", () => {
  test("в списке все приключения каталога, выбрано первое доступное", async () => {
    await loadedForm();

    expect(
      screen.getAllByRole("radio", { name: /Склеп|Башня|Лесная/ }),
    ).toHaveLength(3);
    expect(
      screen.getByRole("radio", { name: /Склеп забытого короля/ }),
    ).toBeChecked();
  });

  test("у приключения видны завязка и длительность", async () => {
    await loadedForm();

    const crypt = screen.getByRole("radio", { name: /Склеп забытого короля/ });
    expect(crypt).toHaveAccessibleName(/Отряд спускается в старый склеп/);
    expect(crypt).toHaveAccessibleName(/2 ч/);
  });

  test("приключение с блокирующими ошибками неактивно и показывает балл", async () => {
    await loadedForm();

    const tower = screen.getByRole("radio", { name: /Сломанная башня/ });
    expect(tower).toBeDisabled();
    expect(tower).toHaveAccessibleName(/балл/i);
    expect(tower).toHaveAccessibleName(/41/);
  });

  test("неактивное приключение нельзя выбрать: ни по щелчку, ни при отправке", async () => {
    const { server } = await loadedForm();

    fireEvent.click(screen.getByRole("radio", { name: /Сломанная башня/ }));
    expect(
      screen.getByRole("radio", { name: /Склеп забытого короля/ }),
    ).toBeChecked();
    submit();

    await vi.waitFor(() => expect(posted(server)).toHaveLength(1));
    expect(posted(server)[0].body).toMatchObject({
      adventure_id: "demo-crypt",
    });
  });

  test("выбранное игроком приключение уходит на сервер", async () => {
    const { server } = await loadedForm();

    fireEvent.click(screen.getByRole("radio", { name: /Лесная тропа/ }));
    submit();

    await vi.waitFor(() => expect(posted(server)).toHaveLength(1));
    expect(posted(server)[0].body).toMatchObject({
      adventure_id: "forest-path",
    });
  });

  test("если первое приключение заблокировано, выбрано первое доступное", async () => {
    await loadedForm({ adventures: [TOWER, FOREST] });

    expect(screen.getByRole("radio", { name: /Лесная тропа/ })).toBeChecked();
  });

  test("все приключения заблокированы: объяснение и «Создать» неактивна", async () => {
    await loadedForm({ adventures: [TOWER] });

    expect(screen.getByRole("button", { name: "Создать" })).toBeDisabled();
    expect(
      screen.getByText(/нет приключений, которые можно/i),
    ).toBeInTheDocument();
  });

  test("каталог пуст: объяснение и «Создать» неактивна", async () => {
    renderForm({ adventures: [] });

    expect(
      await screen.findByText(/на сервере нет приключений/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Создать" })).toBeDisabled();
    // Про блокирующие ошибки молчим: приключений нет вовсе, а не все заблокированы.
    expect(screen.queryByText(/блокирующие ошибки/i)).toBeNull();
  });
});

describe("выбор провайдера и модели", () => {
  test("провайдеры из списка сервера, недоступный неактивен", async () => {
    await loadedForm();

    const group = screen.getByRole("radiogroup", {
      name: "Нейронка для мастера",
    });
    expect(within(group).getAllByRole("radio")).toHaveLength(3);
    expect(
      within(group).getByRole("radio", { name: /Claude через API Anthropic/ }),
    ).toBeEnabled();
    const openai = within(group).getByRole("radio", {
      name: /совместимый с OpenAI/,
    });
    expect(openai).toBeDisabled();
    expect(openai).toHaveAccessibleName(/не настроен/i);
  });

  test("пояснение у провайдеров: модель по умолчанию, ничего или «Не настроен»", async () => {
    await loadedForm();

    const radio = (name: RegExp) => screen.getByRole("radio", { name });
    expect(radio(/Claude через API Anthropic/)).toHaveAccessibleName(
      /Модель по умолчанию: claude-opus-5-5/,
    );
    expect(radio(/Claude через API Anthropic/)).not.toHaveAccessibleName(
      /не настроен/i,
    );
    // Доступный провайдер без модели по умолчанию получает только пометку о живой модели.
    expect(radio(/Claude Code на этом компьютере/)).not.toHaveAccessibleName(
      /Модель по умолчанию|не настроен/i,
    );
    expect(radio(/совместимый с OpenAI/)).toHaveAccessibleName(
      /Не настроен на сервере/,
    );
  });

  test("недоступного провайдера не выбрать", async () => {
    await loadedForm();

    fireEvent.click(
      screen.getByRole("radio", { name: /совместимый с OpenAI/ }),
    );

    expect(
      screen.getByRole("radio", { name: /Claude через API Anthropic/ }),
    ).toBeChecked();
  });

  test("провайдер по умолчанию выбран заранее, модель по умолчанию подставлена", async () => {
    await loadedForm();

    expect(
      screen.getByRole("radio", { name: /Claude через API Anthropic/ }),
    ).toBeChecked();
    expect(screen.getByLabelText("Модель")).toHaveValue("claude-opus-5-5");
  });

  test("всё по умолчанию: на сервер уходит только приключение", async () => {
    const { server } = await loadedForm();

    submit();

    await vi.waitFor(() => expect(posted(server)).toHaveLength(1));
    expect(posted(server)[0].body).toEqual({ adventure_id: "demo-crypt" });
  });

  test("свою модель можно ввести: уходит model, а провайдер остаётся по умолчанию", async () => {
    const { server } = await loadedForm();

    typeInto("Модель", "claude-sonnet-5");
    submit();

    await vi.waitFor(() => expect(posted(server)).toHaveLength(1));
    expect(posted(server)[0].body).toEqual({
      adventure_id: "demo-crypt",
      model: "claude-sonnet-5",
    });
  });

  test("пустая модель — берётся модель по умолчанию, поле model не шлётся", async () => {
    const { server } = await loadedForm();

    typeInto("Модель", "  ");
    submit();

    await vi.waitFor(() => expect(posted(server)).toHaveLength(1));
    expect(posted(server)[0].body).toEqual({ adventure_id: "demo-crypt" });
  });

  test("другой доступный провайдер уходит на сервер по id", async () => {
    const { server } = await loadedForm();

    fireEvent.click(
      screen.getByRole("radio", { name: /Claude Code на этом компьютере/ }),
    );
    submit();

    await vi.waitFor(() => expect(posted(server)).toHaveLength(1));
    expect(posted(server)[0].body).toEqual({
      adventure_id: "demo-crypt",
      provider: "claude_code_cli",
    });
  });

  test("провайдер без выбора модели: поля «Модель» нет, есть пояснение", async () => {
    await loadedForm({
      providers: [
        ANTHROPIC,
        { ...FAKE_NO_SCENARIO, available: true, is_default: false },
      ],
    });

    fireEvent.click(
      screen.getByRole("radio", { name: /Заглушка по сценарию/ }),
    );

    expect(screen.queryByLabelText("Модель")).toBeNull();
    expect(screen.getByText(/сам выбирает модель/i)).toBeInTheDocument();
  });

  test("при смене провайдера правка модели не переносится на другого", async () => {
    await loadedForm({
      providers: [
        ANTHROPIC,
        {
          ...OPENAI,
          available: true,
          default_model: "gpt-x",
          models: ["gpt-x"],
        },
      ],
    });

    typeInto("Модель", "claude-sonnet-5");
    fireEvent.click(
      screen.getByRole("radio", { name: /совместимый с OpenAI/ }),
    );
    expect(screen.getByLabelText("Модель")).toHaveValue("gpt-x");
    fireEvent.click(
      screen.getByRole("radio", { name: /Claude через API Anthropic/ }),
    );

    expect(screen.getByLabelText("Модель")).toHaveValue("claude-sonnet-5");
  });

  test("провайдер по умолчанию не настроен: заранее ничего не выбрано, «Создать» неактивна", async () => {
    await loadedForm({
      providers: [{ ...ANTHROPIC, available: false }, CLI],
    });

    const group = screen.getByRole("radiogroup", {
      name: "Нейронка для мастера",
    });
    expect(within(group).queryByRole("radio", { checked: true })).toBeNull();
    expect(screen.getByRole("button", { name: "Создать" })).toBeDisabled();
    expect(screen.getByText(/выберите нейронку/i)).toBeInTheDocument();
    // Доступные нейронки есть, поэтому «нет доступных» не говорим.
    expect(screen.queryByText(/нет доступных нейронок/i)).toBeNull();
  });

  test("после явного выбора доступной нейронки «Создать» оживает, а провайдер уходит на сервер", async () => {
    const { server } = await loadedForm({
      providers: [{ ...ANTHROPIC, available: false }, CLI],
    });

    fireEvent.click(
      screen.getByRole("radio", { name: /Claude Code на этом компьютере/ }),
    );

    expect(screen.queryByText(/выберите нейронку/i)).toBeNull();
    expect(screen.getByRole("button", { name: "Создать" })).toBeEnabled();
    submit();
    await vi.waitFor(() => expect(posted(server)).toHaveLength(1));
    expect(posted(server)[0].body).toEqual({
      adventure_id: "demo-crypt",
      provider: "claude_code_cli",
    });
  });

  test("шаблонный .env: доступен только Claude Code на этом компьютере — сам он не выбран", async () => {
    const { server } = await loadedForm({
      providers: [
        { ...ANTHROPIC, available: false, is_default: false },
        OPENAI,
        CLI,
        FAKE_NO_SCENARIO,
      ],
    });

    const cli = screen.getByRole("radio", {
      name: /Claude Code на этом компьютере/,
    });
    expect(cli).toBeEnabled();
    expect(cli).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Создать" })).toBeDisabled();
    // Нажатие «Создать» (или Enter) без выбора живую модель не запускает.
    fireEvent.submit(cli.closest("form") as HTMLFormElement);
    expect(posted(server)).toHaveLength(0);
  });

  test("у Claude Code на этом компьютере пометка: живая модель по подписке, только dev", async () => {
    await loadedForm();

    expect(
      screen.getByRole("radio", { name: /Claude Code на этом компьютере/ }),
    ).toHaveAccessibleName(
      /Живая модель по подписке на этом компьютере \(только dev\)/,
    );
    // У остальных такой пометки нет.
    expect(
      screen.getByRole("radio", { name: /Claude через API Anthropic/ }),
    ).not.toHaveAccessibleName(/по подписке/);
  });

  test("пометка живой модели привязана к провайдеру claude_code_cli, а не к названию", async () => {
    await loadedForm({
      providers: [ANTHROPIC, { ...CLI, title: "Локальный Claude" }],
    });

    expect(
      screen.getByRole("radio", { name: /Локальный Claude/ }),
    ).toHaveAccessibleName(/по подписке на этом компьютере/);
  });

  test("недоступному Claude Code пометки о подписке нет: только «Не настроен»", async () => {
    await loadedForm({ providers: [ANTHROPIC, { ...CLI, available: false }] });

    const cli = screen.getByRole("radio", {
      name: /Claude Code на этом компьютере/,
    });
    expect(cli).toHaveAccessibleName(/Не настроен на сервере/);
    expect(cli).not.toHaveAccessibleName(/по подписке/);
  });

  test("Claude Code — провайдер сервера по умолчанию: владелец выбрал его сам, он выбран заранее", async () => {
    const { server } = await loadedForm({
      providers: [
        { ...ANTHROPIC, is_default: false },
        { ...CLI, is_default: true },
      ],
    });

    expect(
      screen.getByRole("radio", { name: /Claude Code на этом компьютере/ }),
    ).toBeChecked();
    submit();
    await vi.waitFor(() => expect(posted(server)).toHaveLength(1));
    expect(posted(server)[0].body).toEqual({ adventure_id: "demo-crypt" });
  });

  test("список провайдеров пуст: выбора нет, объяснение и «Создать» неактивна", async () => {
    await loadedForm({ providers: [] });

    expect(
      screen.queryByRole("radiogroup", { name: "Нейронка для мастера" }),
    ).toBeNull();
    expect(screen.getByText(/нет доступных нейронок/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Создать" })).toBeDisabled();
  });

  test("провайдер один: выбор всё равно показан, и он выбран", async () => {
    await loadedForm({ providers: [ANTHROPIC] });

    const group = screen.getByRole("radiogroup", {
      name: "Нейронка для мастера",
    });
    expect(within(group).getAllByRole("radio")).toHaveLength(1);
    expect(within(group).getByRole("radio")).toBeChecked();
  });

  test("доступных провайдеров нет: объяснение и «Создать» неактивна", async () => {
    await loadedForm({
      providers: [{ ...ANTHROPIC, available: false }, OPENAI],
    });

    expect(screen.getByRole("button", { name: "Создать" })).toBeDisabled();
    expect(screen.getByText(/нет доступных нейронок/i)).toBeInTheDocument();
  });

  test("в prod заводского по умолчанию нет: ничего не выбрано, выбранный вручную уходит явно", async () => {
    const { server } = await loadedForm({
      providers: [{ ...ANTHROPIC, is_default: false }],
    });

    const only = within(
      screen.getByRole("radiogroup", { name: "Нейронка для мастера" }),
    ).getByRole("radio");
    expect(only).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Создать" })).toBeDisabled();
    fireEvent.click(only);
    submit();

    await vi.waitFor(() => expect(posted(server)).toHaveLength(1));
    expect(posted(server)[0].body).toEqual({
      adventure_id: "demo-crypt",
      provider: "anthropic_api",
    });
  });
});

describe("модель Claude Code на этом компьютере", () => {
  const chooseCli = () =>
    fireEvent.click(
      screen.getByRole("radio", { name: /Claude Code на этом компьютере/ }),
    );
  const modelSelect = () => screen.getByRole("combobox", { name: "Модель" });
  const optionNames = () =>
    within(modelSelect())
      .getAllByRole("option")
      .map((option) => option.textContent);

  test("модель выбирается из списка сервера, а не вводится текстом", async () => {
    await loadedForm();

    chooseCli();

    expect(optionNames()).toEqual(["По умолчанию", "opus", "sonnet", "haiku"]);
    expect(modelSelect()).toHaveValue("");
    expect(screen.queryByRole("textbox", { name: "Модель" })).toBeNull();
    expect(screen.queryByText(/сам выбирает модель/i)).toBeNull();
  });

  test("выбранная модель уходит на сервер", async () => {
    const { server } = await loadedForm();

    chooseCli();
    fireEvent.change(modelSelect(), { target: { value: "sonnet" } });
    submit();

    await vi.waitFor(() => expect(posted(server)).toHaveLength(1));
    expect(posted(server)[0].body).toEqual({
      adventure_id: "demo-crypt",
      provider: "claude_code_cli",
      model: "sonnet",
    });
  });

  test("без выбора модели поле model не отправляется", async () => {
    const { server } = await loadedForm();

    chooseCli();
    submit();

    await vi.waitFor(() => expect(posted(server)).toHaveLength(1));
    expect(posted(server)[0].body).not.toHaveProperty("model");
  });

  test("модель по умолчанию сервера (CLAUDE_CLI_MODEL) выбрана заранее и сама не отправляется", async () => {
    const { server } = await loadedForm({
      providers: [
        ANTHROPIC,
        {
          ...CLI,
          default_model: "sonnet[1m]",
          models: ["opus", "sonnet", "haiku", "sonnet[1m]"],
        },
      ],
    });

    chooseCli();

    expect(modelSelect()).toHaveValue("sonnet[1m]");
    expect(optionNames()).toEqual(["opus", "sonnet", "haiku", "sonnet[1m]"]);
    submit();
    await vi.waitFor(() => expect(posted(server)).toHaveLength(1));
    expect(posted(server)[0].body).toEqual({
      adventure_id: "demo-crypt",
      provider: "claude_code_cli",
    });
  });

  test("другая модель вместо заданной по умолчанию уходит на сервер", async () => {
    const { server } = await loadedForm({
      providers: [ANTHROPIC, { ...CLI, default_model: "sonnet" }],
    });

    chooseCli();
    fireEvent.change(modelSelect(), { target: { value: "haiku" } });
    submit();

    await vi.waitFor(() => expect(posted(server)).toHaveLength(1));
    expect(posted(server)[0].body).toMatchObject({ model: "haiku" });
  });

  test("вернули «По умолчанию» после выбора — поле model снова не уходит", async () => {
    const { server } = await loadedForm();

    chooseCli();
    fireEvent.change(modelSelect(), { target: { value: "opus" } });
    fireEvent.change(modelSelect(), { target: { value: "" } });
    expect(modelSelect()).toHaveValue("");
    submit();

    await vi.waitFor(() => expect(posted(server)).toHaveLength(1));
    expect(posted(server)[0].body).toEqual({
      adventure_id: "demo-crypt",
      provider: "claude_code_cli",
    });
  });

  test("выбор модели Claude Code не уходит с другим провайдером и ждёт возврата к нему", async () => {
    const { server } = await loadedForm();
    const chooseAnthropic = () =>
      fireEvent.click(
        screen.getByRole("radio", { name: /Claude через API Anthropic/ }),
      );

    chooseCli();
    fireEvent.change(modelSelect(), { target: { value: "haiku" } });
    chooseAnthropic();
    // У Anthropic своё текстовое поле со своей моделью по умолчанию, списка CLI нет.
    expect(screen.queryByRole("combobox", { name: "Модель" })).toBeNull();
    expect(screen.getByRole("textbox", { name: "Модель" })).toHaveValue(
      "claude-opus-5-5",
    );
    chooseCli();
    expect(modelSelect()).toHaveValue("haiku");
    chooseAnthropic();
    submit();

    await vi.waitFor(() => expect(posted(server)).toHaveLength(1));
    expect(posted(server)[0].body).toEqual({ adventure_id: "demo-crypt" });
  });

  test("сервер сказал accepts_model: false — списка нет, пояснение, model не уходит", async () => {
    const { server } = await loadedForm({
      providers: [
        ANTHROPIC,
        { ...CLI, accepts_model: false, default_model: "sonnet" },
      ],
    });

    chooseCli();

    expect(screen.queryByLabelText("Модель")).toBeNull();
    expect(
      screen.getByText("Этот вариант сам выбирает модель."),
    ).toBeInTheDocument();
    submit();
    await vi.waitFor(() => expect(posted(server)).toHaveLength(1));
    expect(posted(server)[0].body).toEqual({
      adventure_id: "demo-crypt",
      provider: "claude_code_cli",
    });
  });

  test("модель не из списка сервера (список устарел): 422 с текстом сервера, выбор не сброшен, другая модель создаёт игру", async () => {
    // Форма загрузила список, когда у сервера был CLAUDE_CLI_MODEL=sonnet[1m]; потом его убрали,
    // и сервер принимает только псевдонимы. Отказ и его текст — как у `games.service.check_model`.
    const REFUSAL =
      "Модель для Claude Code — одна из списка: opus, sonnet, haiku.";
    const { server } = await loadedForm({
      providers: [
        ANTHROPIC,
        { ...CLI, models: ["opus", "sonnet", "haiku", "sonnet[1m]"] },
      ],
      create: (call) =>
        (call.body as { model?: string }).model === "sonnet[1m]"
          ? { status: 422, json: { detail: REFUSAL } }
          : { status: 201, json: gameSummary({ id: "g-cli" }) },
    });
    const profanity = () =>
      screen.getByRole("switch", { name: "Мат и грубая речь" });

    chooseCli();
    fireEvent.click(profanity());
    fireEvent.change(modelSelect(), { target: { value: "sonnet[1m]" } });
    submit();

    expect(await screen.findByRole("alert")).toHaveTextContent(REFUSAL);
    // Значение со скобками ушло ровно как в списке, вместе с выключенным матом.
    expect(posted(server)[0].body).toEqual({
      adventure_id: "demo-crypt",
      provider: "claude_code_cli",
      model: "sonnet[1m]",
      profanity: false,
    });
    expect(useGame.getState().screen).not.toBe("lobby");
    expect(profanity()).not.toBeChecked();
    expect(modelSelect()).toHaveValue("sonnet[1m]");
    expect(screen.getByRole("button", { name: "Создать" })).toBeEnabled();

    fireEvent.change(modelSelect(), { target: { value: "sonnet" } });
    submit();

    await vi.waitFor(() =>
      expect(useGame.getState()).toMatchObject({
        screen: "lobby",
        gameId: "g-cli",
      }),
    );
    expect(posted(server)).toHaveLength(2);
    expect(posted(server)[1].body).toEqual({
      adventure_id: "demo-crypt",
      provider: "claude_code_cli",
      model: "sonnet",
      profanity: false,
    });
  });
});

describe("мат и грубая речь", () => {
  const profanity = () =>
    screen.getByRole("switch", { name: "Мат и грубая речь" });

  test("переключатель включён по умолчанию, флажок на сервер не уходит", async () => {
    const { server } = await loadedForm();

    expect(profanity()).toBeChecked();
    submit();

    await vi.waitFor(() => expect(posted(server)).toHaveLength(1));
    expect(posted(server)[0].body).toEqual({ adventure_id: "demo-crypt" });
  });

  test("выключенный мат уходит на сервер: profanity: false", async () => {
    const { server } = await loadedForm();

    fireEvent.click(profanity());
    expect(profanity()).not.toBeChecked();
    submit();

    await vi.waitFor(() => expect(posted(server)).toHaveLength(1));
    expect(posted(server)[0].body).toEqual({
      adventure_id: "demo-crypt",
      profanity: false,
    });
  });

  test("включённый обратно — снова как по умолчанию", async () => {
    const { server } = await loadedForm();

    fireEvent.click(profanity());
    fireEvent.click(profanity());
    submit();

    await vi.waitFor(() => expect(posted(server)).toHaveLength(1));
    expect(posted(server)[0].body).not.toHaveProperty("profanity");
  });
});

describe("длительности отсчётов", () => {
  test("не заданы — не отправляются", async () => {
    const { server } = await loadedForm();

    submit();

    await vi.waitFor(() => expect(posted(server)).toHaveLength(1));
    expect(posted(server)[0].body).not.toHaveProperty("collect_seconds");
    expect(posted(server)[0].body).not.toHaveProperty("hurry_seconds");
    expect(posted(server)[0].body).not.toHaveProperty("camp_seconds");
    expect(posted(server)[0].body).not.toHaveProperty("short_rest_seconds");
  });

  test("заданные в границах сервера уходят целыми числами", async () => {
    const { server } = await loadedForm();

    typeInto("Сбор ходов, сек.", "45");
    typeInto("«Поторопить», сек.", "20");
    typeInto("Долгий привал, сек.", "600");
    typeInto("Короткий привал, сек.", "90");
    submit();

    await vi.waitFor(() => expect(posted(server)).toHaveLength(1));
    expect(posted(server)[0].body).toEqual({
      adventure_id: "demo-crypt",
      collect_seconds: 45,
      hurry_seconds: 20,
      camp_seconds: 600,
      short_rest_seconds: 90,
    });
  });

  test.each([
    ["Сбор ходов, сек.", "29"],
    ["Сбор ходов, сек.", "601"],
    ["«Поторопить», сек.", "14"],
    ["«Поторопить», сек.", "301"],
    ["Долгий привал, сек.", "29"],
    ["Долгий привал, сек.", "901"],
    ["Короткий привал, сек.", "29"],
    ["Короткий привал, сек.", "601"],
    ["Короткий привал, сек.", "1.5"],
    ["Сбор ходов, сек.", "1.5"],
    ["Сбор ходов, сек.", "abc"],
    ["Сбор ходов, сек.", "-60"],
    ["Сбор ходов, сек.", "1e2"],
  ] as const)(
    "%s = «%s» вне границ: ошибка у поля, запроса нет",
    async (label, value) => {
      const { server } = await loadedForm();

      typeInto(label, value);
      submit();

      expect(screen.getByLabelText(label)).toBeInvalid();
      expect(screen.getByRole("alert")).toHaveTextContent(/от \d+ до \d+/i);
      expect(posted(server)).toHaveLength(0);
    },
  );

  test.each([
    ["Сбор ходов, сек.", "30", "collect_seconds", 30],
    ["Сбор ходов, сек.", "600", "collect_seconds", 600],
    ["«Поторопить», сек.", "15", "hurry_seconds", 15],
    ["«Поторопить», сек.", "300", "hurry_seconds", 300],
    ["Долгий привал, сек.", "30", "camp_seconds", 30],
    ["Долгий привал, сек.", "900", "camp_seconds", 900],
    ["Короткий привал, сек.", "30", "short_rest_seconds", 30],
    ["Короткий привал, сек.", "600", "short_rest_seconds", 600],
  ] as const)(
    "%s = %s — граница допустима",
    async (label, value, field, expected) => {
      const { server } = await loadedForm();

      typeInto(label, value);
      submit();

      await vi.waitFor(() => expect(posted(server)).toHaveLength(1));
      expect(posted(server)[0].body).toMatchObject({ [field]: expected });
    },
  );

  test("поле короткого привала с подсказкой о границах сервера", async () => {
    await loadedForm();

    expect(
      screen.getByLabelText("Короткий привал, сек."),
    ).toHaveAccessibleDescription(
      "От 30 до 600. Пусто — как задано на сервере.",
    );
  });

  test("исправленное значение снимает ошибку", async () => {
    await loadedForm();

    typeInto("Сбор ходов, сек.", "5");
    expect(screen.getByLabelText("Сбор ходов, сек.")).toBeInvalid();
    typeInto("Сбор ходов, сек.", "60");

    expect(screen.getByLabelText("Сбор ходов, сек.")).toBeValid();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("создание", () => {
  test("успех: открывается лобби новой игры", async () => {
    await loadedForm({
      create: {
        status: 201,
        json: gameSummary({ id: "g-new", code: "NEW123" }),
      },
    });

    submit();

    await vi.waitFor(() =>
      expect(useGame.getState()).toMatchObject({
        screen: "lobby",
        gameId: "g-new",
      }),
    );
  });

  test("запрос идёт с токеном сессии", async () => {
    const { server } = await loadedForm();

    submit();

    await vi.waitFor(() => expect(posted(server)).toHaveLength(1));
    expect(posted(server)[0].headers.get("Authorization")).toBe(
      "Bearer tok-u1",
    );
  });

  test("отказ сервера: его русский текст и подробности (problems) у формы", async () => {
    await loadedForm({
      create: {
        status: 422,
        json: {
          detail:
            "Это приключение нельзя использовать: в нём есть блокирующие ошибки.",
          problems: [
            "Локация «Подвал»: нет выхода",
            "Монстр «Крыса»: нет стат-блока",
          ],
        },
      },
    });

    submit();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("в нём есть блокирующие ошибки");
    expect(alert).toHaveTextContent("Локация «Подвал»: нет выхода");
    expect(alert).toHaveTextContent("Монстр «Крыса»: нет стат-блока");
    expect(useGame.getState().screen).not.toBe("lobby");
    expect(screen.getByRole("button", { name: "Создать" })).toBeEnabled();
  });

  test("одинаковые подробности отказа не ломают список: предупреждений React нет", async () => {
    const warn = vi.spyOn(console, "error").mockImplementation(() => {});
    await loadedForm({
      create: {
        status: 422,
        json: {
          detail: "Это приключение нельзя использовать.",
          problems: [
            "Локация «Подвал»: нет выхода",
            "Локация «Подвал»: нет выхода",
          ],
        },
      },
    });

    submit();

    const alert = await screen.findByRole("alert");
    expect(within(alert).getAllByRole("listitem")).toHaveLength(2);
    expect(warn).not.toHaveBeenCalled();
  });

  test("403 не создателю: текст сервера", async () => {
    await loadedForm({
      create: {
        status: 403,
        json: {
          detail:
            "Создавать игры могут только игроки из списка владельца сервера.",
        },
      },
    });

    submit();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "только игроки из списка",
    );
  });

  test("пока запрос идёт, повторное нажатие не создаёт вторую игру", async () => {
    let release: (reply: FakeReply) => void = () => {};
    const pending = new Promise<FakeReply>((resolve) => {
      release = resolve;
    });
    signInAs({ id: "u1", name: "Вика", can_create_games: true });
    const server = fakeServer({
      "GET /api/adventures": { json: [CRYPT] },
      "GET /api/providers": { json: { providers: [ANTHROPIC] } },
    });
    const slowFetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === "POST") {
          const reply = await pending;
          return new Response(JSON.stringify(reply.json), {
            status: reply.status ?? 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        return server.fetch(input, init);
      },
    );
    render(
      <ApiContext
        value={createApi(slowFetch as unknown as typeof fetch, sessionToken)}
      >
        <CreateGameForm onCancel={() => {}} />
      </ApiContext>,
    );
    await screen.findByRole("radiogroup", { name: "Приключение" });

    submit();
    const button = screen.getByRole("button", { name: "Создать" });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    await act(async () => release({ status: 201, json: gameSummary() }));

    const creations = slowFetch.mock.calls.filter(
      ([, init]) => init?.method === "POST",
    );
    expect(creations).toHaveLength(1);
  });

  test("отправка формы, пока она не готова, запроса не создаёт (например, Enter в поле)", async () => {
    const { server } = await loadedForm();
    typeInto("Сбор ходов, сек.", "5"); // меньше границы сервера: форма не готова
    const form = screen
      .getByRole("radiogroup", { name: "Приключение" })
      .closest("form") as HTMLFormElement;

    fireEvent.submit(form);

    expect(posted(server)).toHaveLength(0);
    expect(screen.getByRole("button", { name: "Создать" })).toBeDisabled();
  });

  test("отправка формы без выбранного приключения запроса не создаёт", async () => {
    const { server } = await loadedForm({ adventures: [TOWER] });
    const form = screen
      .getByRole("radiogroup", { name: "Нейронка для мастера" })
      .closest("form") as HTMLFormElement;

    fireEvent.submit(form);

    expect(posted(server)).toHaveLength(0);
  });

  test("«Отмена» закрывает форму без запроса", async () => {
    const onCancel = vi.fn();
    const { server } = await loadedForm({ onCancel });

    fireEvent.click(screen.getByRole("button", { name: "Отмена" }));

    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(posted(server)).toHaveLength(0);
  });
});
