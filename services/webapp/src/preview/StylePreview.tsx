import { useEffect, useId, useState } from "react";
import type { ReactNode } from "react";
import { RollCard } from "../components/RollCard";
import { applyTheme } from "../theme/telegram";
import type { ThemeName } from "../theme/telegram";
import {
  Alert,
  Avatar,
  Badge,
  Button,
  Card,
  Countdown,
  EmptyState,
  HpBar,
  ICON_NAMES,
  Icon,
  Loading,
  Panel,
  SealedNote,
} from "../ui";
import type { IconName } from "../ui";
import { ROLL_SAMPLES } from "./rollSamples";
import "./preview.css";

// Страница для владельца: все элементы стиля в обеих темах, без входа и без сервера.
// Попадает только в dev-сборку (см. main.tsx). Карточки бросков здесь те же, что в игре.

type Mode = ThemeName | "both";

function initialMode(): Mode {
  const requested = new URLSearchParams(window.location.search).get("theme");
  if (requested === "light" || requested === "dark" || requested === "both") {
    return requested;
  }
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

const MODES: ReadonlyArray<readonly [Mode, string]> = [
  ["dark", "Тёмная"],
  ["light", "Светлая"],
  ["both", "Обе рядом"],
];

const SWATCHES: ReadonlyArray<readonly [string, string]> = [
  ["Фон стола", "--color-bg"],
  ["Шапка", "--color-header"],
  ["Пергамент", "--color-surface"],
  ["Вдавленное", "--color-surface-sunken"],
  ["Свеча", "--color-accent"],
  ["Сукно", "--color-cloth"],
  ["Сургуч", "--color-seal"],
  ["Текст", "--color-text"],
];

const ICON_CAPTIONS: Record<IconName, string> = {
  d20: "Кубик",
  candle: "Свеча",
  scroll: "Свиток",
  shield: "Щит",
  heart: "Сердце",
  hourglass: "Песочные часы",
  seal: "Печать",
};

const HEROES = ["Лира", "Борин", "Кай", "Эльза", "Тарн", "Мира"];

function PreviewSection({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  const id = useId();
  return (
    <section className="preview-section" aria-labelledby={id}>
      <h2 id={id} className="preview-section__title">
        {title}
      </h2>
      {children}
    </section>
  );
}

function PreviewBody() {
  return (
    <>
      <PreviewSection title="Цвета">
        <ul className="preview-swatches">
          {SWATCHES.map(([name, token]) => (
            <li key={token}>
              <span
                className="preview-swatch"
                style={{ background: `var(${token})` }}
              />
              {name}
            </li>
          ))}
        </ul>
      </PreviewSection>

      <PreviewSection title="Шрифты">
        <Card>
          <p className="preview-display">Подземелье забытой свечи</p>
          <p>
            Основной текст набран Alegreya: он спокойно читается с телефона
            вечером, при тусклом свете.
          </p>
          <p>
            <em>Курсив для мыслей, шёпота и тайных писем.</em>{" "}
            <strong>Полужирный — для важного.</strong>
          </p>
        </Card>
      </PreviewSection>

      <PreviewSection title="Кнопки">
        <div className="preview-row">
          <Button icon="d20">Основная</Button>
          <Button variant="secondary">Второстепенная</Button>
          <Button variant="danger">Опасная</Button>
          <Button disabled>Недоступна</Button>
          <Button busy>Занята…</Button>
        </div>
      </PreviewSection>

      <PreviewSection title="Плашки">
        <div className="preview-row">
          <Badge>Нейтральная</Badge>
          <Badge tone="amber" icon="candle">
            Ваш ход
          </Badge>
          <Badge tone="cloth" icon="shield">
            Готов
          </Badge>
          <Badge tone="seal" icon="seal">
            Тайное
          </Badge>
        </div>
      </PreviewSection>

      <PreviewSection title="Карточка и панель">
        <Card title="Склеп забытых свечей" icon="scroll" headingLevel={3}>
          <p>
            Карточка — пергамент: на ней лежит основной текст. Внутри бывают
            заголовок, иконка и кнопки.
          </p>
          <Button variant="secondary">Открыть</Button>
        </Card>
        <Panel title="Партия" icon="shield" headingLevel={3}>
          <p>Деревянная панель собирает управление: героев, кнопки, отсчёт.</p>
        </Panel>
        <Panel title="Привал" icon="candle" headingLevel={3} tone="cloth">
          <p>Зелёное сукно — для второстепенных плашек и тихих минут.</p>
        </Panel>
      </PreviewSection>

      <PreviewSection title="Рассказ мастера">
        <Card>
          <div className="ui-narration ui-narration--dropcap">
            <p>
              Дверь склепа поддаётся с тихим вздохом. Пахнет воском и старой
              пылью. Где-то в глубине коридора мерцает огонёк — будто кто-то
              оставил свечу, зная, что вы придёте.
            </p>
            <p>Что вы делаете?</p>
          </div>
        </Card>
      </PreviewSection>

      <PreviewSection title="Отсчёт">
        <Countdown
          remainingSeconds={87}
          totalSeconds={120}
          label="До конца хода"
        />
        <Countdown remainingSeconds={8} totalSeconds={120} label="Торопимся" />
        <Countdown
          remainingSeconds={242}
          totalSeconds={300}
          label="Привал"
          paused
        />
      </PreviewSection>

      <PreviewSection title="Бросок">
        {ROLL_SAMPLES.map((item) => (
          <RollCard key={item.id} item={item} />
        ))}
      </PreviewSection>

      <PreviewSection title="Только тебе">
        <SealedNote>
          Стражник заметил, как ты спрятал письмо в рукав. Остальные этого не
          видели.
        </SealedNote>
      </PreviewSection>

      <PreviewSection title="Здоровье">
        <Card>
          <div className="preview-stack">
            <HpBar current={17} max={20} label="Здоровье: Лира" />
            <HpBar current={9} max={20} temp={3} label="Здоровье: Борин" />
            <HpBar current={3} max={20} label="Здоровье: Кай" />
          </div>
        </Card>
      </PreviewSection>

      <PreviewSection title="Герои">
        <div className="preview-row">
          {HEROES.map((name) => (
            <Avatar key={name} name={name} />
          ))}
        </div>
        <div className="preview-row">
          <Avatar name="Лира Ветер" size="sm" />
          <Avatar name="Лира Ветер" size="md" />
          <Avatar name="Лира Ветер" size="lg" />
        </div>
      </PreviewSection>

      <PreviewSection title="Пусто, загрузка, ошибка">
        <Card>
          <EmptyState
            icon="scroll"
            title="Здесь пока пусто"
            action={<Button variant="secondary">Создать игру</Button>}
          >
            Когда друзья соберутся за столом, здесь появятся ваши игры.
          </EmptyState>
        </Card>
        <Card>
          <Loading label="Мастер думает…" />
        </Card>
        <Alert>
          Нет связи с сервером. Проверьте интернет и попробуйте ещё раз.
        </Alert>
      </PreviewSection>

      <PreviewSection title="Иконки">
        <ul className="preview-icons">
          {ICON_NAMES.map((name) => (
            <li key={name}>
              <Icon name={name} size={32} />
              {ICON_CAPTIONS[name]}
            </li>
          ))}
        </ul>
      </PreviewSection>
    </>
  );
}

export default function StylePreview() {
  const [mode, setMode] = useState<Mode>(initialMode);

  // «Обе рядом» оставляет страницу в последней выбранной теме.
  useEffect(() => {
    if (mode !== "both") applyTheme(mode);
  }, [mode]);

  return (
    <main
      className={mode === "both" ? "app preview preview--wide" : "app preview"}
    >
      <header className="app-header">
        <Icon name="d20" size={28} />
        <h1>Стиль «ламповые посиделки»</h1>
      </header>
      <p className="preview-note">
        Превью для оценки внешнего вида: все элементы в обеих темах, без входа и
        без сервера. Есть только в режиме разработки.
      </p>
      <div role="group" aria-label="Тема" className="preview-row">
        {MODES.map(([value, label]) => (
          <Button
            key={value}
            variant="secondary"
            className="preview-switch"
            aria-pressed={mode === value}
            onClick={() => setMode(value)}
          >
            {label}
          </Button>
        ))}
      </div>
      {mode === "both" ? (
        <div className="preview-both">
          {(["dark", "light"] as const).map((theme) => (
            <div key={theme} className="preview-scope" data-theme={theme}>
              <PreviewBody />
            </div>
          ))}
        </div>
      ) : (
        <PreviewBody />
      )}
    </main>
  );
}
