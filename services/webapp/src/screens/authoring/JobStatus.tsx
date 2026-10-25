import type { Job } from "../../api/types";
import { Alert, Button, Loading } from "../../ui";
import { stepLabel } from "./StepNav";
import "./authoring.css";

/** Задание ещё идёт: пока так, черновик менять нельзя (сервер ответит 409 `job_running`). */
export const isActive = (job: Job) =>
  job.status === "queued" || job.status === "running";

interface JobStatusProps {
  job: Job | null;
  /** Опрос задания сорвался: статус неизвестен, нужен свежий черновик. */
  pollError: string | null;
  busy: boolean;
  onRetry: (job: Job) => void;
  onRefresh: () => void;
}

/** Индикатор задания: «Генерирую…», а при сбое — текст сервера и «Повторить». */
export function JobStatus({
  job,
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
