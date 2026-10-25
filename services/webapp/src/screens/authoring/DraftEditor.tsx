import { useCallback, useEffect, useId, useState } from "react";
import type { ComponentType } from "react";
import { ApiError, errorText } from "../../api/client";
import { useApi } from "../../api/context";
import type { AuthoringStep, Draft, DraftSection, Job } from "../../api/types";
import { Alert, Button, Card, Loading } from "../../ui";
import { ErrorAlert } from "../fields";
import { useAction, useLoaded } from "../hooks";
import { DeitiesStep } from "./DeitiesStep";
import { FactionsStep } from "./FactionsStep";
import type { StepFormProps } from "./fields";
import { GeneratePanel } from "./GeneratePanel";
import { JobStatus, isActive } from "./JobStatus";
import { PlotStep } from "./PlotStep";
import { SettingsStep } from "./SettingsStep";
import { EDITOR_STEPS, StepNav } from "./StepNav";
import type { EditorStep } from "./StepNav";
import { WorldStep } from "./WorldStep";
import "./authoring.css";

/** Как часто спрашивать сервер о задании (контракт B4, §5). */
export const POLL_MS = 2000;

interface StepForm {
  section: DraftSection;
  /** Содержимое раздела в черновике: форма начинается заново, только когда оно поменялось. */
  content: (draft: Draft) => unknown;
  Form: ComponentType<StepFormProps>;
}

// Шаг генерации → раздел черновика (контракт B4, §2).
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
};

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
      try {
        await action();
      } catch (failure) {
        if (!isVersionConflict(failure)) throw failure;
        setConflict(true);
      }
    });

  const save = (section: DraftSection, data: unknown) =>
    write(async () =>
      setDraft(await api.saveSection(draft.id, draft.version, section, data)),
    );

  const lock = (ref: string, locked: boolean) =>
    write(async () => {
      const locks = locked
        ? [...draft.locks, ref]
        : draft.locks.filter((item) => item !== ref);
      setDraft(await api.setLocks(draft.id, draft.version, locks));
    });

  const generate = (jobStep: AuthoringStep) =>
    write(async () => {
      // Пожелание живёт в черновике: генерация берёт его оттуда, поэтому новое сохраняется первым.
      const wish = wishOf(jobStep);
      if (wish.trim() !== (draft.wishes[jobStep] ?? "")) {
        setDraft(await api.setWish(draft.id, draft.version, jobStep, wish));
      }
      // Дальше поле показывает пожелание, как его знает сервер.
      setTypedWishes((typed) => ({ ...typed, [jobStep]: undefined }));
      setJob(await api.generate(draft.id, { step: jobStep }));
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
          return (
            <div key={item} hidden={item !== step}>
              <Card title={label} headingLevel={3}>
                <div className="screen-stack">
                  <Form
                    key={JSON.stringify(content(draft))}
                    draft={draft}
                    onSave={(data) => void save(section, data)}
                    onLock={(ref, locked) => void lock(ref, locked)}
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
                        onGenerate={() => void generate(item)}
                      />
                    )}
                  />
                </div>
              </Card>
            </div>
          );
        })}
      </fieldset>
      <ErrorAlert error={error} problems={problems} />
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
