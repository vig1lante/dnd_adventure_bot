import { useState } from "react";
import type { SettingsSection } from "../../api/types";
import { CheckList, Switch, TextAreaField, TextField } from "../fields";
import { SectionForm, sameData } from "./fields";
import type { StepFormProps } from "./fields";
import { LockToggle } from "./LockToggle";

// Модули правил движка (README «Модули правил»). Список только подписывает флажки: допустимые
// модули проверяет сервер; незнакомый клиенту модуль черновика остаётся в списке как есть.
const KNOWN_MODULES = [
  { value: "bg3_flavor", label: "Специи BG3" },
  { value: "campfire", label: "Привал у костра" },
];

const NUMBER_FIELDS = [
  {
    key: "target_hours",
    label: "Длина игры, часов",
    hint: "Сколько часов длится игра, например 3,5.",
    integer: false,
  },
  {
    key: "party_size",
    label: "Игроков",
    hint: "От 1 до 6.",
    integer: true,
  },
  {
    key: "party_level",
    label: "Уровень героев",
    hint: "Уровень героев в начале игры.",
    integer: true,
  },
] as const;

type NumberKey = (typeof NUMBER_FIELDS)[number]["key"];

/** Число из поля ввода (запятая или точка); не число — `null`. Пределы проверяет сервер. */
function numberFrom(text: string, integer: boolean): number | null {
  const trimmed = text.trim();
  const value = Number(trimmed.replace(",", "."));
  if (trimmed === "" || !Number.isFinite(value)) return null;
  return integer && !Number.isInteger(value) ? null : value;
}

const numberText = (value: number) => String(value).replace(".", ",");

/** Шаг «Замысел»: раздел `settings` и поля черновика `title`, `journal_intro`. */
export function SettingsStep({
  draft,
  onSave,
  onLock,
  generate,
}: StepFormProps) {
  const [form, setForm] = useState({
    title: draft.title,
    journal_intro: draft.journal_intro,
    pitch: draft.settings.pitch,
    tone: draft.settings.tone,
    modules: draft.settings.modules,
    profanity: draft.settings.profanity,
  });
  const [numbers, setNumbers] = useState<Record<NumberKey, string>>({
    target_hours: numberText(draft.settings.target_hours),
    party_size: numberText(draft.settings.party_size),
    party_level: numberText(draft.settings.party_level),
  });

  const parsed = Object.fromEntries(
    NUMBER_FIELDS.map(({ key, integer }) => [
      key,
      numberFrom(numbers[key], integer),
    ]),
  ) as Record<NumberKey, number | null>;
  const { target_hours, party_size, party_level } = parsed;
  // Раздел целиком; `null`, пока в числовых полях не числа.
  const data: SettingsSection | null =
    target_hours === null || party_size === null || party_level === null
      ? null
      : { ...form, target_hours, party_size, party_level };
  const saved: SettingsSection = {
    title: draft.title,
    journal_intro: draft.journal_intro,
    ...draft.settings,
  };
  const dirty = data === null || !sameData(data, saved);

  const known = new Set(KNOWN_MODULES.map((module) => module.value));
  const moduleChoices = [
    ...KNOWN_MODULES,
    ...form.modules
      .filter((module) => !known.has(module))
      .map((module) => ({ value: module, label: module })),
  ];

  return (
    <SectionForm
      onSubmit={() => {
        if (data !== null) onSave(data);
      }}
      canSave={data !== null}
      dirty={dirty}
      generate={generate(dirty)}
      lock={
        <LockToggle
          lockRef="step:concept"
          locks={draft.locks}
          onLock={onLock}
          label="Закрепить раздел"
          disabled={dirty}
        />
      }
    >
      <p className="screen-muted">
        {draft.adventure_id === null
          ? "id приключения появится после генерации замысла."
          : `id приключения: ${draft.adventure_id}`}
      </p>
      <TextField
        label="Название"
        value={form.title}
        onChange={(event) => setForm({ ...form, title: event.target.value })}
      />
      <TextAreaField
        label="Завязка"
        hint="С чего начинается история и что нужно отряду."
        value={form.pitch}
        onChange={(event) => setForm({ ...form, pitch: event.target.value })}
      />
      <TextField
        label="Тон"
        hint="Например: мрачный, весёлый, загадочный."
        value={form.tone}
        onChange={(event) => setForm({ ...form, tone: event.target.value })}
      />
      {NUMBER_FIELDS.map(({ key, label, hint, integer }) => (
        <TextField
          key={key}
          label={label}
          hint={hint}
          inputMode={integer ? "numeric" : "decimal"}
          autoComplete="off"
          value={numbers[key]}
          error={
            parsed[key] === null
              ? integer
                ? "Нужно целое число."
                : "Нужно число."
              : null
          }
          onChange={(event) =>
            setNumbers({ ...numbers, [key]: event.target.value })
          }
        />
      ))}
      <CheckList
        legend="Модули правил"
        name="modules"
        choices={moduleChoices}
        values={form.modules}
        onChange={(modules) => setForm({ ...form, modules })}
      />
      <Switch
        label="Мат и грубая речь"
        hint="Для генерации: персонажи могут материться, но не в адрес игроков."
        checked={form.profanity}
        onChange={(profanity) => setForm({ ...form, profanity })}
      />
      <TextAreaField
        label="Главное для дневника"
        hint="Завязка и цель отряда для начала дневника. Пусто — возьмётся завязка."
        value={form.journal_intro}
        onChange={(event) =>
          setForm({ ...form, journal_intro: event.target.value })
        }
      />
    </SectionForm>
  );
}
