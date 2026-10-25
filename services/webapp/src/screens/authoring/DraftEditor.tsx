import { useCallback, useEffect, useId, useMemo, useState } from "react";
import type { ComponentType } from "react";
import { ApiError, errorText } from "../../api/client";
import { useApi } from "../../api/context";
import type { AuthoringStep, Draft, DraftSection, Job } from "../../api/types";
import { Alert, Button, Card, Loading } from "../../ui";
import { ErrorAlert } from "../fields";
import { useAction, useLoaded } from "../hooks";
import { ContentsStep } from "./ContentsStep";
import { DeitiesStep } from "./DeitiesStep";
import { FactionsStep } from "./FactionsStep";
import type { DraftRefs, SectionWrite, StepFormProps } from "./fields";
import { GeneratePanel } from "./GeneratePanel";
import { JobStatus, isActive } from "./JobStatus";
import { MapStep } from "./MapStep";
import { NpcsStep } from "./NpcsStep";
import { PlotStep } from "./PlotStep";
import { RelationsStep } from "./RelationsStep";
import { SettingsStep } from "./SettingsStep";
import { EDITOR_STEPS, StepNav } from "./StepNav";
import type { EditorStep } from "./StepNav";
import { WorldStep } from "./WorldStep";
import "./authoring.css";

/** Как часто спрашивать сервер о задании (контракт B4, §5). */
export const POLL_MS = 2000;

interface StepForm {
  /** Раздел, который пишет «Сохранить раздел» шага. */
  section: DraftSection;
  /**
   * Содержимое черновика, которое показывает форма: она начинается заново, только когда оно
   * поменялось. `null` — форма начинается заново сама (у неё есть состояние вида над правкой).
   */
  content: (draft: Draft) => unknown;
  Form: ComponentType<StepFormProps>;
  /** В форме есть выбор ссылок: на этом экране нужен справочник сервера. */
  refs?: true;
}

// Экран (шаг генерации) → раздел черновика (дополнения B4 §2 и B5 §2).
const STEP_FORMS: Record<EditorStep, StepForm> = {
  concept: {
    section: "settings",
    content: (draft) => [draft.title, draft.journal_intro, draft.settings],
    Form: SettingsStep,
  },
  world: { section: "world", content: (draft) => draft.world, Form: WorldStep },
  pantheon: {
    section: "deities",
    content: (draft) => draft.deities,
    Form: DeitiesStep,
  },
  factions: {
    section: "factions",
    content: (draft) => draft.factions,
    Form: FactionsStep,
  },
  plot: { section: "plot", content: (draft) => draft.plot, Form: PlotStep },
  locations: {
    section: "locations",
    content: (draft) => [
      draft.locations,
      draft.start_location,
      draft.finale_location,
    ],
    Form: MapStep,
  },
  // «Наполнение» пишет и `locations`, и `items` (предметы локаций) — своими записями. Эта форма
  // и «Связи» начинаются заново сами: выбор локации и фильтр переживают запись.
  location_contents: {
    section: "locations",
    content: () => null,
    Form: ContentsStep,
    refs: true,
  },
  npcs: {
    section: "npcs",
    content: (draft) => draft.npcs,
    Form: NpcsStep,
    refs: true,
  },
  relations: {
    section: "relations",
    content: () => null,
    Form: RelationsStep,
    refs: true,
  },
};

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

const isVersionConflict = (failure: unknown) =>
  failure instanceof ApiError && failure.code === "version_conflict";

function ConflictDialog({
  onRefresh,
  onCancel,
}: {
  onRefresh: () => void;
  onCancel: () => void;
}) {
  const titleId = useId();
  return (
    <div
      role="alertdialog"
      aria-labelledby={titleId}
      className="authoring-dialog"
    >
      <p id={titleId} className="authoring-dialog__title">
        Черновик изменился, обновить?
      </p>
      <p className="screen-muted">
        Его успели поменять в другой вкладке или генерацией. Обновление заменит
        несохранённые правки.
      </p>
      <div className="screen-actions">
        <Button onClick={onRefresh}>Обновить</Button>
        <Button variant="secondary" onClick={onCancel}>
          Отмена
        </Button>
      </div>
    </div>
  );
}

interface EditorProps {
  initial: Draft;
  step: EditorStep;
  onStep: (step: EditorStep) => void;
  /** Перечитать черновик с сервера (конфликт версии, сорванный опрос). */
  onReload: () => void;
}

