import { useId } from "react";
import { useApi } from "../../api/context";
import type { Draft } from "../../api/types";
import { Button } from "../../ui";
import { ErrorAlert } from "../fields";
import { useAction } from "../hooks";

// Сколько держать ссылку на скачанный файл: Safari (и вебвью Telegram на iOS) не успевает начать
// скачивание, если отпустить ссылку сразу после щелчка.
const RELEASE_MS = 60_000;

/** Отдаёт файл браузеру на скачивание под именем `name`. */
function saveFile(file: Blob, name: string) {
  const url = URL.createObjectURL(file);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), RELEASE_MS);
}

/** «Скачать архив»: zip папки приключения (`GET /export`) файлом `<id приключения>.zip`. */
export function ExportButton({ draft }: { draft: Draft }) {
  const api = useApi();
  const { busy, error, problems, run } = useAction();
  const adventureId = draft.adventure_id;

  const download = () =>
    run(async () => {
      if (adventureId === null) return;
      saveFile(await api.exportArchive(draft.id), `${adventureId}.zip`);
    });

  return (
    <div className="screen-stack">
      <p className="form-field__hint">
        Архив zip с папкой приключения: его можно импортировать в новый черновик
        или положить в папку приключений сервера.
      </p>
      <div>
        <Button
          variant="secondary"
          icon="scroll"
          busy={busy}
          disabled={adventureId === null}
          onClick={() => void download()}
        >
          Скачать архив
        </Button>
      </div>
      <ErrorAlert error={error} problems={problems} />
    </div>
  );
}

/**
 * «Импорт архива»: выбранный zip уходит на сервер как есть (`POST /api/drafts/import`) и становится
 * новым черновиком; отказ — текст сервера и список проблем по-русски.
 */
export function ImportButton({
  onImported,
}: {
  onImported: (draft: Draft) => void;
}) {
  const api = useApi();
  const { busy, error, problems, run } = useAction();
  const inputId = useId();
  const hintId = useId();

  return (
    <div className="screen-stack">
      <div className="form-field">
        <label htmlFor={inputId} className="form-field__label">
          Импорт архива (.zip)
        </label>
        <input
          id={inputId}
          type="file"
          accept=".zip,application/zip"
          className="form-field__input"
          aria-describedby={hintId}
          disabled={busy}
          onChange={(event) => {
            const input = event.currentTarget;
            const file = input.files?.[0];
            // Поле очищается сразу: тот же файл можно выбрать снова, исправив его.
            input.value = "";
            if (file !== undefined) {
              void run(async () => onImported(await api.importArchive(file)));
            }
          }}
        />
        <p id={hintId} className="form-field__hint">
          Архив из «Скачать архив» или папка приключения в zip, не больше 2 МиБ.
          Станет новым черновиком.
        </p>
      </div>
      <ErrorAlert error={error} problems={problems} />
    </div>
  );
}
