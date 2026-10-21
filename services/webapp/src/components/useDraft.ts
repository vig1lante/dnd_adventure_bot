import { useCallback, useEffect, useRef, useState } from "react";
import { errorText } from "../api/client";
import { useApi } from "../api/context";
import type { PlayerAction } from "../api/types";
import { useGame } from "../store/game";

/** Через сколько после последнего ввода черновик уходит на сервер. */
export const AUTOSAVE_MS = 800;

export interface DraftValues {
  text: string;
  secret: boolean;
  inspiration: boolean;
}

const EMPTY: DraftValues = { text: "", secret: false, inspiration: false };

const valuesOf = (action: PlayerAction | null): DraftValues =>
  action === null
    ? EMPTY
    : {
        text: action.text,
        secret: action.secret,
        inspiration: action.inspiration,
      };

const same = (a: DraftValues, b: DraftValues) =>
  a.text === b.text && a.secret === b.secret && a.inspiration === b.inspiration;

// Ответ на запрос прежнего хода, пришедший уже в новом, к нему не относится и не применяется.
const currentTurn = () => useGame.getState().live.turn;

export interface UseDraftOptions {
  gameId: string;
  /** Ход игрока, каким его знает сервер (снимок или последний ответ REST). */
  saved: PlayerAction | null;
  /** У героя есть вдохновение: без него флажок на сервер не уходит. */
  hasInspiration: boolean;
  /** Поле хода сейчас открыто: в закрытом черновик на сервер не отправляется. */
  open: boolean;
}

/**
 * Черновик хода игрока: правки, автосохранение через 800 мс после ввода и три действия
 * «Готов», «Пас», «Изменить». Всё, что хоть раз показали игроку, но сервер ещё не знает,
 * — `edited`; сервер знает `saved`. Запросы идут по одному, последний уходит с последним
 * текстом, а перед «Готов» и «Пас» несохранённый текст досылается: сервер сдаёт ход по тому,
 * что у него записано. Ответы сервера кладутся в хранилище — событий о своём ходе нет.
 */
export function useDraft({
  gameId,
  saved,
  hasInspiration,
  open,
}: UseDraftOptions) {
  const api = useApi();
  const setAction = useGame((state) => state.setAction);
  const [edited, setEdited] = useState<DraftValues | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const confirmed = valuesOf(saved);
  const values = edited ?? confirmed;

  // Таймер и запросы читают значения позже, чем была отрисовка: берут свежие отсюда.
  const latest = useRef({ values, confirmed, hasInspiration, open });
  useEffect(() => {
    latest.current = { values, confirmed, hasInspiration, open };
  });
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const queue = useRef<Promise<boolean>>(Promise.resolve(true));
  const acting = useRef(false);

  // Уход с экрана и смена хода: отложенное сохранение не должно улететь в чужой ход.
  useEffect(() => () => clearTimeout(timer.current), []);

  /** Отправляет черновик, если он отличается от записанного на сервере. `false` — не удалось. */
  const save = useCallback((): Promise<boolean> => {
    const run = async (): Promise<boolean> => {
      const current = latest.current;
      if (!current.open || same(current.values, current.confirmed)) return true;
      const sent = current.values;
      const turn = currentTurn();
      try {
        const action = await api.saveDraft(gameId, {
          text: sent.text,
          secret: sent.secret,
          inspiration: sent.inspiration && current.hasInspiration,
        });
        if (currentTurn() !== turn) return true;
        latest.current.confirmed = valuesOf(action);
        setAction(action);
        // Если за время запроса текст изменился, новая правка остаётся ждать своей очереди.
        setEdited((now) => (now !== null && same(now, sent) ? null : now));
        setError(null);
        return true;
      } catch (failure) {
        setError(errorText(failure));
        return false;
      }
    };
    queue.current = queue.current.then(run);
    return queue.current;
  }, [api, gameId, setAction]);

  const flush = useCallback((): Promise<boolean> => {
    clearTimeout(timer.current);
    return save();
  }, [save]);

  // Вкладку свернули (Telegram ушёл в фон): не ждём 800 мс, пока приложение заморозят.
  useEffect(() => {
    const onHide = () => {
      if (document.visibilityState === "hidden") void flush();
    };
    document.addEventListener("visibilitychange", onHide);
    return () => document.removeEventListener("visibilitychange", onHide);
  }, [flush]);

  const change = (patch: Partial<DraftValues>) => {
    const next = { ...latest.current.values, ...patch };
    latest.current.values = next;
    setEdited(next);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => void save(), AUTOSAVE_MS);
  };

  /** Одно действие за раз; отказ сервера остаётся текстом для игрока. */
  const perform = async (action: () => Promise<void>) => {
    if (acting.current) return;
    acting.current = true;
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      acting.current = false;
      setBusy(false);
    }
  };

  /** Сначала досылаем несохранённое, потом само действие: иначе сдастся устаревший текст. */
  const afterFlush = (request: () => Promise<PlayerAction>) =>
    perform(async () => {
      const turn = currentTurn();
      if (!(await flush())) return;
      const action = await request();
      if (currentTurn() === turn) setAction(action);
    });

  return {
    values,
    busy,
    error,
    change,
    submit: () => afterFlush(() => api.submitAction(gameId)),
    pass: () => afterFlush(() => api.passAction(gameId)),
    /** Возвращает сданный ход или пас в черновик: любая правка на сервере так и делает. */
    edit: () =>
      perform(async () => {
        const row = latest.current.confirmed;
        const turn = currentTurn();
        const action = await api.saveDraft(gameId, {
          text: row.text,
          secret: row.secret,
          inspiration: row.inspiration && latest.current.hasInspiration,
        });
        if (currentTurn() === turn) setAction(action);
      }),
  };
}
