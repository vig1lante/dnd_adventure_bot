import type { Request } from "./client";
import type {
  AuthoringStep,
  Draft,
  DraftRef,
  DraftSection,
  DraftSummary,
  GenerateBody,
  Job,
} from "./types";

/**
 * Методы конструктора приключений (спека §10.3, дополнение к контракту B4). Они входят в общий
 * клиент `createApi` и ходят через его `request`: тот же токен сессии и те же ошибки по-русски.
 * Каждая запись в черновик несёт `version`: чужая версия даёт 409 `version_conflict`.
 */
export function authoringApi(request: Request) {
  const draftPath = (draftId: string) =>
    `/api/drafts/${encodeURIComponent(draftId)}`;

  return {
    listDrafts: () => request<DraftSummary[]>("GET", "/api/drafts"),
    createDraft: () => request<Draft>("POST", "/api/drafts", { body: {} }),
    getDraft: (draftId: string) => request<Draft>("GET", draftPath(draftId)),
    saveSection: (
      draftId: string,
      version: number,
      section: DraftSection,
      data: unknown,
    ) =>
      request<Draft>("PUT", `${draftPath(draftId)}/sections/${section}`, {
        body: { version, data },
      }),
    setLocks: (draftId: string, version: number, locks: string[]) =>
      request<Draft>("PUT", `${draftPath(draftId)}/locks`, {
        body: { version, locks },
      }),
    setWish: (
      draftId: string,
      version: number,
      step: AuthoringStep,
      text: string,
    ) =>
      request<Draft>("PUT", `${draftPath(draftId)}/wishes/${step}`, {
        body: { version, text },
      }),
    generate: (draftId: string, body: GenerateBody) =>
      request<Job>("POST", `${draftPath(draftId)}/generate`, { body }),
    getJob: (draftId: string, jobId: string) =>
      request<Job>(
        "GET",
        `${draftPath(draftId)}/jobs/${encodeURIComponent(jobId)}`,
      ),
    /** Справочник ссылок черновика для выбора в формах (дополнение B5, §6). */
    listRefs: (draftId: string) =>
      request<DraftRef[]>("GET", `${draftPath(draftId)}/refs`),
  };
}
