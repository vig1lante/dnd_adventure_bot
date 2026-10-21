import type { CombatView, Fighter, MonsterFighter } from "../api/types";
import { useMotionClass } from "../theme/motion";
import { Avatar, Badge, HpBar } from "../ui";
import type { BadgeProps } from "../ui";
import { classNames } from "../ui/classNames";
import { cannotAct } from "./access";
import { effectLabel } from "./format";
import "./game.css";

// Цвет плашки следует слову сервера о монстре; чисел здоровья монстров у клиента нет.
const HEALTH_TONES: Record<MonsterFighter["health"], BadgeProps["tone"]> = {
  цел: "cloth",
  ранен: "amber",
  "тяжело ранен": "seal",
  повержен: "neutral",
};

/** Здоровье бойца так, как его отдаёт сервер: у героя шкала, у монстра слово. */
function Health({ fighter }: { fighter: Fighter }) {
  if (fighter.kind === "monster") {
    return (
      <Badge tone={HEALTH_TONES[fighter.health] ?? "neutral"}>
        {fighter.health}
      </Badge>
    );
  }
  if (fighter.hp === null || fighter.hp_max === null) return null;
  return (
    <HpBar
      current={fighter.hp}
      max={fighter.hp_max}
      label={`Здоровье: ${fighter.name}`}
    />
  );
}

/** Метка «Ходит» появляется мягко; при reduced motion движения нет, метка остаётся. */
function TurnMark() {
  const appear = useMotionClass("ui-anim-appear");
  return (
    <span className={classNames("initiative__mark", appear)}>
      <Badge tone="amber" icon="d20">
        Ходит
      </Badge>
    </span>
  );
}

export interface InitiativeListProps {
  combat: CombatView;
}

/**
 * Порядок хода в бою: бойцы по инициативе, как прислал сервер; текущий выделен рамкой и меткой
 * «Ходит». Имена берутся из `name` (монстр с NPC назван по NPC), здоровье героев — шкала с
 * числами сервера, монстров — слово («ранен»). Героя, который не может действовать (`can_act`
 * от сервера), помечает «Не может действовать». Спрятанных монстров в данных нет, и клиент их не
 * достраивает: если ходит один из них, текущего нет.
 */
export function InitiativeList({ combat }: InitiativeListProps) {
  return (
    <ol className="initiative" aria-label="Порядок хода">
      {combat.order.map((fighter) => {
        const current = fighter.id === combat.current_id;
        return (
          <li
            key={fighter.id}
            aria-current={current ? "true" : undefined}
            className={classNames(
              "initiative__item",
              current && "initiative__item--current",
            )}
          >
            <div className="initiative__head">
              <Avatar name={fighter.name} size="sm" decorative />
              <span className="initiative__name">{fighter.name}</span>
              {current && <TurnMark />}
              {fighter.side === "enemies" && <Badge tone="seal">Враг</Badge>}
              {cannotAct(fighter) && (
                <Badge tone="seal">Не может действовать</Badge>
              )}
            </div>
            <Health fighter={fighter} />
            {fighter.conditions.length > 0 && (
              <p className="initiative__effects">
                {fighter.conditions.map((condition) => (
                  <Badge key={condition.id} tone="seal">
                    {effectLabel(condition)}
                  </Badge>
                ))}
              </p>
            )}
          </li>
        );
      })}
    </ol>
  );
}
