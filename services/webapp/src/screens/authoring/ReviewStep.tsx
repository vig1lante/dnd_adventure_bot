import { useId } from "react";
import type { Draft, Finding } from "../../api/types";
import { Button, ErrorState, Skeleton } from "../../ui";
import { FINDING_KINDS } from "./labels";
import { screenLabel } from "./steps";
import type { EditorScreen } from "./steps";
import { useDraftReport } from "./useDraftReport";
import "./authoring.css";

// Сущности с именем: по ссылке находки показывается имя, остальные — своим id.
const NAMED: Partial<
  Record<string, (draft: Draft) => readonly { id: string; name: string }[]>
> = {
  location: (draft) => draft.locations,
  npc: (draft) => draft.npcs,
  item: (draft) => draft.items,
  deity: (draft) => draft.deities,
  faction: (draft) => draft.factions,
  monster: (draft) => draft.monsters,
};

/** Где найдена находка, словами: `location:crypt/exit:down` → «локация «Склеп» → выход «down»». */
function entityText(ref: string, draft: Draft): string {
  return ref
    .split("/")
    .map((part) => {
      const colon = part.indexOf(":");
      if (colon < 0) return FINDING_KINDS[part] ?? part;
      const kind = part.slice(0, colon);
      const id = part.slice(colon + 1);
      const name =
        NAMED[kind]?.(draft).find((entity) => entity.id === id)?.name || id;
      return `${FINDING_KINDS[kind] ?? kind} «${name}»`;
    })
    .join(" → ");
}

/** Оценка времени игры сервера словами: 150 → «около 2 ч 30 мин». Только запись числа. */
function durationText(minutes: number): string {
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  const parts = [
    hours > 0 ? `${hours} ч` : null,
    rest > 0 || hours === 0 ? `${rest} мин` : null,
  ].filter((part) => part !== null);
  return `около ${parts.join(" ")}`;
}

interface GoButtonProps {
  screen: EditorScreen;
  onGo: (screen: EditorScreen) => void;
}

/** Переход на экран редактора: к шагу находки, к проверке, к замыслу. */
export function GoButton({ screen, onGo }: GoButtonProps) {
  return (
    <Button variant="secondary" onClick={() => onGo(screen)}>
      Перейти к шагу «{screenLabel(screen)}»
    </Button>
  );
}

/** Переход по ссылке находки: экран шага и, если есть, сущность (`ref` находки). */
type FindingGo = (screen: EditorScreen, ref?: string) => void;

interface FindingsProps {
  title: string;
  empty: string;
  findings: readonly Finding[];
  draft: Draft;
  onGo: FindingGo;
}

/**
 * Список находок: текст сервера как есть, где найдена и переход к шагу, который за неё
 * отвечает, — прямо к её сущности на экране шага.
 */
function Findings({ title, empty, findings, draft, onGo }: FindingsProps) {
  const titleId = useId();
  return (
    <div className="screen-stack">
      <p id={titleId} className="form-legend">
        {title}
      </p>
      {findings.length === 0 ? (
        <p className="screen-muted">{empty}</p>
      ) : (
        <ul aria-labelledby={titleId} className="authoring-entities">
          {findings.map((finding, index) => (
            // Сервер может прислать одинаковые находки: номер делает ключ уникальным.
            <li key={`${index}:${finding.code}:${finding.ref}`}>
              <div className="authoring-entity">
                <p>{finding.message}</p>
                <p className="screen-muted">{entityText(finding.ref, draft)}</p>
                {finding.step !== null && (
                  <div>
                    <GoButton
                      screen={finding.step}
                      onGo={(screen) => onGo(screen, finding.ref)}
                    />
                  </div>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

interface ReviewStepProps {
  draft: Draft;
  onGo: FindingGo;
}

/**
 * Экран «Проверка»: отчёт сервера по черновику — доля готовности полоской, балл, оценка времени,
 * ошибки и предупреждения со ссылками на шаг. Считает всё сервер; экран только показывает.
 */
export function ReviewStep({ draft, onGo }: ReviewStepProps) {
  const [report, reload] = useDraftReport(draft.id);

  if (report.status === "loading") {
    return <Skeleton label="Проверяем черновик…" rows={2} />;
  }
  if (report.status === "error") {
    return (
      <ErrorState title="Не удалось проверить черновик" onRetry={reload}>
        <p>{report.message}</p>
      </ErrorState>
    );
  }
  const { errors, warnings, score, estimated_minutes, readiness_percent } =
    report.data;
  return (
    <div className="screen-stack">
      <div className="screen-stack">
        <p>Готовность: {readiness_percent}%</p>
        <div
          role="progressbar"
          aria-label="Готовность"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={readiness_percent}
          className="authoring-progress"
        >
          <div
            className="authoring-progress__fill"
            style={{ width: `${readiness_percent}%` }}
          />
        </div>
      </div>
      <p>Балл: {score} из 100</p>
      <p>Время игры: {durationText(estimated_minutes)}</p>
      <Findings
        title="Ошибки"
        empty="Ошибок нет."
        findings={errors}
        draft={draft}
        onGo={onGo}
      />
      <Findings
        title="Предупреждения"
        empty="Предупреждений нет."
        findings={warnings}
        draft={draft}
        onGo={onGo}
      />
      <div>
        <Button variant="secondary" onClick={reload}>
          Проверить снова
        </Button>
      </div>
    </div>
  );
}