function Editor({ initial, step, onStep, onReload }: EditorProps) {
  const api = useApi();
  const [draft, setDraft] = useState(initial);
  const [job, setJob] = useState(initial.job);
  const [pollError, setPollError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  // Несколько записей подряд оборвались не на первой: часть правок уже на сервере.
  const [partial, setPartial] = useState<string | null>(null);
  // Есть ли несохранённые правки на экранах. Экраны одного раздела («Карта» и «Наполнение»)
  // предупреждают друг о друге: запись или генерация одного начинает другой заново.
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
  const unsavedElsewhere = (screen: EditorStep) => {
    const others = EDITOR_STEPS.filter(
      ({ step: other }) =>
        other !== screen &&
        unsaved[other] === true &&
        STEP_FORMS[other].section === STEP_FORMS[screen].section,
    ).map(({ label }) => `«${label}»`);
    return others.length === 0
      ? null
      : `На экране ${others.join(", ")} есть несохранённые правки: они пропадут.`;
  };
  const refs = useDraftRefs(
    draft.id,
    draft.version,
    STEP_FORMS[step].refs === true,
  );
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

  // Запись в черновик идёт с его версией; чужая версия — диалог, а не текст ошибки.
  const write = (action: () => Promise<void>) =>
    run(async () => {
      setPartial(null);
      try {
        await action();
      } catch (failure) {
        if (!isVersionConflict(failure)) throw failure;
        setConflict(true);
      }
    });

  // Разделы пишутся по очереди: каждая запись — с версией, которую вернула предыдущая.
  const save = (writes: SectionWrite[]) =>
    write(async () => {
      let version = draft.version;
      for (const [index, { section, data }] of writes.entries()) {
        try {
          const saved = await api.saveSection(draft.id, version, section, data);
          version = saved.version;
          setDraft(saved);
        } catch (failure) {
          // Часть уже записана — автор узнаёт об этом и при конфликте версии (рядом с диалогом).
          if (index > 0) {
            const done = writes
              .slice(0, index)
              .map((w) => SECTION_LABELS[w.section]);
            setPartial(
              `Сохранено: ${done.join(", ")}. Не сохранено: ${SECTION_LABELS[section]} — обновите черновик и повторите правку.`,
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
      {conflict && (
        <ConflictDialog
          onRefresh={onReload}
          onCancel={() => setConflict(false)}
        />
      )}
      <StepNav draft={draft} current={step} onSelect={onStep} />
      <JobStatus
        job={job}
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
          const { section, content, Form } = STEP_FORMS[item];
          const elsewhere = unsavedElsewhere(item);
          return (
            <div key={item} hidden={item !== step}>
              <Card title={label} headingLevel={3}>
                <div className="screen-stack">
                  <Form
                    key={JSON.stringify(content(draft))}
                    draft={draft}
                    refs={refs}
                    onSave={(data) => void save([{ section, data }])}
                    onSaveSections={(writes) => void save(writes)}
                    onLock={(ref, locked) => void lock(ref, locked)}
                    onDirtyChange={markUnsaved[item]}
                    unsavedElsewhere={elsewhere}
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
                        warning={elsewhere}
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
                          // при несохранённых правках кнопка закрыта (как «Закрепить», B4 §10).
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
                        {elsewhere !== null && (
                          <p className="authoring-warning">{elsewhere}</p>
                        )}
                      </>
                    )}
                  />
                </div>
              </Card>
            </div>
          );
        })}
      </fieldset>
      <ErrorAlert error={error} problems={problems} />
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

/** Редактор черновика: шаги, формы разделов, генерация с опросом задания и закрепления. */
export function DraftEditor({ draftId, onClose }: DraftEditorProps) {
  const api = useApi();
  const load = useCallback(() => api.getDraft(draftId), [api, draftId]);
  const [loaded, reload] = useLoaded(load);
  // Шаг живёт выше редактора: после «Обновить» автор остаётся на том же шаге.
  const [step, setStep] = useState<EditorStep>("concept");

  return (
    <Card title="Редактор приключения" icon="scroll">
      <div className="screen-stack">
        <div>
          <Button variant="secondary" onClick={onClose}>
            К списку приключений
          </Button>
        </div>
        {loaded.status === "loading" && <Loading label="Загружаем черновик…" />}
        {loaded.status === "error" && (
          <div className="screen-stack">
            <Alert>{loaded.message}</Alert>
            <div>
              <Button variant="secondary" onClick={reload}>
                Повторить
              </Button>
            </div>
          </div>
        )}
        {loaded.status === "ready" && (
          <Editor
            initial={loaded.data}
            step={step}
            onStep={setStep}
            onReload={reload}
          />
        )}
      </div>
    </Card>
  );
}
