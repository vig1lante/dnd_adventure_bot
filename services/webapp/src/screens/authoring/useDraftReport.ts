import { useCallback } from "react";
import { useApi } from "../../api/context";
import { useLoaded } from "../hooks";

/** Отчёт проверки черновика (`POST /validate`): запрашивается при показе экрана и по `reload`. */
export function useDraftReport(draftId: string) {
  const api = useApi();
  const load = useCallback(() => api.validate(draftId), [api, draftId]);
  return useLoaded(load);
}
