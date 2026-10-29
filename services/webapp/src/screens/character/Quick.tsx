import { useState } from "react";
import type { FormEvent } from "react";
import { useApi } from "../../api/context";
import type {
  CreatedCharacter,
  CreateCharacterBody,
  QuickChoices,
} from "../../api/types";
import { Button, Card, Loading } from "../../ui";
import { ErrorAlert, TextField } from "../fields";
import { useAction } from "../hooks";
import "../screens.css";
import { LIMITS } from "./draft";
import { ClassBriefCarousel, RaceCarousel } from "./OptionCards";
import { CreatedNotice } from "./shared";
import type { CharacterFormProps } from "./shared";

/** Вариант с этим id есть в правилах сервера. */
const known = (list: readonly { id: string }[], id: string) =>
  list.some((option) => option.id === id);

/**
 * Быстрый герой: класс, раса и строка о герое; имя и описание пишет мастер. Класс и раса
 * выбираются листанием карточек; карточка класса короткая (подробности — по «Подробнее»),
 * чтобы на телефоне раса и поле героя не уходили далеко вниз. «Изменить героя» открывает форму с прежним выбором
 * (`initial`), иначе она пустая.
 */
export function Quick({
  gameId,
  options,
  initial,
  onBack,
  onCreated,
}: CharacterFormProps & { initial?: QuickChoices }) {
  const api = useApi();
  const { busy, error, problems, run } = useAction();
  const [classId, setClassId] = useState(initial?.class_id ?? "");
  const [species, setSpecies] = useState(initial?.species ?? "");
  const [concept, setConcept] = useState(initial?.concept ?? "");
  const [name, setName] = useState(initial?.name ?? "");
  const [created, setCreated] = useState<CreatedCharacter | null>(null);

  // Прежний выбор («Изменить героя») мог исчезнуть из правил: такой вариант не показан выбранным,
  // и форма ждёт видимого выбора, а не уходит на сервер с невидимым id.
  const shownClass = known(options.classes, classId) ? classId : null;
  const shownSpecies = known(options.species, species) ? species : null;
  const ready =
    shownClass !== null && shownSpecies !== null && concept.trim() !== "";

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!ready || busy) return;
    const given = name.trim();
    const body: CreateCharacterBody = {
      mode: "quick",
      class_id: classId,
      species,
      concept: concept.trim(),
      ...(given === "" ? {} : { name: given }),
    };
    await run(async () => {
      const result = await api.createCharacter(gameId, body);
      if (result.flavor_error === null) onCreated();
      else setCreated(result);
    });
  };

  if (created !== null) {
    return <CreatedNotice created={created} onDone={onCreated} />;
  }

  return (
    <Card title="Быстрый герой" icon="candle">
      <form className="screen-stack" onSubmit={(event) => void submit(event)}>
        {/* Карусели две, поэтому у каждой видимая подпись. */}
        <div>
          <p className="form-legend">Класс</p>
          <ClassBriefCarousel
            options={options}
            value={shownClass}
            onChange={setClassId}
          />
        </div>
        <div>
          <p className="form-legend">Раса</p>
          <RaceCarousel
            options={options}
            value={shownSpecies}
            onChange={setSpecies}
          />
        </div>
        <TextField
          label="Опиши героя одной строкой"
          hint="Например: сбежавший из цирка акробат, который боится темноты."
          value={concept}
          maxLength={LIMITS.concept}
          autoComplete="off"
          onChange={(event) => setConcept(event.target.value)}
        />
        <TextField
          label="Имя (необязательно)"
          hint="Не напишете — имя придумает мастер."
          value={name}
          maxLength={LIMITS.name}
          autoComplete="off"
          onChange={(event) => setName(event.target.value)}
        />
        {busy && (
          <Loading label="Мастер придумывает героя… Это может занять до минуты." />
        )}
        <ErrorAlert error={error} problems={problems} />
        <div className="screen-actions">
          <Button type="submit" busy={busy} disabled={!ready}>
            Создать героя
          </Button>
          <Button variant="secondary" disabled={busy} onClick={onBack}>
            Назад
          </Button>
        </div>
      </form>
    </Card>
  );
}
