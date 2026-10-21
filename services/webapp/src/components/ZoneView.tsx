import type { CombatView } from "../api/types";
import { Badge } from "../ui";
import "./game.css";

export interface ZoneViewProps {
  combat: CombatView;
}

/**
 * Зоны боя списком: название и бойцы, стоящие в ней (`zone_id` бойца из порядка хода). Кого в
 * порядке хода нет — например, спрятанного монстра — в зонах нет тоже, и клиент его не
 * называет. Пустая зона говорит «Никого не видно», а не «пусто»: в ней может ждать засада.
 */
export function ZoneView({ combat }: ZoneViewProps) {
  if (combat.zones.length === 0) {
    return <p className="zone__empty">Поле боя пока не описано</p>;
  }
  return (
    <ul className="zones" aria-label="Зоны боя">
      {combat.zones.map((zone) => {
        const here = combat.order.filter(
          (fighter) => fighter.zone_id === zone.id,
        );
        return (
          <li key={zone.id} className="zone">
            <h3 className="zone__name">{zone.name}</h3>
            {here.length === 0 ? (
              <p className="zone__empty">Никого не видно</p>
            ) : (
              <ul className="zone__fighters">
                {here.map((fighter) => (
                  <li key={fighter.id} className="zone__fighter">
                    <span className="zone__fighter-name">{fighter.name}</span>
                    {fighter.side === "enemies" && (
                      <Badge tone="seal">Враг</Badge>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </li>
        );
      })}
    </ul>
  );
}
