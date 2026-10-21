import { useState } from "react";
import { useApi } from "../../api/context";
import type { BackstoryHelpBody } from "../../api/types";
import { useAction } from "../hooks";
import type { Draft } from "./draft";

export interface BackstoryHelp {
  busy: boolean;
  error: string | null;
  /** Попросить мастера написать предысторию по замыслу и черновику. */
  ask: () => void;
  /** Был ли текст игрока до подстановки: его можно вернуть. */
  canRestore: boolean;
  restore: () => void;
  /** Игрок правит текст сам: прежний черновик возвращать уже не нужно. */
  forgetPrevious: () => void;
}

/**
 * «Помоги написать»: мастер отвечает текстом предыстории, он подставляется в поле.
 * Текст игрока до подстановки запоминается, чтобы его не потерять.
 */
export function useBackstoryHelp(
  gameId: string,
  draft: Draft,
  update: (patch: Partial<Draft>) => void,
): BackstoryHelp {
  const api = useApi();
  const { busy, error, run } = useAction();
  const [previous, setPrevious] = useState<string | null>(null);

  const setBackstory = (backstory: string) =>
    update({ persona: { ...draft.persona, backstory } });

  const ask = () => {
    if (busy || draft.classId === null || draft.species === null) return;
    const concept = draft.persona.concept.trim();
    const text = draft.persona.backstory.trim();
    const body: BackstoryHelpBody = {
      class_id: draft.classId,
      species: draft.species,
      ...(concept === "" ? {} : { concept }),
      ...(text === "" ? {} : { draft: text }),
    };
    void run(async () => {
      const { backstory } = await api.backstoryHelp(gameId, body);
      // Пустой черновик возвращать незачем: «вернуть» значило бы стереть поле.
      setPrevious(
        draft.persona.backstory === "" ? null : draft.persona.backstory,
      );
      setBackstory(backstory);
    });
  };

  return {
    busy,
    error,
    ask,
    canRestore: previous !== null,
    restore: () => {
      if (previous === null) return;
      setBackstory(previous);
      setPrevious(null);
    },
    forgetPrevious: () => setPrevious(null),
  };
}
