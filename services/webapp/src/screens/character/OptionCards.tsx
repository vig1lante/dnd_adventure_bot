import { useState } from "react";
import type { ReactNode } from "react";
import type {
  BackgroundOption,
  ClassOption,
  CreationOptions,
  Described,
  SkillOption,
  SpeciesOption,
  SpellOption,
} from "../../api/types";
import { Button, Carousel, DetailCard } from "../../ui";
import type { CarouselItem, DetailFact } from "../../ui";
import "./character.css";
import { nameIn, namesIn } from "./names";

// Подробные карточки вариантов создания героя из `details` сервера и карусели выбора.
// Только показ: тексты, подписи и расстояния в метрах приходят с сервера как есть.

/** Названия через запятую — для строки карточки. */
const namesLine = (...args: Parameters<typeof namesIn>) =>
  namesIn(...args).join(", ");

const itemLines = (items: BackgroundOption["equipment"]) =>
  items.map((item) => `${item.name} × ${item.qty}`).join(", ");

interface CardProps {
  /** Действия карточки: «Выбрать». */
  actions?: ReactNode;
}

export function SpeciesCard({
  species,
  actions,
}: CardProps & { species: SpeciesOption }) {
  const { details } = species;
  return (
    <DetailCard
      title={species.name}
      subtitle={details.name_en}
      description={details.description}
      facts={[
        { label: "Размер", value: details.size },
        { label: "Скорость", value: details.speed_text },
      ]}
      sections={[{ title: "Черты", entries: details.traits }]}
      actions={actions}
    />
  );
}

function magicLine(
  casting: ClassOption["details"]["spellcasting"],
  options: CreationOptions,
): string {
  if (casting === null) return "Нет";
  return [
    nameIn(options.abilities, casting.ability),
    casting.cantrips > 0 ? `заговоров: ${casting.cantrips}` : null,
    casting.prepared > 0 ? `заклинаний: ${casting.prepared}` : null,
  ]
    .filter((part) => part !== null)
    .join("; ");
}

/** Факты карточки класса по порядку: от кости хитов до магии. */
function classFacts(cls: ClassOption, options: CreationOptions): DetailFact[] {
  const { details } = cls;
  return [
    { label: "Кость хитов", value: `к${details.hit_die}` },
    {
      label: "Основная характеристика",
      value: nameIn(options.abilities, details.primary),
    },
    {
      label: "Спасброски",
      value: namesLine(options.abilities, details.saves),
    },
    { label: "Доспехи", value: details.armor.join(", ") || "Нет" },
    { label: "Оружие", value: details.weapons.join(", ") || "Нет" },
    {
      label: "Навыки",
      value: `${details.skill_choices.count} на выбор: ${namesLine(options.skills, details.skill_choices.from)}`,
    },
    { label: "Магия", value: magicLine(details.spellcasting, options) },
  ];
}

const classSections = (cls: ClassOption) => [
  { title: "Особенности", entries: cls.details.features },
];

export function ClassCard({
  cls,
  options,
  actions,
}: CardProps & { cls: ClassOption; options: CreationOptions }) {
  return (
    <DetailCard
      title={cls.name}
      facts={classFacts(cls, options)}
      sections={classSections(cls)}
      actions={actions}
    />
  );
}

/** Что видно на короткой карточке класса: с этим выбирают, остальное — по «Подробнее». */
const BRIEF_FACTS: ReadonlySet<string> = new Set([
  "Кость хитов",
  "Основная характеристика",
  "Магия",
]);

/**
 * Короткая карточка класса для быстрой формы: полная на телефоне длиной в несколько экранов,
 * и раса с полем героя уходят далеко вниз. Здесь главное и «Выбрать» рядом, а доспехи,
 * оружие, навыки и особенности раскрывает «Подробнее».
 */
function ClassBriefCard({
  cls,
  options,
  actions,
}: CardProps & { cls: ClassOption; options: CreationOptions }) {
  const [open, setOpen] = useState(false);
  const facts = classFacts(cls, options);
  return (
    <DetailCard
      title={cls.name}
      facts={open ? facts : facts.filter((fact) => BRIEF_FACTS.has(fact.label))}
      sections={open ? classSections(cls) : []}
      actions={
        <>
          {actions}
          <Button
            variant="secondary"
            className="char-more__toggle"
            aria-label={`Подробнее: ${cls.name}`}
            aria-expanded={open}
            onClick={() => setOpen(!open)}
          >
            Подробнее
          </Button>
        </>
      }
    />
  );
}

