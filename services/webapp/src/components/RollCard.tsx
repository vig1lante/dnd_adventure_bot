import type { FeedItem } from "../api/types";
import { RollCard as RollCardView } from "../ui";
import type { RollLabels } from "../ui";
import "./game.css";
import {
  againstLabel,
  outcomeLabel,
  parseRoll,
  rollFormula,
  rollSummary,
} from "./rolls";
import type { Roll } from "./rolls";

export interface RollCardProps {
  /** Запись ленты `kind: "roll"`: карточка собирается из её `data`, `text` — запасной. */
  item: FeedItem;
  /** Перекат кубика при появлении; для записей, что уже были на экране, выключают. */
  animate?: boolean;
}

/**
 * Подписи игры: к ним добавлены «против КД», кость эффекта в формуле, «Попадание» и запись
 * одной строкой для чтения вслух. Всё это слова вокруг чисел сервера, а не расчёт.
 */
function gameLabels(roll: Roll): RollLabels {
  // Кости эффектов уже в формуле, а вот урон из нескольких костей и преимущество — нет.
  const showRolls =
    roll.natural === null
      ? roll.rolls.length > 1
      : roll.rolls.length > roll.kept.length;
  return {
    formula: [rollFormula(roll)],
    against: againstLabel(roll),
    outcome: outcomeLabel(roll),
    rolls: showRolls ? `Выпало: ${roll.rolls.join(", ")}` : null,
    summary: `${roll.actor}. ${rollSummary(roll)}`,
  };
}

/**
 * Карточка броска в ленте игры: данные записи разбираются в бросок и рисуются общей
 * карточкой `ui/RollCard`, той же, что показывает страница стиля. Числа и исход приходят
 * с сервера и только показываются.
 */
export function RollCard({ item, animate = true }: RollCardProps) {
  const roll = parseRoll(item.data);
  // Запись старой или чужой формы не должна ломать ленту: остаётся текст сервера.
  if (roll === null) return <p className="feed-line">{item.text}</p>;
  return (
    <RollCardView
      actor={roll.actor}
      label={roll.label}
      natural={roll.natural}
      total={roll.total}
      advantage={roll.advantage}
      success={roll.success}
      labels={gameLabels(roll)}
      animate={animate}
    />
  );
}
