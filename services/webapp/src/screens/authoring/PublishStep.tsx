import { useId, useState } from "react";
import { ApiError } from "../../api/client";
import { useApi } from "../../api/context";
import type { Draft, JournalFields, PublishResult } from "../../api/types";
import { Button, ErrorState, Skeleton } from "../../ui";
import { ErrorAlert } from "../fields";
import { useAction } from "../hooks";
import { AlertDialog, ConflictDialog } from "./fields";
import { ExportButton } from "./ImportExport";
import { openItems } from "./ItemsStep";
import { publishProblem } from "./problems";
import { GoButton, useDraftReport } from "./ReviewStep";
import type { EditorScreen } from "./StepNav";

// Отказы публикации, которые исправляют на «Замысле»: id не задан, занят или уже закреплён
// прежней публикацией (B6 §6).
const FIX_ON_CONCEPT = new Set([
  "no_adventure_id",
  "id_taken",
  "adventure_id_locked",
]);

/** «Что знают игроки» сущностей с непустым полем: «Локация «Склеп»: …». */
function publicRows(draft: Draft): string[] {
  const rows = (
    label: string,
    entities: readonly (JournalFields & { name: string })[],
  ) =>
    entities
      .filter((entity) => entity.public.trim() !== "")
      .map((entity) => `${label} «${entity.name}»: ${entity.public}`);
  return [
    ...rows("Регион", draft.world?.regions ?? []),
    ...rows(
      "Слух",
      (draft.world?.rumors ?? []).map((rumor) => ({
        ...rumor,
        name: rumor.text,
      })),
    ),
    ...rows("Бог", draft.deities),
    ...rows("Фракция", draft.factions),
    ...rows("Локация", draft.locations),
    ...rows("Персонаж", draft.npcs),
    ...rows("Предмет", draft.items),
  ];
}

/** Что после публикации увидят игроки: название, завязка, начало дневника и «Что знают игроки». */
function PlayersSee({ draft }: { draft: Draft }) {
  const titleId = useId();
  const rows = [
    `Название: ${draft.title || "—"}`,
    `Завязка: ${draft.settings.pitch || "—"}`,
    `Главное для дневника: ${draft.journal_intro || "возьмётся завязка"}`,
    ...publicRows(draft),
  ];
  return (
    <div className="screen-stack">
      <p id={titleId} className="form-legend">
        Это увидят игроки
      </p>
      <ul aria-labelledby={titleId} className="form-problems">
        {rows.map((row, index) => (
          // Строки могут совпасть (два «Что знают игроки» с одним текстом): номер делает ключ уникальным.
          <li key={`${index}:${row}`}>{row}</li>
        ))}
      </ul>
      <p className="authoring-warning">
        Не пишите в эти поля тайн: после публикации их увидят все игроки.
      </p>
    </div>
  );
}

/** Поле «Замысла», к которому ведут кнопки перехода «Публикации»: id задают там. */
const ADVENTURE_ID_FIELD = "id приключения";

interface PublishStepProps {
  draft: Draft;
  /** Переход к шагу; `field` — подпись поля, на которое встанет фокус. */
  onGo: (screen: EditorScreen, field?: string) => void;
  /** Перечитать черновик: публикация отвечает 409 `version_conflict`, если он изменился. */
  onReload: () => void;
}

/**
 * Экран «Публикация»: что увидят игроки, «Опубликовать» (закрыта, пока проверка находит ошибки
 * или не задан id), подтверждение для предметов, лежащих открыто, ответы сервера и архив.
 */
