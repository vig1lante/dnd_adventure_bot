import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ComponentType } from "react";
import { flushSync } from "react-dom";
import { ApiError, errorText } from "../../api/client";
import { useApi } from "../../api/context";
import type {
  AuthoringStep,
  Draft,
  DraftSection,
  Job,
  StepState,
} from "../../api/types";
import { useReducedMotion } from "../../theme/motion";
import { Alert, Button, Card, ErrorState, Skeleton } from "../../ui";
import { ErrorAlert } from "../fields";
import { useAction, useLoaded } from "../hooks";
import { CluesStep } from "./CluesStep";
import { ContentsStep } from "./ContentsStep";
import { DeitiesStep } from "./DeitiesStep";
import { EncountersStep } from "./EncountersStep";
import { FactionsStep } from "./FactionsStep";
import { AlertDialog, ConflictDialog } from "./fields";
import type {
  DraftRefs,
  FocusTarget,
  SectionWrite,
  SrdMonsters,
  StepFormProps,
} from "./fields";
import { sameData } from "./fieldValues";
import { focusedElement } from "./focus";
import { GeneratePanel } from "./GeneratePanel";
import { ItemsStep } from "./ItemsStep";
import { isActive } from "./jobs";
import { JobStatus } from "./JobStatus";
import { MapStep } from "./MapStep";
import { NpcsStep } from "./NpcsStep";
import { PlotStep } from "./PlotStep";
import { formProblem } from "./problems";
import { PublishStep } from "./PublishStep";
import { RelationsStep } from "./RelationsStep";
import { ReviewStep } from "./ReviewStep";
import { SettingsStep } from "./SettingsStep";
import { StepNav } from "./StepNav";
import { EDITOR_STEPS } from "./steps";
import type { EditorScreen, EditorStep } from "./steps";
import { WorldStep } from "./WorldStep";
import "./authoring.css";

/** Как часто спрашивать сервер о задании. */
export const POLL_MS = 2000;

interface StepForm {
  /**
   * Разделы, которые пишет экран; первый — тот, что пишет «Сохранить раздел» (`onSave`). Экраны
   * с общим разделом предупреждают друг о друге: запись одного начинает другой заново.
   */
  sections: readonly DraftSection[];
  /**
   * Разделы, которые пишет генерация шага экрана; по умолчанию — те же `sections`. Генерация
   * Замысла пишет и откровения «Улик» (`_merge_concept`), генерация «Наполнения» —
   * только локации.
   */
  generates?: readonly DraftSection[];
  /** Подсказка у «Сгенерировать шаг»: что генерация заменит на другом экране. */
  generateNote?: string;
  /**
   * У формы есть состояние вида над правкой (выбор локации, фильтр): она остаётся, а заново по
   * `contentKey` начинается только правка внутри. Остальные формы начинаются заново целиком.
   */
  ownKey?: true;
  Form: ComponentType<StepFormProps>;
  /** В форме есть выбор ссылок: на этом экране нужен справочник сервера. */
  refs?: true;
  /** В форме есть выбор монстров: на этом экране нужен справочник SRD. */
  srd?: true;
}

// Экран (шаг генерации) → разделы черновика.
const STEP_FORMS: Record<EditorStep, StepForm> = {
  concept: {
    sections: ["settings"],
    generates: ["settings", "revelations"],
    generateNote:
      "Генерация Замысла заменит и незакреплённые откровения с уликами на шаге «Улики»: закрепите нужные.",
    Form: SettingsStep,
  },
  world: { sections: ["world"], Form: WorldStep },
  pantheon: { sections: ["deities"], Form: DeitiesStep },
  factions: { sections: ["factions"], Form: FactionsStep },
  plot: { sections: ["plot"], Form: PlotStep },
  locations: { sections: ["locations"], Form: MapStep },
  // «Наполнение» пишет и `locations`, и `items` (предметы локаций) — своими записями. Эта форма
  // и «Связи» начинаются заново сами: выбор локации и фильтр переживают запись.
  location_contents: {
    sections: ["locations", "items"],
    generates: ["locations"],
    ownKey: true,
    Form: ContentsStep,
    refs: true,
  },
  npcs: { sections: ["npcs"], Form: NpcsStep, refs: true },
  relations: {
    sections: ["relations"],
    ownKey: true,
    Form: RelationsStep,
    refs: true,
  },
  // «Бои и боссы» пишут свои статблоки и бои — двумя записями, статблоки первыми.
  encounters: {
    sections: ["encounters", "monsters"],
    Form: EncountersStep,
    refs: true,
    srd: true,
  },
  items: { sections: ["items"], Form: ItemsStep, refs: true },
  clues: { sections: ["revelations"], Form: CluesStep, refs: true },
};

