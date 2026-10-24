import { useState } from "react";
import { ApiError } from "../../api/client";
import { useApi } from "../../api/context";
import type { CreatedCharacter, RandomPreview } from "../../api/types";
import { Button, Card, Loading } from "../../ui";
import { ErrorAlert } from "../fields";
import { useAction } from "../hooks";
import "../screens.css";
import { HeroSheet } from "./HeroSheet";
import { CreatedNotice } from "./shared";
import type { CharacterFormProps } from "./shared";

type Pending = "roll" | "take";

const PENDING_TEXT: Record<Pending, string> = {
  roll: "Бросаем кубики…",
  take: "Мастер придумывает имя и описание… Это может занять до минуты.",
};

/**
 * Случайный герой с предпросмотром. Бросает только сервер: «Ещё раз» просит нового
 * героя, «Беру» создаёт показанного по его `preview_id`. Пока идёт любой из запросов,
 * обе кнопки заблокированы: ответы не обгонят друг друга, и у клиента не останется
 * `preview_id`, который сервер уже заменил.
 */
export function Random({
  gameId,
  onBack,
  onCreated,
  options,
}: CharacterFormProps) {
  const api = useApi();
  const { busy, error, problems, run } = useAction();
  const [preview, setPreview] = useState<RandomPreview | null>(null);
  // Сервер ответил 409: показанного героя больше нельзя взять, нужен новый бросок.
  const [stale, setStale] = useState(false);
  const [pending, setPending] = useState<Pending>("roll");
  const [created, setCreated] = useState<CreatedCharacter | null>(null);

  const roll = () => {
    setPending("roll");
    void run(async () => {
      setPreview(await api.randomPreview(gameId));
      setStale(false);
    });
  };

  const take = () => {
    if (preview === null) return;
    setPending("take");
    void run(async () => {
      try {
        const result = await api.createCharacter(gameId, {
          mode: "random",
          preview_id: preview.preview_id,
        });
        if (result.flavor_error === null) onCreated();
        else setCreated(result);
      } catch (failure) {
        if (failure instanceof ApiError && failure.status === 409) {
          setStale(true);
        }
        throw failure;
      }
    });
  };

  if (created !== null) {
    return <CreatedNotice created={created} onDone={onCreated} />;
  }

  return (
    <Card title="Случайный герой" icon="d20">
      <div className="screen-stack">
        {preview === null ? (
          <p>
            Судьба бросит кубики: раса, класс, характеристики и снаряжение
            выпадут сами. Имя и описание придумает мастер.
          </p>
        ) : (
          <HeroSheet character={preview.character} options={options} />
        )}
        {busy && <Loading label={PENDING_TEXT[pending]} />}
        <ErrorAlert error={error} problems={problems} />
        <div className="screen-actions">
          {preview === null ? (
            <Button busy={busy} onClick={roll}>
              Бросить судьбу
            </Button>
          ) : (
            <>
              <Button
                variant={stale ? "primary" : "secondary"}
                disabled={busy}
                onClick={roll}
              >
                Ещё раз
              </Button>
              <Button
                variant={stale ? "secondary" : "primary"}
                disabled={busy || stale}
                onClick={take}
              >
                Беру
              </Button>
            </>
          )}
          <Button variant="secondary" disabled={busy} onClick={onBack}>
            Назад
          </Button>
        </div>
      </div>
    </Card>
  );
}
