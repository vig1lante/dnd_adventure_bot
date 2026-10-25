// Данные ТОЛЬКО для тестов конструктора. Формы — строго по контракту блоков 4 и 5 (спека §8,
// §10.3 и дополнения dispatcher): живого сервера у тестов нет, заглушки отвечают как он.
// Сущности блока 5 — модели движка, как их отдаёт `model_dump(mode="json")`: все поля, включая
// поля по умолчанию.
import { fireEvent, render, screen, within } from "@testing-library/react";
import { createElement } from "react";
import { createApi } from "../../api/client";
import { ApiContext } from "../../api/context";
import { sessionToken } from "../../store/game";
import { fakeServer, signInAs } from "../../test-setup";
import type { FakeReply, RecordedCall } from "../../test-setup";
import { DraftEditor } from "./DraftEditor";
import type {
  Act,
  AuthoringStep,
  Deity,
  Draft,
  DraftRef,
  DraftSummary,
  Exit,
  Faction,
  Feature,
  Item,
  Job,
  Location,
  LocationSecret,
  Npc,
  Region,
  Relation,
  Rumor,
  StepState,
} from "../../api/types";

const ALL_STEPS: AuthoringStep[] = [
  "concept",
  "world",
  "pantheon",
  "factions",
  "plot",
  "locations",
  "location_contents",
  "npcs",
  "relations",
  "encounters",
  "items",
  "clues",
];

/** Статусы всех 12 шагов: по умолчанию пусто, выбранные — как задано. */
export function stepsOf(
  overrides: Partial<Record<AuthoringStep, StepState>> = {},
): Record<AuthoringStep, StepState> {
  const empty: StepState = { status: "empty", updated_at: null };
  return Object.fromEntries(
    ALL_STEPS.map((step) => [step, overrides[step] ?? empty]),
  ) as Record<AuthoringStep, StepState>;
}

/** Пустой черновик, как его отдаёт `POST /api/drafts`; поля переопределяются точечно. */
export function draftOf(overrides: Partial<Draft> = {}): Draft {
  return {
    id: "d1",
    version: 1,
    title: "",
    adventure_id: null,
    journal_intro: "",
    settings: {
      pitch: "",
      tone: "",
      target_hours: 3,
      party_size: 6,
      party_level: 3,
      modules: [],
      profanity: true,
    },
    world: null,
    deities: [],
    factions: [],
    plot: null,
    locations: [],
    npcs: [],
    relations: [],
    encounters: [],
    monsters: [],
    items: [],
    revelations: [],
    start_location: null,
    finale_location: null,
    locks: [],
    wishes: {},
    steps: stepsOf(),
    job: null,
    ...overrides,
  };
}

export function summaryOf(overrides: Partial<DraftSummary> = {}): DraftSummary {
  return {
    id: "d1",
    title: "Туманная гавань",
    adventure_id: null,
    version: 1,
    updated_at: "2026-10-25T10:00:00+00:00",
    ...overrides,
  };
}

/** Задание генерации; по умолчанию только что поставлено в очередь. */
export function jobOf(overrides: Partial<Job> = {}): Job {
  return {
    id: "j1",
    step: "concept",
    target: null,
    status: "queued",
    error: null,
    created_at: "2026-10-25T10:00:00+00:00",
    finished_at: null,
    ...overrides,
  };
}

export function deityOf(overrides: Partial<Deity> = {}): Deity {
  return {
    id: "tiamat",
    name: "Тиамат",
    title: "Владычица драконов",
    domains: ["обман", "война"],
    symbol: "пятиглавый дракон",
    dogma: "Сила берёт своё.",
    appearance: "Дракон о пяти головах",
    attitude_to_mortals: "презирает",
    secret: "",
    avatar_statblock: null,
    public: "",
    known_at_start: false,
    ...overrides,
  };
}

export function factionOf(overrides: Partial<Faction> = {}): Faction {
  return {
    id: "guild",
    name: "Гильдия теней",
    goal: "Власть над портом",
    methods: "Подкуп и шантаж",
    leader: null,
    base_location: null,
    secret: "",
    attitude_to_party: 0,
    public: "",
    known_at_start: false,
    ...overrides,
  };
}

export function regionOf(overrides: Partial<Region> = {}): Region {
  return {
    id: "harbor",
    name: "Гавань",
    description: "Шумный порт в тумане",
    public: "",
    known_at_start: false,
    ...overrides,
  };
}

export function rumorOf(overrides: Partial<Rumor> = {}): Rumor {
  return {
    text: "В маяке живёт призрак",
    true: false,
    public: "",
    known_at_start: false,
    ...overrides,
  };
}

export function actOf(overrides: Partial<Act> = {}): Act {
  return {
    id: "act-1",
    title: "Прибытие",
    summary: "Отряд приплывает в гавань",
    key_scenes: ["Встреча на пирсе"],
    ...overrides,
  };
}

// --- сущности блока 5 ---

export function exitOf(overrides: Partial<Exit> = {}): Exit {
  return {
    id: "down",
    to: "crypt",
    direction: "вниз",
    description: "Лестница в склеп",
    locked_by: null,
    hidden_dc: null,
    ...overrides,
  };
}

export function featureOf(overrides: Partial<Feature> = {}): Feature {
  return {
    id: "coffin",
    name: "Гроб",
    description: "Каменный гроб с трещиной",
    check: null,
    reveals: [],
    hidden: false,
    locked_by: null,
    ...overrides,
  };
}

export function secretOf(
  overrides: Partial<LocationSecret> = {},
): LocationSecret {
  return {
    id: "crypt-truth",
    text: "В склепе похоронен не тот",
    check: null,
    reveals: [],
    ...overrides,
  };
}