// Данные раздела в черновике: то, что пишет `PUT .../sections/{section}`.
const SECTION_CONTENT: Record<DraftSection, (draft: Draft) => unknown> = {
  settings: (draft) => [
    draft.title,
    draft.journal_intro,
    draft.adventure_id,
    draft.settings,
  ],
  world: (draft) => draft.world,
  deities: (draft) => draft.deities,
  factions: (draft) => draft.factions,
  plot: (draft) => draft.plot,
  locations: (draft) => [
    draft.locations,
    draft.start_location,
    draft.finale_location,
  ],
  npcs: (draft) => draft.npcs,
  relations: (draft) => draft.relations,
  items: (draft) => draft.items,
  encounters: (draft) => draft.encounters,
  monsters: (draft) => draft.monsters,
  revelations: (draft) => draft.revelations,
};

/** Данные разделов экрана: форма начинается заново, только когда они поменялись. */
const contentOf = (draft: Draft, screen: EditorStep) =>
  STEP_FORMS[screen].sections.map((section) => SECTION_CONTENT[section](draft));

/** Черновик с записываемыми данными разделов: по нему отказ называет сущности именами. */
function withWrites(draft: Draft, writes: readonly SectionWrite[]): unknown {
  return writes.reduce<Record<string, unknown>>(
    (merged, { section, data }) =>
      // Раздел «Карты» — сами поля черновика: локации, старт и финал.
      section === "locations" && data !== null && typeof data === "object"
        ? { ...merged, ...data }
        : { ...merged, [section]: data },
    { ...draft },
  );
}

/** Менялся ли черновик после `moment`: позже ли запись какого-нибудь шага. */
const changedAfter = (
  steps: Record<string, StepState>,
  moment: string,
): boolean =>
  Object.values(steps).some(
    ({ updated_at }) =>
      updated_at !== null && Date.parse(updated_at) > Date.parse(moment),
  );

/** Форма экрана; у проверки и публикации её нет. */
const formOf = (screen: EditorScreen): StepForm | undefined =>
  (STEP_FORMS as Partial<Record<EditorScreen, StepForm>>)[screen];

/** Начало текста о несохранённых правках: «На экране «Улики»» или «На экранах «Замысел», «Боги»». */
const onScreens = (labels: readonly string[]) =>
  `${labels.length === 1 ? "На экране" : "На экранах"} ${labels
    .map((label) => `«${label}»`)
    .join(", ")}`;

// Подписи разделов для текста о частично сохранённых правках.
const SECTION_LABELS: Record<DraftSection, string> = {
  settings: "замысел",
  world: "мир",
  deities: "боги",
  factions: "фракции",
  plot: "сюжет",
  locations: "локации",
  npcs: "персонажи",
  relations: "связи",
  items: "предметы",
  encounters: "бои",
  monsters: "свои монстры",
  revelations: "откровения и улики",
};

/**
 * Справочник ссылок черновика, пока он нужен экрану (`enabled`): грузится при входе на экран и
 * после каждой записи (новая `version`), чтобы в выборе были и только что сохранённые сущности.
 * Ответ на устаревший запрос отбрасывается; при сбое остаётся прежний список и текст ошибки.
 */
function useDraftRefs(
  draftId: string,
  version: number,
  enabled: boolean,
): DraftRefs {
  const api = useApi();
  const [refs, setRefs] = useState<DraftRefs>({ list: [], error: null });
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    api.listRefs(draftId).then(
      (list) => {
        if (alive) setRefs({ list, error: null });
      },
      (failure: unknown) => {
        if (alive)
          setRefs((current) => ({ ...current, error: errorText(failure) }));
      },
    );
    return () => {
      alive = false;
    };
  }, [api, draftId, version, enabled]);
  return refs;
}

