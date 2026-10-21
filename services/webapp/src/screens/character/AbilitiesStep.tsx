import { useEffect, useRef } from "react";
import { useApi } from "../../api/context";
import type { Ability } from "../../api/types";
import { Button, Loading } from "../../ui";
import { ErrorAlert, SelectField } from "../fields";
import { useAction } from "../hooks";
import "../screens.css";
import "./character.css";
import { availableValues, canLower, canRaise, pointBuyLeft } from "./draft";
import type { Assignment } from "./draft";
import type { StepProps } from "./steps";

const METHODS = [
  { id: "point_buy", label: "Покупка очков" },
  { id: "standard_array", label: "Стандартный набор" },
] as const;

function PointBuy({ draft, options, update }: StepProps) {
  const scores = draft.pointBuy;
  const step = (ability: Ability, delta: number) =>
    update({ pointBuy: { ...scores, [ability]: scores[ability] + delta } });

  return (
    <div className="char-points">
      <p className="char-counter" aria-live="polite">
        {`Осталось ${pointBuyLeft(scores, options)} из ${options.point_buy.budget}`}
      </p>
      {options.abilities.map(({ id, name }) => (
        <div key={id} role="group" aria-label={name} className="char-point">
          <span className="char-point__label">
            <span className="char-point__name">{name}</span>
            <span className="char-point__cost">
              {`${options.point_buy.costs[String(scores[id])] ?? 0} оч.`}
            </span>
          </span>
          <Button
            variant="secondary"
            className="char-round"
            aria-label={`Уменьшить ${name}`}
            disabled={!canLower(scores, id, options)}
            onClick={() => step(id, -1)}
          >
            −
          </Button>
          <span className="char-point__value">{scores[id]}</span>
          <Button
            variant="secondary"
            className="char-round"
            aria-label={`Увеличить ${name}`}
            disabled={!canRaise(scores, id, options)}
            onClick={() => step(id, 1)}
          >
            +
          </Button>
        </div>
      ))}
    </div>
  );
}

interface AssignProps {
  options: StepProps["options"];
  values: readonly number[];
  assigned: Assignment;
  onAssign: (assigned: Assignment) => void;
}

/** Раздача готовых значений по характеристикам: каждое значение — одной характеристике. */
function Assign({ options, values, assigned, onAssign }: AssignProps) {
  return (
    <>
      {options.abilities.map(({ id, name }) => (
        <SelectField
          key={id}
          label={name}
          placeholder="—"
          value={assigned[id] === null ? "" : String(assigned[id])}
          options={availableValues(values, assigned, id).map((value) => ({
            value: String(value),
            label: String(value),
          }))}
          onChange={(value) =>
            onAssign({ ...assigned, [id]: value === "" ? null : Number(value) })
          }
        />
      ))}
    </>
  );
}

/**
 * Способы задать характеристики. «Покупка очков» и «Стандартный набор» — переключатели,
 * «Распределить случайно» — действие: сервер раскладывает те же очки по классу героя, ответ
 * кладётся в покупку очков, и его можно поправить «+» и «−». Нажимать можно сколько угодно.
 * Значения каждого способа хранятся отдельно.
 */
export function AbilitiesStep(props: StepProps & { gameId: string }) {
  const { draft, options, update, gameId } = props;
  const api = useApi();
  const { busy, error, problems, run } = useAction();

  // Игрок мог уйти с шага, пока сервер раскладывал: поздний ответ тогда ничего не меняет.
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const classId = draft.classId;
  // Раскладку, как и любые числа, считает сервер; клиент кладёт ответ в покупку очков как есть.
  const scatter = () => {
    if (classId === null) return;
    void run(async () => {
      const { method, scores } = await api.randomAbilities(gameId, classId);
      if (mounted.current) update({ method, pointBuy: scores });
    });
  };

  return (
    <div className="screen-stack">
      <fieldset className="char-methods">
        <legend className="form-legend">Способ</legend>
        {METHODS.map(({ id, label }) => (
          <Button
            key={id}
            variant={draft.method === id ? "primary" : "secondary"}
            aria-pressed={draft.method === id}
            disabled={busy}
            onClick={() => update({ method: id })}
          >
            {label}
          </Button>
        ))}
        <Button
          variant="secondary"
          busy={busy}
          disabled={classId === null}
          onClick={scatter}
        >
          Распределить случайно
        </Button>
      </fieldset>
      <p className="char-note">
        {`«Распределить случайно» сам раскладывает те же ${options.point_buy.budget} очков: больше получают ключевые характеристики класса. Раскладку можно поправить кнопками «+» и «−».`}
      </p>
      {busy && <Loading label="Сервер раскладывает очки…" />}
      <ErrorAlert error={error} problems={problems} />
      {draft.method === "point_buy" && <PointBuy {...props} />}
      {draft.method === "standard_array" && (
        <Assign
          options={options}
          values={options.standard_array}
          assigned={draft.standard}
          onAssign={(standard) => update({ standard })}
        />
      )}
    </div>
  );
}
