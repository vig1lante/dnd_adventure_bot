import type { Request } from "./client";
import type {
  AuthoringStep,
  Draft,
  DraftRef,
  DraftReport,
  DraftSection,
  DraftSummary,
  GenerateBody,
  Job,
  PublishResult,
  SrdMonster,
} from "./types";

/**
 * Методы конструктора приключений. Они входят в общий клиент `createApi` и ходят через его
 * `request`: тот же токен сессии и те же ошибки по-русски.
 * Каждая запись в черновик несёт `version`: чужая версия даёт 409 `version_conflict`.
 */
export function authoringApi(request: Request) {
  const draftPath = (draftId: string) =>
    `/api/drafts/${encodeURIComponent(draftId)}`;

  return {
    listDrafts: () => request<DraftSummary[]>("GET", "/api/drafts"),
    createDraft: () => request<Draft>("POST", "/api/drafts", { body: {} }),
    getDraft: (draftId: string) => request<Draft>("GET", draftPath(draftId)),
    /**
     * Удалить черновик; ответ 204. Опубликованное из него приключение остаётся в каталоге и
     * играх. Пока идёт генерация — 409 `job_running`.
     */
    deleteDraft: (draftId: string) =>
      request<void>("DELETE", draftPath(draftId)),
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
    /** Справочник ссылок черновика для выбора в формах. */
    listRefs: (draftId: string) =>
      request<DraftRef[]>("GET", `${draftPath(draftId)}/refs`),
    /** Проверка черновика: находки со ссылками на шаг, балл, время и доля готовности. */
    validate: (draftId: string) =>
      request<DraftReport>("POST", `${draftPath(draftId)}/validate`),
    /** Публикация в каталог; блокирующие ошибки — 422, занятый id — 409. */
    publish: (draftId: string) =>
      request<PublishResult>("POST", `${draftPath(draftId)}/publish`, {
        body: {},
      }),
    /** Архив zip папки приключения v2. */
    exportArchive: (draftId: string) =>
      request<Blob>("GET", `${draftPath(draftId)}/export`, { blob: true }),
    /**
     * Новый черновик из архива zip: тело — сам файл с типом `application/zip`, без multipart.
     * Тип задаётся здесь: браузер не всегда узнаёт его у выбранного файла.
     */
    importArchive: (archive: Blob) =>
      request<Draft>("POST", "/api/drafts/import", {
        raw: { data: archive, type: "application/zip" },
      }),
    /** Копия приключения каталога (папка или опубликованное) в новый черновик. */
    copyAdventure: (adventureId: string) =>
      request<Draft>(
        "POST",
        `/api/adventures/${encodeURIComponent(adventureId)}/draft`,
      ),
    /** Справочник монстров SRD для выбора в боях. */
    listSrdMonsters: () => request<SrdMonster[]>("GET", "/api/srd/monsters"),
  };
}