/**
 * Справочник монстров SRD: грузится, когда экран впервые его просит (`enabled`), и дальше не
 * меняется, поэтому ответ не отбрасывается и после ухода с экрана. После сбоя — текст ошибки;
 * следующий вход на экран пробует снова.
 */
function useSrdMonsters(enabled: boolean): SrdMonsters {
  const api = useApi();
  const [srd, setSrd] = useState<SrdMonsters>({ list: [], error: null });
  const requested = useRef(false);
  useEffect(() => {
    if (!enabled || requested.current) return;
    requested.current = true;
    api.listSrdMonsters().then(
      (list) => setSrd({ list, error: null }),
      (failure: unknown) => {
        requested.current = false;
        setSrd((current) => ({ ...current, error: errorText(failure) }));
      },
    );
  }, [api, enabled]);
  return srd;
}

const isVersionConflict = (failure: unknown) =>
  failure instanceof ApiError && failure.code === "version_conflict";

/**
 * Запись экрана оборвалась не на первом разделе: часть уже на сервере. Форма не начинается
 * заново — держит прежний ключ (`key`) и свои правки, пока данные её разделов на сервере не
 * поменяются ещё раз (`saved` — черновик после записанной части).
 */
interface HeldForm {
  key: string;
  saved: Draft;
}

interface DeleteDraftProps {
  draftId: string;
  /** Правка закрыта (идёт задание или запись): удалять тоже нельзя, сервер ответил бы 409. */
  disabled: boolean;
  onDeleted: () => void;
}

/**
 * «Удалить черновик» с вопросом-подтверждением, как у публикации: первое нажатие спрашивает,
 * второе удаляет. Опубликованное из черновика приключение остаётся — у него своя запись.
 */
function DeleteDraft({ draftId, disabled, onDeleted }: DeleteDraftProps) {
  const api = useApi();
  const { busy, error, run } = useAction();
  // Открытый вопрос и кнопка, с которой он задан: на неё вернётся фокус после ответа.
  const [confirming, setConfirming] = useState<{ from: HTMLElement } | null>(
    null,
  );

  const remove = async () => {
    setConfirming(null);
    if (await run(() => api.deleteDraft(draftId))) onDeleted();
  };

  return (
    <div className="screen-stack">
      <div>
        <Button
          variant="secondary"
          busy={busy}
          disabled={disabled}
          onClick={(event) => setConfirming({ from: event.currentTarget })}
        >
          Удалить черновик
        </Button>
      </div>
      {confirming !== null && (
        <AlertDialog
          title="Удалить черновик?"
          returnFocus={confirming.from}
          actions={
            <>
              <Button
                variant="danger"
                disabled={disabled}
                onClick={() => void remove()}
              >
                Да, удалить
              </Button>
              <Button variant="secondary" onClick={() => setConfirming(null)}>
                Отмена
              </Button>
            </>
          }
        >
          <p className="screen-muted">
            Черновик пропадёт насовсем. Опубликованное приключение останется в
            каталоге и играх.
          </p>
        </AlertDialog>
      )}
      {error !== null && <Alert>{error}</Alert>}
    </div>
  );
}

interface EditorProps {
  initial: Draft;
  step: EditorScreen;
  onStep: (step: EditorScreen) => void;
  /** Перечитать черновик с сервера (конфликт версии, сорванный опрос). */
  onReload: () => void;
  /** Черновик удалён: редактор закрывается к списку. */
  onDeleted: () => void;
}

