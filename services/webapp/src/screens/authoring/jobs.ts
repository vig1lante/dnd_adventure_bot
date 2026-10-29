import type { Job } from "../../api/types";

/** Задание ещё идёт: пока так, черновик менять нельзя (сервер ответит 409 `job_running`). */
export const isActive = (job: Job) =>
  job.status === "queued" || job.status === "running";
