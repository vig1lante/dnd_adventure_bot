import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { FeedItem } from "../api/types";
import "../screens/screens.css";
import { useMotionClass, useReducedMotion } from "../theme/motion";
import { Avatar, Badge, EmptyState, SealedNote } from "../ui";
import { classNames } from "../ui/classNames";
import "./game.css";
import { RollCard } from "./RollCard";

// Тексты мастера и игроков выводятся только как текст React: разметка в них остаётся буквами.

/** Абзацы по переводам строки: мастер пишет рассказ с пустыми строками между ними. */
function paragraphs(text: string): ReactNode[] {
  return text
    .split(/\n+/)
    .filter((part) => part.trim() !== "")
    .map((part, index) => <p key={index}>{part}</p>);
}

/** Имя автора из данных записи (ход игрока, реплика у костра), если сервер его прислал. */
function actorOf(item: FeedItem): string | null {
  const actor = item.data?.actor;
  return typeof actor === "string" ? actor : null;
}

/** id героя-автора записи (сервер присылает его с ходом и репликой), если он есть. */
function actorIdOf(item: FeedItem): string | null {
  const id = item.data?.actor_id;
  return typeof id === "string" ? id : null;
}

/**
 * Как открыть лист героя по имени в ленте: только у героев отряда (у монстров и NPC листа нет)
 * и только если экран умеет это делать.
 */
interface HeroLinks {
  heroIds: ReadonlySet<string>;
  onHero?: (characterId: string) => void;
}

const SecretMark = () => (
  <Badge tone="seal" icon="seal">
    Тайно
  </Badge>
);

/**
 * Имя героя — кнопка к его листу; имя остаётся ровно текстом записи. Двоеточие внутри кнопки:
 * у короткого имени кнопка шире слова (зона нажатия 44 px), и снаружи оно бы отстояло. Для
 * читалки оно скрыто: имя кнопки — ровно имя героя.
 */
function HeroName({ name, onOpen }: { name: string; onOpen: () => void }) {
  return (
    <button
      type="button"
      className="feed-hero"
      title="Открыть лист героя"
      onClick={onOpen}
    >
      <span className="feed-hero__name">{name}</span>
      <span aria-hidden="true">:</span>
    </button>
  );
}

interface SpokenProps {
  item: FeedItem;
  links: HeroLinks;
  quote?: boolean;
}

/** Ход героя или реплика у костра: жетон, имя (у героя отряда — кнопка к листу) и текст. */
function Spoken({ item, links, quote }: SpokenProps) {
  const actor = actorOf(item);
  const actorId = actorIdOf(item);
  const { onHero } = links;
  const linked =
    actor !== null &&
    actorId !== null &&
    onHero !== undefined &&
    links.heroIds.has(actorId);
  return (
    <p className="feed-act">
      {actor !== null && <Avatar name={actor} size="sm" decorative />}
      <span className="feed-act__text">
        {item.private && <SecretMark />}
        {actor !== null && (
          <strong>
            {linked ? (
              <HeroName name={actor} onOpen={() => onHero(actorId)} />
            ) : (
              `${actor}:`
            )}
          </strong>
        )}
        {` ${quote ? `«${item.text}»` : item.text}`}
      </span>
    </p>
  );
}

/**
 * Рассказ мастера — страница книги для чтения вслух: подпись «Мастер», крупный кегль и буквица.
 * Новый рассказ появляется мягко; при «меньше движения» — сразу.
 */
function Narration({ text, animate }: { text: string; animate: boolean }) {
  const appear = useMotionClass("ui-anim-appear");
  return (
    <div className={classNames("feed-read", animate && appear)}>
      <p className="feed-read__who">Мастер</p>
      <div className="ui-narration ui-narration--dropcap">
        {paragraphs(text)}
      </div>
    </div>
  );
}

interface EntryProps {
  item: FeedItem;
  animate: boolean;
  links: HeroLinks;
}

