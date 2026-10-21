import type { PartyMember } from "../api/types";
import { Avatar, Badge, EmptyState, HpBar } from "../ui";
import { effectLabel } from "./format";
import "./game.css";

export interface PartyListProps {
  party: readonly PartyMember[];
  /** Чей экран: его герой помечается «(вы)». */
  userId: string;
}

/** Отряд: здоровье и состояния всех героев. Здоровье, имена и сроки — ровно то, что прислал сервер. */
export function PartyList({ party, userId }: PartyListProps) {
  if (party.length === 0) {
    return (
      <EmptyState icon="shield" title="Отряд ещё не собран">
        Герои появятся, когда игра начнётся.
      </EmptyState>
    );
  }
  return (
    <ul className="party">
      {party.map((member) => (
        <li key={member.id} className="party-member ui-card">
          <div className="party-member__head">
            <Avatar name={member.name} decorative />
            <span className="party-member__name">{member.name}</span>
            {member.player_id === userId && (
              <span className="party-member__me">(вы)</span>
            )}
          </div>
          <HpBar
            current={member.hp}
            max={member.hp_max}
            temp={member.temp_hp}
            label={`Здоровье: ${member.name}`}
          />
          {(member.conditions.length > 0 || member.effects.length > 0) && (
            <p className="party-member__effects">
              {member.conditions.map((condition) => (
                <Badge key={`c-${condition.id}`} tone="seal">
                  {effectLabel(condition)}
                </Badge>
              ))}
              {member.effects.map((effect) => (
                <Badge key={`e-${effect.id}`} tone="amber">
                  {effectLabel(effect)}
                </Badge>
              ))}
            </p>
          )}
        </li>
      ))}
    </ul>
  );
}