function Editor({ initial, step, onStep, onReload, onDeleted }: EditorProps) {
  const api = useApi();
  const reducedMotion = useReducedMotion();
  const [draft, setDraft] = useState(initial);
  const [job, setJob] = useState(initial.job);
  // Задание, которое упало ещё до открытия черновика, — не ошибка этого входа:
  // спокойная заметка, а если черновик с тех пор менялся — ничего.
  const [failedBefore] = useState(
    initial.job?.status === "failed" ? initial.job.id : null,
  );
  const pastFailure = job !== null && job.id === failedBefore;
  const outdatedFailure =
    pastFailure && changedAfter(draft.steps, job.finished_at ?? job.created_at);
  // Формы, чья запись оборвалась на втором разделе: правки остаются в форме.
  const [held, setHeld] = useState<Partial<Record<EditorStep, HeldForm>>>({});
  /**
   * Ключ формы экрана — данные её разделов: форма начинается заново, когда они поменялись. После
   * частичной записи — прежний ключ, пока сервер не поменял эти данные ещё раз.
   */
  const formKey = (screen: EditorStep): string => {
    const hold = held[screen];
    return hold !== undefined &&
      sameData(contentOf(hold.saved, screen), contentOf(draft, screen))
      ? hold.key
      : JSON.stringify(contentOf(draft, screen));
  };
  // Данные последней записи разделов: по ним отказ сервера называет сущности именами.
  const [written, setWritten] = useState<unknown>(null);
  // Куда ведёт ссылка находки «Проверки»: экран шага и сущность на нём.
  const [focus, setFocus] = useState<
    (FocusTarget & { screen: EditorScreen }) | null
  >(null);
  const panels = useRef<Partial<Record<EditorScreen, HTMLElement | null>>>({});
  const [pollError, setPollError] = useState<string | null>(null);
  // Вопрос о конфликте версий и элемент, с которого начата отвергнутая запись (`null` — фокуса
  // не было): к появлению вопроса он уже без фокуса — форма закрыта на время записи.
  const [conflict, setConflict] = useState<{ from: HTMLElement | null } | null>(
    null,
  );
  // Несколько записей подряд оборвались не на первой: часть правок уже на сервере.
  const [partial, setPartial] = useState<string | null>(null);
  // Есть ли несохранённые правки на экранах. Экраны с общим разделом («Карта» и «Наполнение»,
  // «Наполнение» и «Предметы», «Замысел» и «Улики» через генерацию) предупреждают друг о друге:
  // запись или генерация одного начинает другой заново.
  const [unsaved, setUnsaved] = useState<Partial<Record<EditorStep, boolean>>>(
    {},
  );
  // Свой постоянный обработчик на каждый экран: форма сообщает о правках из эффекта.
  const markUnsaved = useMemo(
    () =>
      Object.fromEntries(
        EDITOR_STEPS.map(({ step: screen }) => [
          screen,
          (dirty: boolean) =>
            setUnsaved((current) =>
              current[screen] === dirty
                ? current
                : { ...current, [screen]: dirty },
            ),
        ]),
      ) as Record<EditorStep, (dirty: boolean) => void>,
    [],
  );
  /** Предупреждение о несохранённых правках других экранов, чьи разделы задевает запись `written`. */
  const unsavedIn = (screen: EditorStep, written: readonly DraftSection[]) => {
    const others = EDITOR_STEPS.filter(
      ({ step: other }) =>
        other !== screen &&
        unsaved[other] === true &&
        STEP_FORMS[other].sections.some((section) => written.includes(section)),
    ).map(({ label }) => label);
    return others.length === 0
      ? null
      : `${onScreens(others)} есть несохранённые правки: они пропадут.`;
  };
  // Проверка и публикация видят только сохранённый черновик.
  const unsavedScreens = EDITOR_STEPS.filter(
    ({ step: screen }) => unsaved[screen] === true,
  ).map(({ label }) => label);
  const unsavedNote =
    unsavedScreens.length === 0 ? null : (
      <p className="authoring-warning">
        {onScreens(unsavedScreens)} есть несохранённые правки: проверка и
        публикация их не видят.
      </p>
    );
  const refs = useDraftRefs(
    draft.id,
    draft.version,
    formOf(step)?.refs === true,
  );
  const srd = useSrdMonsters(formOf(step)?.srd === true);
  // Набранные, но ещё не отправленные пожелания по шагам. Они живут здесь, а не в форме шага:
  // форма пересоздаётся при каждой смене раздела (например, после «Сохранить раздел»).
  const [typedWishes, setTypedWishes] = useState<
    Partial<Record<AuthoringStep, string>>
  >({});
  const wishOf = (wishStep: AuthoringStep) =>
    typedWishes[wishStep] ?? draft.wishes[wishStep] ?? "";
  const { busy, error, problems, run } = useAction();
  const activeJobId = job !== null && isActive(job) ? job.id : null;

  // Опрос задания, пока оно идёт: следующий запрос — через 2 секунды после ответа на предыдущий,
  // так на медленной связи запросы не накладываются. На done сначала берётся свежий черновик (в
  // нём результат генерации), и только потом задание считается законченным: правка открывается
  // уже на новых данных. Уход с экрана или новое задание обрывают цепочку.
  useEffect(() => {
    if (activeJobId === null) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = await api.getJob(draft.id, activeJobId);
        if (!alive) return;
        if (next.status === "queued" || next.status === "running") {
          timer = setTimeout(() => void poll(), POLL_MS);
          return;
        }
        if (next.status === "done") {
          const fresh = await api.getDraft(draft.id);
          if (!alive) return;
          setDraft(fresh);
        }
        setJob(next);
      } catch (failure) {
        if (alive) setPollError(errorText(failure));
      }
    };
    timer = setTimeout(() => void poll(), POLL_MS);
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [api, draft.id, activeJobId]);

  // Запись в черновик идёт с его версией; чужая версия — диалог, а не текст ошибки. Открытый
  // вопрос закрывается на время записи: новый отказ показывает его заново, с фокусом и прокруткой.
  // Элемент в фокусе запоминается до того, как форма закроется на время записи.
  const write = (action: () => Promise<void>) =>
    run(async () => {
      const from = focusedElement();
      setConflict(null);
      setPartial(null);
      setWritten(null);
      try {
        await action();
      } catch (failure) {
        if (!isVersionConflict(failure)) throw failure;
        setConflict({ from });
      }
    });

  // Разделы экрана пишутся по очереди: каждая запись — с версией, которую вернула предыдущая.
  const save = (screen: EditorStep, writes: SectionWrite[]) =>
    write(async () => {
      setWritten(withWrites(draft, writes));
      const key = formKey(screen);
      let version = draft.version;
      let saved: Draft | null = null;
      for (const [index, { section, data }] of writes.entries()) {
        try {
          saved = await api.saveSection(draft.id, version, section, data);
          version = saved.version;
          setDraft(saved);
        } catch (failure) {
          // Часть уже записана — автор узнаёт об этом и при конфликте версии (рядом с диалогом),
          // а форма не начинается заново: несохранённая часть правок остаётся в ней.
          if (index > 0 && saved !== null) {
            const kept = saved;
            setHeld((current) => ({
              ...current,
              [screen]: { key, saved: kept },
            }));
            const done = writes
              .slice(0, index)
              .map((w) => SECTION_LABELS[w.section]);
            setPartial(
              `Сохранено: ${done.join(", ")}. Не сохранено: ${SECTION_LABELS[section]} — правки остались в форме: исправьте и сохраните раздел снова или обновите черновик.`,
            );
          }
          throw failure;
        }
      }
    });

  const lock = (ref: string, locked: boolean) =>
    write(async () => {
      const locks = locked
        ? [...draft.locks, ref]
        : draft.locks.filter((item) => item !== ref);
      setDraft(await api.setLocks(draft.id, draft.version, locks));
    });

  // Без цели — шаг целиком; с целью (`<вид>:<id>`) — одна сущность шага.
  const generate = (jobStep: AuthoringStep, target?: string) =>
    write(async () => {
      // Пожелание живёт в черновике: генерация берёт его оттуда, поэтому новое сохраняется первым.
      const wish = wishOf(jobStep);
      if (wish.trim() !== (draft.wishes[jobStep] ?? "")) {
        setDraft(await api.setWish(draft.id, draft.version, jobStep, wish));
      }
      // Дальше поле показывает пожелание, как его знает сервер.
      setTypedWishes((typed) => ({ ...typed, [jobStep]: undefined }));
      setJob(
        await api.generate(
          draft.id,
          target === undefined ? { step: jobStep } : { step: jobStep, target },
        ),
      );
    });

  /**
   * Переход по ссылке находки: экран её шага, затем прокрутка и фокус к сущности (`ref` —
   * `location:crypt/exit:down`). Экран сначала раскрывает её (выбор локации, фильтр связей),
   * поэтому переход отрисовывается сразу. Сущности нет — к началу экрана шага.
   */
  const go = (screen: EditorScreen, ref?: string) => {
    if (ref === undefined) {
      onStep(screen);
      return;
    }
    flushSync(() => {
      onStep(screen);
      setFocus({ screen, ref });
    });
    const panel = panels.current[screen];
    if (panel === null || panel === undefined) return;
    const parts = ref.split("/");
    const entities = [...panel.querySelectorAll<HTMLElement>("[data-ref]")];
    const entity = parts
      .map((_, index) => parts.slice(0, parts.length - index).join("/"))
      .map((prefix) => entities.find((item) => item.dataset.ref === prefix))
      .find((item) => item !== undefined);
    const target =
      entity ?? panel.querySelector<HTMLElement>(":scope > section");
    if (target !== null) reveal(target);
  };

  /** Фокус и прокрутка к цели перехода. */
  const reveal = (target: HTMLElement) => {
    // Группа полей и раздел экрана фокусируются только ради перехода: с уходом фокуса — как были.
    if (target.tabIndex < 0) {
      target.setAttribute("tabindex", "-1");
      target.addEventListener(
        "blur",
        () => target.removeAttribute("tabindex"),
        { once: true },
      );
    }
    target.focus({ preventScroll: true });
    target.scrollIntoView?.({
      block: "start",
      behavior: reducedMotion ? "auto" : "smooth",
    });
  };

  /**
   * Переход к шагу кнопкой («Перейти к шагу «Замысел»» на «Публикации»): нажатая кнопка
   * исчезает вместе с экраном, поэтому фокус уходит на поле, о котором речь (`field` — его
   * подпись, например «id приключения»), а без него — на раздел шага, а не на страницу.
   */
  const goToStep = (screen: EditorScreen, field?: string) => {
    flushSync(() => onStep(screen));
    const panel = panels.current[screen];
    if (panel === null || panel === undefined) return;
    const label = [...panel.querySelectorAll("label")].find(
      (item) => item.textContent === field,
    );
    const target =
      label?.control ?? panel.querySelector<HTMLElement>(":scope > section");
    if (target instanceof HTMLElement) reveal(target);
  };

  const retry = (failed: Job) =>
    write(async () => {
      setJob(
        await api.generate(
          draft.id,
          failed.target === null
            ? { step: failed.step }
            : { step: failed.step, target: failed.target },
        ),
      );
    });

  return (
    <div className="screen-stack">
      <DeleteDraft
        draftId={draft.id}
        disabled={activeJobId !== null || busy}
        onDeleted={onDeleted}
      />
      {conflict !== null && (
        <ConflictDialog
          returnFocus={conflict.from}
          onRefresh={onReload}
          onCancel={() => setConflict(null)}
        />
      )}
      <StepNav draft={draft} current={step} onSelect={onStep} />
      <JobStatus
        job={outdatedFailure ? null : job}
        past={pastFailure}
        pollError={pollError}
        busy={busy}
        onRetry={(failed) => void retry(failed)}
        onRefresh={onReload}
      />
      {/* Пока идёт задание или запрос, правка закрыта целиком: результат генерации не должен
          встретиться с ручной записью, а две записи подряд — с одной версией. Все шаги на
          месте (скрыты): несохранённые правки переживают переход между шагами. */}
      <fieldset
        className="authoring-editor"
        disabled={activeJobId !== null || busy}
      >
        {EDITOR_STEPS.map(({ step: item, label }) => {
          const { sections, generates, generateNote, ownKey, Form } =
            STEP_FORMS[item];
          const contentKey = formKey(item);
          // Запись раздела и генерация шага задевают разные экраны: генерация Замысла — и «Улики».
          const savingElsewhere = unsavedIn(item, sections);
          const generatingElsewhere = unsavedIn(item, generates ?? sections);
          return (
            <div
              key={item}
              hidden={item !== step}
              ref={(element) => {
                panels.current[item] = element;
              }}
            >
              <Card title={label} headingLevel={3}>
                <div className="screen-stack">
                  <Form
                    key={ownKey ? "own" : contentKey}
                    contentKey={contentKey}
                    draft={draft}
                    refs={refs}
                    srd={srd}
                    focus={focus?.screen === item ? focus : null}
                    onSave={(data) =>
                      void save(item, [{ section: sections[0], data }])
                    }
                    onSaveSections={(writes) => void save(item, writes)}
                    onLock={(ref, locked) => void lock(ref, locked)}
                    onDirtyChange={markUnsaved[item]}
                    unsavedElsewhere={savingElsewhere}
                    generate={(dirty) => (
                      <GeneratePanel
                        wish={wishOf(item)}
                        onWishChange={(wish) =>
                          setTypedWishes((typed) => ({
                            ...typed,
                            [item]: wish,
                          }))
                        }
                        locked={draft.locks.includes(`step:${item}`)}
                        dirty={dirty}
                        warning={generatingElsewhere}
                        note={generateNote}
                        onGenerate={() => void generate(item)}
                      />
                    )}
                    regenerate={(target, name, dirty) => (
                      <>
                        <Button
                          variant="secondary"
                          icon="d20"
                          aria-label={`Перегенерировать: ${name}`}
                          // После генерации форма начнётся заново с черновиком сервера, поэтому
                          // при несохранённых правках кнопка закрыта (как «Закрепить»).
                          // Закреплённое генерация не меняет: сервер ответил бы 422 step_locked.
                          title={dirty ? "Сначала сохраните раздел" : undefined}
                          disabled={
                            dirty ||
                            draft.locks.includes(target) ||
                            draft.locks.includes(`step:${item}`)
                          }
                          onClick={() => void generate(item, target)}
                        >
                          Перегенерировать
                        </Button>
                        {generatingElsewhere !== null && (
                          <p className="authoring-warning">
                            {generatingElsewhere}
                          </p>
                        )}
                      </>
                    )}
                  />
                </div>
              </Card>
            </div>
          );
        })}
        {/* Проверка и публикация показываются, только пока открыты: так каждый вход на экран
            берёт свежий отчёт проверки по текущему черновику. */}
        {step === "review" && (
          // Обёртка — панель шага, как у форм: к ней ведёт переход кнопкой с «Публикации».
          <div
            ref={(element) => {
              panels.current.review = element;
            }}
          >
            <Card title="Проверка" headingLevel={3}>
              <div className="screen-stack">
                {unsavedNote}
                <ReviewStep draft={draft} onGo={go} />
              </div>
            </Card>
          </div>
        )}
        {step === "publish" && (
          <Card title="Публикация" headingLevel={3}>
            <div className="screen-stack">
              {unsavedNote}
              <PublishStep draft={draft} onGo={goToStep} onReload={onReload} />
            </div>
          </Card>
        )}
      </fieldset>
      <ErrorAlert
        error={error}
        problems={problems.map((line) => formProblem(line, written ?? draft))}
      />
      {partial !== null && (
        <div className="screen-stack">
          <p className="authoring-warning">{partial}</p>
          <div>
            <Button variant="secondary" onClick={onReload}>
              Обновить
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

interface DraftEditorProps {
  draftId: string;
  onClose: () => void;
}

/** Редактор черновика: шаги, формы разделов, генерация с опросом задания, закрепления, проверка и публикация. */
export function DraftEditor({ draftId, onClose }: DraftEditorProps) {
  const api = useApi();
  const load = useCallback(() => api.getDraft(draftId), [api, draftId]);
  const [loaded, reload] = useLoaded(load);
  // Шаг живёт выше редактора: после «Обновить» автор остаётся на том же шаге.
  const [step, setStep] = useState<EditorScreen>("concept");

  return (
    <Card title="Редактор приключения" icon="scroll" className="authoring-card">
      <div className="screen-stack">
        <div>
          <Button variant="secondary" onClick={onClose}>
            К списку приключений
          </Button>
        </div>
        {loaded.status === "loading" && (
          <Skeleton label="Загружаем черновик…" />
        )}
        {loaded.status === "error" && (
          <ErrorState title="Не удалось открыть черновик" onRetry={reload}>
            <p>{loaded.message}</p>
            <p>Черновик на сервере цел: попробуйте ещё раз.</p>
          </ErrorState>
        )}
        {loaded.status === "ready" && (
          <Editor
            initial={loaded.data}
            step={step}
            onStep={setStep}
            onReload={reload}
            onDeleted={onClose}
          />
        )}
      </div>
    </Card>
  );
}