function Entry({ item, animate, links }: EntryProps) {
  switch (item.kind) {
    case "narration":
      return <Narration text={item.text} animate={animate} />;
    case "roll":
      return (
        <>
          {item.private && (
            <p className="feed-secret">
              <SecretMark />
            </p>
          )}
          <RollCard item={item} animate={animate} />
        </>
      );
    case "private":
      return <SealedNote>{paragraphs(item.text)}</SealedNote>;
    case "system":
      return <p className="feed-system">{item.text}</p>;
    case "action":
      return <Spoken item={item} links={links} />;
    case "camp_line":
      return <Spoken item={item} links={links} quote />;
    default:
      // Вид записи, которого этот клиент не знает: текст лучше показать, чем потерять.
      return <p className="feed-system">{item.text}</p>;
  }
}

/** Запас под нижним краем окна, в пределах которого игрок ещё считается стоящим у конца ленты. */
const NEAR_END_PX = 64;

const TEXT_INPUT =
  "input, textarea, select, [contenteditable]:not([contenteditable='false'])";

/** Игрок пишет в поле формы (например, ход): лист под ним двигать нельзя. */
const isTyping = () => document.activeElement?.matches(TEXT_INPUT) === true;

/**
 * Игрок у конца ленты: нижний край прежней последней записи не ниже края окна (с запасом).
 * Выше окна тоже подходит: игрок ушёл вниз, к полю хода, и всё прочитал. Если прежней записи
 * нет (первый показ или её заменила новая версия рассказа), оценить нечем, и лента, как
 * раньше, ведёт к последней.
 */
function seesEnd(previousLast: HTMLElement | null): boolean {
  if (previousLast === null || !previousLast.isConnected) return true;
  const { bottom } = previousLast.getBoundingClientRect();
  return bottom <= window.innerHeight + NEAR_END_PX;
}

const NO_HEROES: ReadonlySet<string> = new Set();

export interface FeedProps {
  items: readonly FeedItem[];
  /** id героев отряда (персонажей): только их имена в ленте ведут к листу. */
  heroIds?: ReadonlySet<string>;
  /** Открыть лист героя по id персонажа; без обработчика имена — просто текст. */
  onHero?: (characterId: string) => void;
}

/**
 * Лента игры: рассказ мастера крупным шрифтом для чтения вслух с подписью «Мастер», карточки
 * бросков, личные сообщения «Только тебе», системные строки мелко, ходы игроков и реплики у
 * костра — с жетоном и именем героя, которое открывает его лист. Записи, что уже были на
 * экране при показе, появляются спокойно; новые — с движением (при «меньше движения» — без
 * него). К новой записи лента прокручивает, только пока игрок у её конца и не набирает текст:
 * тот, кто читает выше или пишет ход, остаётся на месте.
 */
export function Feed({ items, heroIds = NO_HEROES, onHero }: FeedProps) {
  const [shown] = useState(() => new Set(items.map((item) => item.id)));
  const lastRef = useRef<HTMLLIElement>(null);
  // Последняя запись, к которой лента уже принимала решение; по ней видно, что пришла новая.
  const handledRef = useRef<HTMLLIElement | null>(null);
  const reduced = useReducedMotion();
  const lastId = items.at(-1)?.id;

  // К новой записи ведёт сама лента: хост читает рассказ вслух, не листая вручную.
  useEffect(() => {
    const last = lastRef.current;
    const previousLast = handledRef.current;
    if (last === null || last === previousLast) return;
    handledRef.current = last;
    if (isTyping() || !seesEnd(previousLast)) return;
    last.scrollIntoView?.({
      block: "nearest",
      behavior: reduced ? "auto" : "smooth",
    });
  }, [lastId, reduced]);

  if (items.length === 0) {
    return (
      <EmptyState icon="scroll" title="Рассказ ещё не начался">
        Мастер вот-вот заговорит.
      </EmptyState>
    );
  }
  return (
    <ol className="feed" aria-label="Лента">
      {items.map((item, index) => (
        <li
          key={item.id}
          ref={index === items.length - 1 ? lastRef : undefined}
          className="feed-entry"
        >
          <Entry
            item={item}
            animate={!shown.has(item.id)}
            links={{ heroIds, onHero }}
          />
        </li>
      ))}
    </ol>
  );
}
