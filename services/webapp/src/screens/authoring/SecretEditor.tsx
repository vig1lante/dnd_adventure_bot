import type { LocationSecret } from "../../api/types";
import { TextAreaField, TextField } from "../fields";
import { CheckField, RevealsField } from "./FindingFields";
import { ID_HINT } from "./labels";

interface SecretEditorProps {
  secret: LocationSecret;
  update: (patch: Partial<LocationSecret>) => void;
}

/** Тайна места для мастера: текст, проверка, которой её раскрывают, и что она раскрывает. */
export function SecretEditor({ secret, update }: SecretEditorProps) {
  return (
    <>
      <TextField
        label="Идентификатор"
        hint={`${ID_HINT} Уникален во всём приключении.`}
        autoComplete="off"
        value={secret.id}
        onChange={(event) => update({ id: event.target.value })}
      />
      <TextAreaField
        label="Тайна"
        hint="Знает только мастер."
        rows={2}
        value={secret.text}
        onChange={(event) => update({ text: event.target.value })}
      />
      <CheckField
        value={secret.check}
        onChange={(check) => update({ check })}
      />
      <RevealsField
        value={secret.reveals}
        kinds={["clue", "item", "flag", "exit"]}
        onChange={(reveals) => update({ reveals })}
      />
    </>
  );
}