/** Локация с полями по умолчанию, как их отдаёт сервер: одна зона «здесь», без связей зон. */
export function locationOf(overrides: Partial<Location> = {}): Location {
  return {
    public: "",
    known_at_start: false,
    id: "crypt",
    name: "Склеп",
    region: "",
    read_aloud: "Холодный склеп под часовней.",
    dm_notes: "",
    exits: [],
    features: [],
    secrets: [],
    encounter_id: null,
    zones: [{ id: "here", name: "здесь" }],
    links: [],
    ...overrides,
  };
}

export function npcOf(overrides: Partial<Npc> = {}): Npc {
  return {
    public: "",
    known_at_start: false,
    id: "marta",
    name: "Марта",
    role: "смотритель маяка",
    appearance: "седая, в штормовке",
    manner: "говорит отрывисто",
    motive: "сберечь маяк",
    secret: "",
    knows: [],
    attitude: 0,
    relations: [],
    location_id: "lighthouse",
    statblock: null,
    ...overrides,
  };
}

export function relationOf(overrides: Partial<Relation> = {}): Relation {
  return {
    id: "fabian-debtor-marta",
    a: "npc:fabian",
    b: "npc:marta",
    kind: "debtor",
    note: "",
    secret: false,
    mutual: true,
    ...overrides,
  };
}

export function itemOf(overrides: Partial<Item> = {}): Item {
  return {
    public: "",
    known_at_start: false,
    id: "crypt-key",
    name: "Ключ от склепа",
    description: "Тяжёлый железный ключ",
    base_item: null,
    holder: "location:crypt",
    ...overrides,
  };
}

/** Бой черновика: разделы боёв — блок 6, клиенту блока 5 нужен только id. */
export const encounterOf = (id: string) => ({
  id,
  location_id: "crypt",
  trigger: "",
  tactics: "",
  monsters: [],
  pc_zone: "here",
  boss: false,
});

/**
 * Справочник ссылок `GET /api/drafts/{id}/refs`, как его собирает сервер (дополнение B5, §6):
 * виды по порядку deity, faction, npc, location, item, encounter, внутри вида — порядок раздела;
 * имя — `name`, у боя — id, пустое имя — id.
 */
export function refsOf(draft: Draft): DraftRef[] {
  const named = (
    kind: DraftRef["kind"],
    entities: readonly { id: string; name?: string }[],
  ) =>
    entities.map((entity) => ({
      ref: `${kind}:${entity.id}`,
      kind,
      name: entity.name || entity.id,
    }));
  return [
    ...named("deity", draft.deities),
    ...named("faction", draft.factions),
    ...named("npc", draft.npcs),
    ...named("location", draft.locations),
    ...named("item", draft.items),
    ...named(
      "encounter",
      (draft.encounters as { id: string }[]).map(({ id }) => ({ id })),
    ),
  ];
}

// --- редактор на заглушке сервера ---

export type Routes = Record<
  string,
  FakeReply | ((call: RecordedCall) => FakeReply)
>;

/** Вызовы заглушки по методу и пути. */
export const callsTo = (
  server: { calls: RecordedCall[] },
  method: string,
  path: string,
) =>
  server.calls.filter((call) => call.method === method && call.path === path);

/**
 * Открывает редактор черновика создателем игр: `GET /api/drafts/{id}` отдаёт `draft`, справочник
 * ссылок собран из него же, остальные маршруты задаёт тест (он же может подменить и эти два).
 * JSX здесь нет: файл заглушек — не .tsx и в сборку не входит.
 */
export function renderEditor(draft: Draft, routes: Routes = {}) {
  signInAs({ id: "u1", name: "Вика", can_create_games: true });
  const server = fakeServer({
    [`GET /api/drafts/${draft.id}`]: { json: draft },
    [`GET /api/drafts/${draft.id}/refs`]: { json: refsOf(draft) },
    ...routes,
  });
  const onClose = vi.fn();
  render(
    createElement(
      ApiContext,
      { value: createApi(server.fetch, sessionToken) },
      createElement(DraftEditor, { draftId: draft.id, onClose }),
    ),
  );
  return { server, onClose };
}

/** Открывает редактор на шаге `step` («Карта», «Персонажи»…) и отдаёт форму этого шага. */
export async function openStep(
  draft: Draft,
  step: string,
  routes: Routes = {},
) {
  const view = renderEditor(draft, routes);
  fireEvent.click(
    within(
      await screen.findByRole("navigation", { name: "Шаги приключения" }),
    ).getByRole("button", { name: new RegExp(step) }),
  );
  return { ...view, form: screen.getByRole("region", { name: step }) };
}

/** Группа полей сущности по её подписи. */
export const group = (scope: HTMLElement, name: string) =>
  within(scope).getByRole("group", { name });

export const typeIn = (scope: HTMLElement, name: string, value: string) =>
  fireEvent.change(within(scope).getByRole("textbox", { name }), {
    target: { value },
  });

export const choose = (scope: HTMLElement, name: string, value: string) =>
  fireEvent.change(within(scope).getByRole("combobox", { name }), {
    target: { value },
  });

export const press = (scope: HTMLElement, name: string) =>
  fireEvent.click(within(scope).getByRole("button", { name }));

/** Значения вариантов выпадающего списка по порядку. */
export const optionValues = (scope: HTMLElement, name: string) =>
  within(within(scope).getByRole("combobox", { name }))
    .getAllByRole("option")
    .map((option) => (option as HTMLOptionElement).value);
