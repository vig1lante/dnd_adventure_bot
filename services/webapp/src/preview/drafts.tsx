import { useEffect, useId, useState } from "react";
import type { ReactNode } from "react";
import type { FeedItem, SceneView, TurnPhase } from "../api/types";
import { Countdown } from "../components/Countdown";
import { phaseLabel } from "../components/phases";
import { RollCard } from "../components/RollCard";
import { Switch, TextAreaField } from "../screens/fields";
import type { GameView } from "../store/game";
import { Avatar, Badge, Button, Card, Icon, Panel, SealedNote } from "../ui";
import type { IconName } from "../ui";
import { classNames } from "../ui/classNames";

// Черновики элементов «как будет» для макетов B10.1. Это предложение для обсуждения с владельцем,
// а не боевые компоненты: их по итогам гейта делают B10.2+ (ui/TabBar, Skeleton, ErrorState,
// Toast, Settings). Всё собрано из токенов темы и элементов src/ui.

// --- нижняя панель вкладок ---

export type GameTab = "story" | "fight" | "hero" | "party" | "journal";

/** А — нынешний порядок (решение B8), В — рекомендация разбора: бой и привал сразу за рассказом. */
export type TabOrder = "A" | "B";

const ORDERS: Record<TabOrder, readonly GameTab[]> = {
  A: ["story", "journal", "fight", "hero", "party"],
  B: ["story", "fight", "hero", "party", "journal"],
};

const TAB_LABELS: Record<Exclude<GameTab, "fight">, string> = {
  story: "Рассказ",
  journal: "Дневник",
  hero: "Персонаж",
  party: "Отряд",
};

const TAB_ICONS: Record<Exclude<GameTab, "fight">, IconName> = {
  story: "scroll",
  journal: "seal",
  hero: "heart",
  party: "shield",
};

interface TabBarProps {
  order: TabOrder;
  /** Вкладка боя или привала по фазе; `null` — её нет. */
  fight: "combat" | "camp" | null;
  active: GameTab;
  /** Бейджи новых событий: только число или точка, без текста тайных ходов и писем. */
  badges?: Partial<Record<GameTab, string>>;
  onSelect: (tab: GameTab) => void;
}

/**
 * Нижняя панель вкладок у большого пальца. Имена вкладок — ровно слова («Бой», «Дневник»):
 * на них опираются сквозные тесты, поэтому бейдж стоит вне доступного имени.
 */
