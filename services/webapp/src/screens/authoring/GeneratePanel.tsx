import { Button } from "../../ui";
import { TextAreaField } from "../fields";
import "./authoring.css";

interface GeneratePanelProps {
  /** Текст «Что хочу увидеть»: его держит редактор, чтобы пересоздание формы шага его не стёрло. */
  wish: string;
  onWishChange: (wish: string) => void;
  /** Шаг закреплён целиком (`step:<шаг>`): генерация его не меняет. */
  locked: boolean;
  /** В форме шага есть несохранённые правки: генерация их заменит. */
  dirty: boolean;
  onGenerate: () => void;
}

/** «Что хочу увидеть» и «Сгенерировать шаг» с предупреждением о незакреплённых правках. */
export function GeneratePanel({
  wish,
  onWishChange,
  locked,
  dirty,
  onGenerate,
}: GeneratePanelProps) {
  return (
    <div className="authoring-generate">
      <TextAreaField
        label="Что хочу увидеть"
        hint="Пожелание к шагу: нейронка учтёт его при генерации."
        rows={3}
        value={wish}
        onChange={(event) => onWishChange(event.target.value)}
      />
      {locked ? (
        <p className="screen-muted">
          Шаг закреплён целиком: генерация его не меняет. Снимите закрепление,
          чтобы сгенерировать шаг заново.
        </p>
      ) : (
        <>
          <p className="form-field__hint">
            Генерация заменит в этом шаге всё незакреплённое, в том числе
            незакреплённые ручные правки. Сначала сохраните раздел, затем
            закрепите то, что хотите сохранить.
          </p>
          {dirty && (
            <p className="authoring-warning">
              В шаге есть несохранённые правки: генерация их заменит.
            </p>
          )}
        </>
      )}
      <div>
        <Button icon="d20" disabled={locked} onClick={onGenerate}>
          Сгенерировать шаг
        </Button>
      </div>
    </div>
  );
}
