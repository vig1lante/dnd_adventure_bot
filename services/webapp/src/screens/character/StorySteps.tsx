import { Button, Loading } from "../../ui";
import { ErrorAlert, TextAreaField, TextField } from "../fields";
import "../screens.css";
import "./character.css";
import { LIMITS } from "./draft";
import type { Draft } from "./draft";
import type { StepProps } from "./steps";
import type { BackstoryHelp } from "./useBackstoryHelp";

export function PersonaStep({ draft, update }: StepProps) {
  const { persona } = draft;
  const setPersona = (patch: Partial<Draft["persona"]>) =>
    update({ persona: { ...persona, ...patch } });

  return (
    <div className="screen-stack">
      <TextField
        label="Имя"
        value={draft.name}
        maxLength={LIMITS.name}
        autoComplete="off"
        onChange={(event) => update({ name: event.target.value })}
      />
      <TextField
        label="Замысел одной строкой (необязательно)"
        hint="Помогает мастеру писать предысторию."
        value={persona.concept}
        maxLength={LIMITS.concept}
        autoComplete="off"
        onChange={(event) => setPersona({ concept: event.target.value })}
      />
      <TextAreaField
        label="Внешность"
        value={persona.appearance}
        maxLength={LIMITS.appearance}
        onChange={(event) => setPersona({ appearance: event.target.value })}
      />
      <TextAreaField
        label="Черты характера"
        value={persona.traits}
        maxLength={LIMITS.short}
        onChange={(event) => setPersona({ traits: event.target.value })}
      />
      <TextAreaField
        label="Идеалы"
        value={persona.ideals}
        maxLength={LIMITS.short}
        onChange={(event) => setPersona({ ideals: event.target.value })}
      />
      <TextAreaField
        label="Привязанности"
        value={persona.bonds}
        maxLength={LIMITS.short}
        onChange={(event) => setPersona({ bonds: event.target.value })}
      />
      <TextAreaField
        label="Слабости"
        value={persona.flaws}
        maxLength={LIMITS.short}
        onChange={(event) => setPersona({ flaws: event.target.value })}
      />
    </div>
  );
}

/** Предыстория героя: свой текст или помощь мастера («Помоги написать»). */
export function BackstoryStep({
  draft,
  update,
  help,
}: Pick<StepProps, "draft" | "update"> & { help: BackstoryHelp }) {
  return (
    <div className="screen-stack">
      <TextAreaField
        label="Предыстория героя"
        hint="Можно написать самому или попросить мастера. Необязательно."
        value={draft.persona.backstory}
        maxLength={LIMITS.backstory}
        rows={8}
        readOnly={help.busy}
        onChange={(event) => {
          help.forgetPrevious();
          update({
            persona: { ...draft.persona, backstory: event.target.value },
          });
        }}
      />
      <div className="screen-actions">
        <Button variant="secondary" busy={help.busy} onClick={help.ask}>
          Помоги написать
        </Button>
        {help.canRestore && (
          <Button variant="secondary" onClick={help.restore}>
            Вернуть мой текст
          </Button>
        )}
      </div>
      {help.busy && (
        <Loading label="Мастер пишет предысторию… Это может занять до минуты." />
      )}
      <ErrorAlert error={help.error} problems={[]} />
    </div>
  );
}
