import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useApi } from "../../api/context";
import type { CharacterChoices } from "../../api/types";
import { Button, Card, Loading } from "../../ui";
import { ErrorAlert } from "../fields";
import { useAction } from "../hooks";
import "../screens.css";
import { AbilitiesStep } from "./AbilitiesStep";
import "./character.css";
import { EquipmentStep, SkillsStep, SpellsStep } from "./ChoiceSteps";
import {
  createDraft,
  draftFromChoices,
  normalize,
  stepComplete,
  stepsOf,
  toChoices,
} from "./draft";
import type { Draft, StepId } from "./draft";
import {
  BackgroundStep,
  BonusStep,
  ClassStep,
  SpeciesStep,
} from "./OriginSteps";
import type { CharacterFormProps } from "./shared";
import { STEP_TITLES } from "./steps";
import type { StepProps } from "./steps";
import { BackstoryStep, PersonaStep } from "./StorySteps";
import { useBackstoryHelp } from "./useBackstoryHelp";

/**
 * Подробное создание героя по шагам. Клиент только подсказывает (что ещё не выбрано,
 * сколько осталось очков); выбор целиком уходит на сервер, и его проверка — окончательная:
 * отказ правил (422) показывается списком проблем, выбор при этом не теряется.
 * «Изменить героя» открывает форму с прежним выбором (`initial`), иначе она пустая.
 */
export function Detailed({
  gameId,
  options,
  initial,
  onBack,
  onCreated,
}: CharacterFormProps & { initial?: CharacterChoices }) {
  const api = useApi();
  const [draft, setDraft] = useState<Draft>(() =>
    initial === undefined
      ? createDraft(options)
      : draftFromChoices(initial, options),
  );
  const [position, setPosition] = useState(0);
  const submit = useAction();

  const update = useCallback(
    (patch: Partial<Draft>) =>
      setDraft((current) => normalize({ ...current, ...patch }, options)),
    [options],
  );
  const help = useBackstoryHelp(gameId, draft, update);

  const steps = stepsOf(draft, options);
  // Шагов может стать меньше (класс без заклинаний): позиция не выходит за список.
  const index = Math.min(position, steps.length - 1);
  const step = steps[index];
  const isLast = index === steps.length - 1;
  const busy = submit.busy || help.busy;
  const choices = toChoices(draft, options);
  const canGoOn = stepComplete(step, draft, options);

  // Новый шаг показывается с начала: иначе страница остаётся прокрученной к кнопкам
  // прошлого шага. На первом показе экрана прокручивать нечего.
  const progressRef = useRef<HTMLDivElement>(null);
  const shownIndex = useRef(index);
  useEffect(() => {
    if (shownIndex.current === index) return;
    shownIndex.current = index;
    progressRef.current?.scrollIntoView?.({ block: "start" });
  }, [index]);

  const create = async () => {
    if (choices === null || busy) return;
    const created = await submit.run(() =>
      api.createCharacter(gameId, { mode: "detailed", choices }),
    );
    if (created) onCreated();
  };

  const props: StepProps = { draft, options, update };
  const body: Record<StepId, () => ReactNode> = {
    species: () => <SpeciesStep {...props} />,
    class: () => <ClassStep {...props} />,
    background: () => <BackgroundStep {...props} />,
    abilities: () => <AbilitiesStep {...props} gameId={gameId} />,
    bonus: () => <BonusStep {...props} />,
    skills: () => <SkillsStep {...props} />,
    spells: () => <SpellsStep {...props} />,
    equipment: () => <EquipmentStep {...props} />,
    persona: () => <PersonaStep {...props} />,
    backstory: () => (
      <BackstoryStep draft={draft} update={update} help={help} />
    ),
  };

  return (
    <Card title="Подробный герой" icon="scroll">
      <div className="screen-stack">
        <div className="char-progress" ref={progressRef}>
          <p className="char-progress__text">
            {`Шаг ${index + 1} из ${steps.length}: ${STEP_TITLES[step]}`}
          </p>
          <div className="char-progress__track" aria-hidden="true">
            <div
              className="char-progress__fill"
              style={{ width: `${((index + 1) / steps.length) * 100}%` }}
            />
          </div>
        </div>
        {body[step]()}
        {submit.busy && <Loading label="Создаём героя…" />}
        <ErrorAlert error={submit.error} problems={submit.problems} />
        {!isLast && !canGoOn && (
          <p className="char-note">
            Чтобы идти дальше, закончите выбор на этом шаге.
          </p>
        )}
        <div className="screen-actions">
          <Button
            variant="secondary"
            disabled={busy}
            onClick={index === 0 ? onBack : () => setPosition(index - 1)}
          >
            Назад
          </Button>
          {isLast ? (
            <Button
              busy={submit.busy}
              disabled={choices === null || help.busy}
              onClick={() => void create()}
            >
              Создать героя
            </Button>
          ) : (
            <Button disabled={!canGoOn} onClick={() => setPosition(index + 1)}>
              Далее
            </Button>
          )}
        </div>
      </div>
    </Card>
  );
}