export function PublishStep({ draft, onGo, onReload }: PublishStepProps) {
  const api = useApi();
  const toConcept = (screen: EditorScreen) => onGo(screen, ADVENTURE_ID_FIELD);
  const [report, reload] = useDraftReport(draft.id);
  const { busy, error, problems, run } = useAction();
  const [failure, setFailure] = useState<string | undefined>();
  // Черновик изменился после чтения публикацией: диалог обновления, как у записи раздела.
  const [conflict, setConflict] = useState(false);
  const [published, setPublished] = useState<PublishResult | null>(null);
  const [confirming, setConfirming] = useState(false);
  const open = openItems(draft);
  const errors = report.status === "ready" ? report.data.errors.length : null;
  const noId = draft.adventure_id === null;
  const canPublish = errors === 0 && !noId;

  const publish = () =>
    run(async () => {
      setConfirming(false);
      setConflict(false);
      setFailure(undefined);
      setPublished(null);
      try {
        setPublished(await api.publish(draft.id));
      } catch (caught) {
        const code = caught instanceof ApiError ? caught.code : undefined;
        if (code === "version_conflict") {
          setConflict(true);
          return;
        }
        setFailure(code);
        throw caught;
      }
    });

  return (
    <div className="screen-stack">
      <PlayersSee draft={draft} />
      {noId ? (
        <div className="screen-stack">
          <p className="authoring-warning">
            Сначала задайте id приключения на шаге «Замысел».
          </p>
          <div>
            <GoButton screen="concept" onGo={toConcept} />
          </div>
        </div>
      ) : (
        <p className="screen-muted">id приключения: {draft.adventure_id}</p>
      )}
      {report.status === "loading" && (
        <Skeleton label="Проверяем черновик…" rows={1} />
      )}
      {report.status === "error" && (
        <ErrorState title="Не удалось проверить черновик" onRetry={reload}>
          <p>{report.message}</p>
          <p>Пока проверки нет, публикация закрыта.</p>
        </ErrorState>
      )}
      {/* Проверка отбрасывает находки пустых шагов (B4 §8), публикация проверяет всё (B6 §6). */}
      {report.status === "ready" && report.data.readiness_percent < 100 && (
        <p className="form-field__hint">
          Проверка показывает находки только заполненных шагов; при публикации
          сервер проверит всё.
        </p>
      )}
      {errors !== null && errors > 0 && (
        <div className="screen-stack">
          <p className="authoring-warning">
            Сначала исправьте ошибки проверки: {errors}.
          </p>
          <div>
            <GoButton screen="review" onGo={onGo} />
          </div>
        </div>
      )}
      <div>
        <Button
          icon="seal"
          busy={busy}
          disabled={!canPublish}
          onClick={() => {
            if (open.length > 0) setConfirming(true);
            else void publish();
          }}
        >
          Опубликовать
        </Button>
      </div>
      {confirming && (
        <AlertDialog
          title="Предметы лежат открыто"
          actions={
            <>
              <Button onClick={() => void publish()}>
                Опубликовать всё равно
              </Button>
              <Button variant="secondary" onClick={() => setConfirming(false)}>
                Отмена
              </Button>
            </>
          }
        >
          <p className="screen-muted">
            Их держатель — локация, и ни одна деталь их не прячет: игроки найдут
            их, просто осмотрев место.
          </p>
          <ul className="form-problems">
            {open.map(({ item, place }, index) => (
              <li key={`${index}:${item.id}`}>
                {item.name || item.id} ({place})
              </li>
            ))}
          </ul>
        </AlertDialog>
      )}
      {published !== null && (
        <div className="screen-stack">
          <p>Приключение опубликовано под id «{published.adventure_id}».</p>
          <p className="screen-muted">
            Оно уже в списке приключений. Создайте игру на нём: вкладка «Мои
            игры» → «Создать игру». Начатые игры публикация не меняет. Менять id
            приключения теперь нельзя.
          </p>
        </div>
      )}
      {conflict && (
        <ConflictDialog
          onRefresh={onReload}
          onCancel={() => setConflict(false)}
        />
      )}
      <ErrorAlert error={error} problems={problems.map(publishProblem)} />
      {failure !== undefined && FIX_ON_CONCEPT.has(failure) && (
        <div>
          <GoButton screen="concept" onGo={toConcept} />
        </div>
      )}
      <ExportButton draft={draft} />
    </div>
  );
}
