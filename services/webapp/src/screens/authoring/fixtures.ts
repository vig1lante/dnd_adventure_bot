// Данные ТОЛЬКО для тестов конструктора. Формы — строго по контракту блока 4 (спека §10.3 и
// дополнение dispatcher): живого сервера у тестов нет, заглушки отвечают как он.
import { render } from "@testing-library/react";
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
  DraftSummary,
  Faction,
  Job,
  Region,
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
 * Открывает редактор черновика создателем игр: `GET /api/drafts/{id}` отдаёт `draft`, остальные
 * маршруты задаёт тест. JSX здесь нет: файл заглушек — не .tsx и в сборку не входит.
 */
export function renderEditor(draft: Draft, routes: Routes = {}) {
  signInAs({ id: "u1", name: "Вика", can_create_games: true });
  const server = fakeServer({
    [`GET /api/drafts/${draft.id}`]: { json: draft },
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