export function BackgroundCard({
  background,
  options,
  actions,
}: CardProps & { background: BackgroundOption; options: CreationOptions }) {
  const { details } = background;
  return (
    <DetailCard
      title={background.name}
      facts={[
        {
          label: "Характеристики для прибавки",
          value: namesLine(options.abilities, details.abilities),
        },
        { label: "Навыки", value: namesLine(options.skills, details.skills) },
        { label: "Инструмент", value: details.tool },
        { label: "Снаряжение", value: itemLines(details.equipment) },
      ]}
      sections={[{ title: "Черта", entries: [details.feat] }]}
      actions={actions}
    />
  );
}

export function SkillCard({
  skill,
  options,
}: {
  skill: SkillOption;
  options: CreationOptions;
}) {
  return (
    <DetailCard
      title={skill.name}
      description={skill.details.summary}
      facts={[
        {
          label: "Характеристика",
          value: nameIn(options.abilities, skill.details.ability),
        },
      ]}
    />
  );
}

export function SpellCard({ spell }: { spell: SpellOption }) {
  const { details } = spell;
  return (
    <DetailCard
      title={spell.name}
      description={details.summary}
      facts={[
        {
          label: "Уровень",
          value: spell.level === 0 ? "Заговор" : `${spell.level}-й`,
        },
        { label: "Школа", value: details.school },
        { label: "Время сотворения", value: details.casting_time },
        { label: "Дистанция", value: details.range_text },
        { label: "Длительность", value: details.duration },
      ]}
    />
  );
}

/** Черта или боевой стиль: только описание. */
export function SummaryCard({ item }: { item: Described }) {
  return <DetailCard title={item.name} description={item.details.summary} />;
}

/** Раскрываемая подробная карточка варианта списка. */
export function MoreDetails({
  name,
  children,
}: {
  /** Название варианта: входит в имя кнопки, чтобы кнопки списка различались. */
  name: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="char-more">
      <Button
        variant="secondary"
        className="char-more__toggle"
        aria-label={`Подробнее: ${name}`}
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        Подробнее
      </Button>
      {open && children}
    </div>
  );
}

/** «Выбрать» на карточке; у выбранной — «Выбрано». */
function ChooseButton({
  chosen,
  onChoose,
}: {
  chosen: boolean;
  onChoose: () => void;
}) {
  return (
    <Button
      variant={chosen ? "primary" : "secondary"}
      aria-pressed={chosen}
      onClick={onChoose}
    >
      {chosen ? "Выбрано" : "Выбрать"}
    </Button>
  );
}

interface PickProps {
  options: CreationOptions;
  /** Выбранный id; `null` — ничего не выбрано. */
  value: string | null;
  onChange: (id: string) => void;
}

/** Карусель выбора одного варианта: начинается с выбранного, выбор — кнопкой на карточке. */
function PickCarousel<T extends CarouselItem>({
  label,
  items,
  value,
  onChange,
  card,
}: Omit<PickProps, "options"> & {
  label: string;
  items: readonly T[];
  card: (item: T, choose: ReactNode) => ReactNode;
}) {
  return (
    <Carousel label={label} items={items} initialId={value}>
      {(item) =>
        card(
          item,
          <ChooseButton
            chosen={item.id === value}
            onChoose={() => onChange(item.id)}
          />,
        )
      }
    </Carousel>
  );
}

/** Листание рас: в быстром и подробном создании. */
export function RaceCarousel({ options, value, onChange }: PickProps) {
  return (
    <PickCarousel
      label="Раса"
      items={options.species}
      value={value}
      onChange={onChange}
      card={(species, choose) => (
        <SpeciesCard species={species} actions={choose} />
      )}
    />
  );
}

export function ClassCarousel({ options, value, onChange }: PickProps) {
  return (
    <PickCarousel
      label="Класс"
      items={options.classes}
      value={value}
      onChange={onChange}
      card={(cls, choose) => (
        <ClassCard cls={cls} options={options} actions={choose} />
      )}
    />
  );
}

/** Листание классов в быстрой форме: короткие карточки, чтобы раса и поле героя были рядом. */
export function ClassBriefCarousel({ options, value, onChange }: PickProps) {
  return (
    <PickCarousel
      label="Класс"
      items={options.classes}
      value={value}
      onChange={onChange}
      card={(cls, choose) => (
        // Ключ — класс: раскрытые подробности одного не переходят к соседнему.
        <ClassBriefCard
          key={cls.id}
          cls={cls}
          options={options}
          actions={choose}
        />
      )}
    />
  );
}

export function BackgroundCarousel({ options, value, onChange }: PickProps) {
  return (
    <PickCarousel
      label="Предыстория"
      items={options.backgrounds}
      value={value}
      onChange={onChange}
      card={(background, choose) => (
        <BackgroundCard
          background={background}
          options={options}
          actions={choose}
        />
      )}
    />
  );
}