export function DraftTabBar({
  order,
  fight,
  active,
  badges = {},
  onSelect,
}: TabBarProps) {
  const tabs = ORDERS[order].filter((tab) => tab !== "fight" || fight !== null);
  return (
    <div className="preview-tabbar">
      <div
        role="tablist"
        aria-label="Разделы игры"
        className="preview-tabbar__list"
        style={{ gridTemplateColumns: `repeat(${tabs.length}, 1fr)` }}
      >
        {tabs.map((tab) => {
          const label =
            tab === "fight"
              ? fight === "combat"
                ? "Бой"
                : "Привал"
              : TAB_LABELS[tab];
          const icon =
            tab === "fight"
              ? fight === "combat"
                ? "d20"
                : "candle"
              : TAB_ICONS[tab];
          return (
            <button
              key={tab}
              type="button"
              role="tab"
              aria-selected={tab === active}
              tabIndex={tab === active ? 0 : -1}
              className={classNames(
                "preview-tab",
                tab === active && "preview-tab--active",
              )}
              onClick={() => onSelect(tab)}
            >
              <Icon name={icon} size={22} />
              <span className="preview-tab__label">{label}</span>
              {badges[tab] !== undefined && (
                <span className="preview-tab__badge" aria-hidden="true">
                  {badges[tab]}
                </span>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}

// --- строка статуса ---

const PARTS_OF_DAY: Record<SceneView["part"], string> = {
  morning: "утро",
  day: "полдень",
  evening: "вечер",
  night: "ночь",
};

/** «День 2, утро» из сцены снимка; без сцены — ничего. */
export function dayPartOf(scene: SceneView | null): string | null {
  return scene === null
    ? null
    : `День ${scene.day}, ${PARTS_OF_DAY[scene.part]}`;
}

const COUNTDOWN_LABELS: Partial<Record<TurnPhase, string>> = {
  collecting: "До конца сбора",
  combat_wait: "Время на ход",
  camp: "До конца привала",
};

const STATUS_BADGES = {
  writing: { label: "Пишет", tone: "amber" },
  ready: { label: "Готов", tone: "cloth" },
  pass: { label: "Пас", tone: "neutral" },
} as const;

/** Строка статуса: фаза, день и часть суток, отсчёт (тот же, что сейчас) и значки игроков. */
export function DraftStatusLine({
  view,
  clockOffsetMs,
}: {
  view: GameView;
  clockOffsetMs: number;
}) {
  const { phase, game, statuses } = view;
  const day = dayPartOf(phase.scene);
  const countdownLabel =
    phase.turn_phase === null ? undefined : COUNTDOWN_LABELS[phase.turn_phase];
  const total =
    phase.turn_phase === "camp"
      ? game.camp_seconds
      : phase.turn_phase === "combat_wait"
        ? game.hurry_seconds
        : game.collect_seconds;
  const showPlayers =
    phase.turn_phase === "discussion" || phase.turn_phase === "collecting";
  return (
    <>
      <Panel className="game-status">
        <div className="preview-status__row">
          <p className="game-status__phase">
            <Icon name="hourglass" size={22} />
            <span>{phaseLabel(phase)}</span>
          </p>
          {day !== null && <span className="preview-status__day">{day}</span>}
        </div>
        <Countdown
          phase={phase}
          clockOffsetMs={clockOffsetMs}
          totalSeconds={total}
          label={countdownLabel}
        />
      </Panel>
      {showPlayers && (
        <ul className="game-players" aria-label="Игроки">
          {game.players.map((player) => {
            const status = statuses[player.user_id];
            return (
              <li key={player.user_id} className="game-players__item">
                <Avatar name={player.name} size="sm" decorative />
                <span className="game-players__name">{player.name}</span>
                {status !== undefined && (
                  <Badge tone={STATUS_BADGES[status].tone}>
                    {STATUS_BADGES[status].label}
                  </Badge>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}

// --- загрузка, пусто, ошибка ---

/** Скелетон списка: строки той же высоты, что настоящие, — экран не прыгает, когда данные пришли. */
export function DraftSkeleton({
  rows = 3,
  label,
}: {
  rows?: number;
  label: string;
}) {
  return (
    <div role="status" aria-label={label} className="preview-skeleton">
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="preview-skeleton__row" aria-hidden="true">
          <span className="preview-skeleton__line preview-skeleton__line--wide" />
          <span className="preview-skeleton__line" />
          <span className="preview-skeleton__button" />
        </div>
      ))}
    </div>
  );
}

interface ErrorStateProps {
  title: string;
  children: ReactNode;
  onRetry?: () => void;
  /** Второе действие (например, «К моим играм»). */
  extra?: ReactNode;
}

/** Ошибка одного вида на всех экранах: что случилось, что делать и «Повторить». */
export function DraftErrorState({
  title,
  children,
  onRetry,
  extra,
}: ErrorStateProps) {
  return (
    <div className="preview-error">
      <div role="alert" className="preview-error__body">
        <svg
          className="preview-error__icon"
          width={40}
          height={40}
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.6}
          strokeLinecap="round"
          aria-hidden="true"
          focusable="false"
        >
          <circle cx="12" cy="12" r="9" />
          <path d="M12 7.5v5.5M12 16.5v.01" />
        </svg>
        <p className="preview-error__title">{title}</p>
        <div className="preview-error__text">{children}</div>
      </div>
      <div className="screen-actions">
        <Button icon="d20" onClick={onRetry}>
          Повторить
        </Button>
        {extra}
      </div>
    </div>
  );
}

// --- тост ---

/** Уведомление о событии над нижней панелью. Только факт: текст тайных ходов сюда не попадает. */
export function DraftToast({
  title,
  children,
  action,
}: {
  title: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  const titleId = useId();
  return (
    <div role="status" aria-labelledby={titleId} className="preview-toast">
      <Icon name="d20" size={28} className="preview-toast__icon" />
      <div className="preview-toast__text">
        <p id={titleId} className="preview-toast__title">
          {title}
        </p>
        <p>{children}</p>
      </div>
      {action}
    </div>
  );
}

// --- «Мастер думает…» ---

/**
 * Нейтральные фразы ожидания: говорят о мастере, а не о событиях игры, — ничего не обещают
 * и не раскрывают.
 */
export const THINKING_PHRASES = [
  "Листает свои заметки…",
  "Перечитывает ваши ходы…",
  "Сверяется с картой…",
  "Подбирает слова…",
  "Точит перо…",
] as const;

const PHRASE_SECONDS = 8;
// После минуты хосту предлагают «Повторить» (как сейчас: RETRY_PATIENCE_MS).
const PATIENCE_SECONDS = 60;

const clock = (seconds: number) =>
  `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;

/** Секунды с показа, начиная со `start`; тикают раз в секунду. */
function useElapsed(start: number): number {
  const [startedAt] = useState(() => Date.now());
  const [now, setNow] = useState(startedAt);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  return start + Math.floor((now - startedAt) / 1000);
}

/**
 * Одна формулировка «Мастер думает…» для лобби и игры: свеча, сменяющаяся фраза и сколько уже
 * ждём. Через минуту хост видит «Повторить». Время считает браузер с показа, как сейчас.
 */
export function DraftThinking({
  startSeconds = 0,
  isHost = false,
}: {
  startSeconds?: number;
  isHost?: boolean;
}) {
  const titleId = useId();
  const elapsed = useElapsed(startSeconds);
  const phrase =
    THINKING_PHRASES[
      Math.floor(elapsed / PHRASE_SECONDS) % THINKING_PHRASES.length
    ];
  return (
    <section
      role="status"
      aria-labelledby={titleId}
      className="ui-card preview-thinking"
    >
      <Icon
        name="candle"
        size={44}
        className="preview-thinking__candle ui-anim-flicker"
      />
      <div>
        <h3 id={titleId}>Мастер думает…</h3>
        <p className="preview-thinking__phrase">{phrase}</p>
        {/* Счётчик не зачитывается каждую секунду: живая область объявит только фразу. */}
        <p className="preview-thinking__time" aria-hidden="true">
          Ждём {clock(elapsed)}
        </p>
        {isHost && elapsed >= PATIENCE_SECONDS && (
          <Button variant="secondary">Повторить</Button>
        )}
      </div>
    </section>
  );
}

// --- лента для чтения вслух ---

const actorOf = (item: FeedItem): string | null =>
  typeof item.data?.actor === "string" ? item.data.actor : null;

const paragraphs = (text: string) =>
  text
    .split(/\n+/)
    .filter((part) => part.trim() !== "")
    .map((part, index) => <p key={index}>{part}</p>);

/** Имя героя — ссылка на его лист (вкладка «Отряд» или «Персонаж»). */
function HeroName({ name, onOpen }: { name: string; onOpen: () => void }) {
  return (
    <button type="button" className="preview-hero-link" onClick={onOpen}>
      {name}
    </button>
  );
}

function ReadEntry({ item, onHero }: { item: FeedItem; onHero: () => void }) {
  const actor = actorOf(item);
  switch (item.kind) {
    case "narration":
      return (
        <article className="preview-read">
          <p className="preview-read__who">Мастер</p>
          <div className="ui-narration ui-narration--dropcap">
            {paragraphs(item.text)}
          </div>
        </article>
      );
    case "roll":
      return <RollCard item={item} animate={false} />;
    case "private":
      return <SealedNote>{paragraphs(item.text)}</SealedNote>;
    case "action":
    case "camp_line":
      return (
        <p className="preview-act">
          {actor !== null && <Avatar name={actor} size="sm" decorative />}
          <span>
            {actor !== null && <HeroName name={actor} onOpen={onHero} />}{" "}
            {item.kind === "camp_line" ? `«${item.text}»` : item.text}
          </span>
        </p>
      );
    default:
      return <p className="feed-system">{item.text}</p>;
  }
}

/**
 * Лента для чтения вслух: рассказ мастера — страница книги без рамки карточки, с подписью
 * «Мастер»; ходы героев — короткие строки с жетоном, имя открывает лист. Тексты — только
 * текст React, как и сейчас.
 */
export function DraftFeed({
  items,
  onHero,
}: {
  items: readonly FeedItem[];
  onHero: () => void;
}) {
  return (
    <ol className="feed preview-feed" aria-label="Лента">
      {items.map((item) => (
        <li key={item.id} className="feed-entry">
          <ReadEntry item={item} onHero={onHero} />
        </li>
      ))}
    </ol>
  );
}

// --- поле хода над клавиатурой ---

/** Компактное поле хода, прижатое к клавиатуре: текст, «Тайно» и «Готов» всегда видны. */
export function DraftComposerDock({ text }: { text: string }) {
  const [draft, setDraft] = useState(text);
  const [secret, setSecret] = useState(false);
  return (
    <div className="preview-dock">
      <TextAreaField
        label="Твой ход"
        rows={2}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
      />
      <div className="preview-dock__actions">
        <Switch label="Тайно" checked={secret} onChange={setSecret} />
        <Button>Готов</Button>
      </div>
    </div>
  );
}

const KEY_ROWS = ["йцукенгшщзх", "фывапролджэ", "ячсмитьбю"] as const;

/** Рисунок экранной клавиатуры телефона — только чтобы показать, где окажется поле хода. */
export function DraftKeyboard() {
  return (
    <div className="preview-keyboard" aria-hidden="true">
      {KEY_ROWS.map((row) => (
        <div key={row} className="preview-keyboard__row">
          {[...row].map((key) => (
            <span key={key} className="preview-keyboard__key">
              {key}
            </span>
          ))}
        </div>
      ))}
      <div className="preview-keyboard__row">
        <span className="preview-keyboard__key preview-keyboard__key--space">
          пробел
        </span>
      </div>
    </div>
  );
}

// --- настройки ---

export type TextSize = "normal" | "large" | "huge";

const TEXT_SIZES: readonly (readonly [TextSize, string])[] = [
  ["normal", "Обычный"],
  ["large", "Крупный"],
  ["huge", "Очень крупный"],
];

/** Размер текста и контраст ставятся атрибутами на корень страницы: они меняют токены темы. */
export function applyTextSize(size: TextSize): void {
  if (size === "normal") delete document.documentElement.dataset.textSize;
  else document.documentElement.dataset.textSize = size;
}

function applyContrast(high: boolean): void {
  if (high) document.documentElement.dataset.contrast = "high";
  else delete document.documentElement.dataset.contrast;
}

/**
 * Настройки интерфейса: размер текста, высокий контраст, вибрация. Хранятся только на этом
 * телефоне. «Меньше движения» берётся из системы, звука в приложении нет — переключателей
 * для них нет.
 */
export function DraftSettings() {
  const labelId = useId();
  const [size, setSize] = useState<TextSize>("normal");
  const [contrast, setContrast] = useState(false);
  const [vibration, setVibration] = useState(true);

  const pickSize = (next: TextSize) => {
    setSize(next);
    applyTextSize(next);
  };

  return (
    <Card title="Настройки" icon="candle">
      <div className="screen-stack">
        <div>
          <p id={labelId} className="form-legend">
            Размер текста
          </p>
          <div
            role="radiogroup"
            aria-labelledby={labelId}
            className="preview-segmented"
          >
            {TEXT_SIZES.map(([value, label]) => (
              <button
                key={value}
                type="button"
                role="radio"
                aria-checked={size === value}
                className={classNames(
                  "preview-segmented__option",
                  size === value && "preview-segmented__option--on",
                )}
                onClick={() => pickSize(value)}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
        <div className="preview-sample ui-narration">
          <p>Так будет выглядеть рассказ мастера: ветер воет в трубе маяка.</p>
        </div>
        <Switch
          label="Высокий контраст"
          hint="Текст ярче, рамки и кнопки чётче. Цвета темы остаются прежними."
          checked={contrast}
          onChange={(on) => {
            setContrast(on);
            applyContrast(on);
          }}
        />
        <Switch
          label="Вибрация"
          hint="Коротко, когда твой ход, когда хост собирает ходы и на привале. Только в Telegram."
          checked={vibration}
          onChange={setVibration}
        />
        <p className="screen-muted">
          Меньше движения — по настройке телефона. Настройки хранятся только на
          этом телефоне.
        </p>
      </div>
    </Card>
  );
}
