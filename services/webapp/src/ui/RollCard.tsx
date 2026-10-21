import { useMotionClass } from "../theme/motion";
import { Badge } from "./Badge";
import { classNames } from "./classNames";
import { Icon } from "./icons";

/** Подписи карточки: всё, что на ней написано словами. Их готовит вызывающий код. */
export interface RollLabels {
  /** Из чего сложился итог; каждая строка в своём элементе («14 + 3 + 2», «1к8+3»). */
  formula: readonly string[];
  /** Против чего брошено («против Сл 15», «против КД 13»); `null` — показан только итог. */
  against: string | null;
  /** Исход словами («Успех», «Промах»); цвет метки задаёт `success`. */
  outcome: string | null;
  /** Строка о выпавших костях («Выпало: 3, 14»); `null` — строки нет. */
  rolls: string | null;
  /** Запись для чтения вслух: доступное имя карточки. */
  summary: string;
}

export interface RollCardProps {
  /** Кто бросает. */
  actor: string;
  /** Что проверяется («Скрытность», «Удар мечом»). */
  label: string;
  /** Значение на кубике d20; у бросков без d20 и у скрытых — `null`. */
  natural: number | null;
  /** Итог так, как его посчитал сервер: карточка ничего не складывает. */
  total: number;
  advantage?: "none" | "advantage" | "disadvantage";
  /** Исход по данным сервера: клиент его не считает. */
  success?: boolean | null;
  labels: RollLabels;
  /** Перекат кубика при появлении; для старых записей ленты выключают. */
  animate?: boolean;
}

/**
 * Единственная карточка броска: кубик, формула, итог и исход. Только показывает числа и
 * подписи, которые ей передали. Её рисуют и игра (через `components/RollCard`), и страница
 * стиля: подписи и правила их сборки живут рядом с данными сервера, в `components/rolls`.
 */
export function RollCard({
  actor,
  label,
  natural,
  total,
  advantage = "none",
  success,
  labels,
  animate = true,
}: RollCardProps) {
  const roll = useMotionClass("ui-anim-roll");
  const { formula, against, outcome, rolls, summary } = labels;

  return (
    <article className="ui-card ui-roll" aria-label={summary}>
      <div
        className={classNames("ui-roll__die", animate && roll)}
        aria-hidden="true"
      >
        <Icon name="d20" size={72} strokeWidth={1} />
        {natural !== null && <span className="ui-roll__face">{natural}</span>}
      </div>
      <div className="ui-roll__body">
        <p className="ui-roll__who">{actor}</p>
        <p className="ui-roll__label">{label}</p>
        <p className="ui-roll__formula">
          {formula.map((part, index) => (
            <span key={index}>{part}</span>
          ))}
        </p>
        <p className="ui-roll__result">
          <strong className="ui-roll__total">{total}</strong>
          {against !== null && <Badge>{against}</Badge>}
          {outcome !== null && (
            <Badge tone={success ? "cloth" : "seal"}>{outcome}</Badge>
          )}
          {advantage === "advantage" && (
            <Badge tone="amber">Преимущество</Badge>
          )}
          {advantage === "disadvantage" && <Badge>Помеха</Badge>}
        </p>
        {rolls !== null && <p className="ui-roll__rolls">{rolls}</p>}
      </div>
    </article>
  );
}
