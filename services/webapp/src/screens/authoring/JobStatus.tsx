import type { Job } from "../../api/types";
import { Alert, Button, Loading } from "../../ui";
import { stepLabel } from "./StepNav";
import "./authoring.css";

/** Задание ещё идёт: пока так, черновик менять нельзя (сервер ответит 409 `job_running`). */
export const isActive = (job: Job) =>
  job.status === "queued" || job.status === "running";

interface JobStatusProps {
  job: Job | null;
  /** Задание упало ещё до открытия черновика: это заметка о прошлом, а не ошибка этого входа. */
  past?: boolean;
  /** Опрос задания сорвался: статус неизвестен, нужен свежий черновик. */
  pollError: string | null;
  busy: boolean;
  onRetry: (job: Job) => void;
  onRefresh: () => void;
}

/**
 * Индикатор задания: «Генерирую…», а при сбое — текст сервера и «Повторить». Сбой, случившийся
 * до открытия черновика, — спокойной заметкой без тревоги.
 */
export function JobStatus({
  job,
  past = false,
  pollError,
  busy,
  onRetry,
  onRefresh,
}: JobStatusProps) {
  if (pollError !== null) {
    return (
      <div className="screen-stack">
        <Alert>{pollError}</Alert>
        <div>
          <Button variant="secondary" onClick={onRefresh}>
            Обновить
          </Button>
        </div>
      </div>
    );
  }
  if (job === null) return null;
  if (isActive(job)) {
    return (
      <div className="authoring-job">
        <Loading label="Генерирую…" />
        <p className="screen-muted">
          Шаг «{stepLabel(job.step)}». Пока идёт генерация, правка недоступна.
        </p>
      </div>
    );
  }
  if (job.status === "failed" && past) {
    return (
      <div className="authoring-job">
        <p className="screen-muted">
          Прошлая генерация шага «{stepLabel(job.step)}» не удалась.
        </p>
        {job.error !== null && <p className="screen-muted">{job.error}</p>}
        <div>
          <Button variant="secondary" busy={busy} onClick={() => onRetry(job)}>
            Повторить
          </Button>
        </div>
      </div>
    );
  }
  if (job.status === "failed") {
    return (
      <div className="screen-stack">
        <Alert>
          <p>Не удалось сгенерировать шаг «{stepLabel(job.step)}».</p>
          {job.error !== null && <p>{job.error}</p>}
        </Alert>
        <div>
          <Button busy={busy} onClick={() => onRetry(job)}>
            Повторить
          </Button>
        </div>
      </div>
    );
  }
  return null;
}
