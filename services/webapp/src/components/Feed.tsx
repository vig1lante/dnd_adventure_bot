import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { FeedItem } from "../api/types";
import { useReducedMotion } from "../theme/motion";
import { Badge, Card, EmptyState, SealedNote } from "../ui";
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

const SecretMark = () => (
  <Badge tone="seal" icon="seal">
    Тайно
  </Badge>
);

function Spoken({ item, quote }: { item: FeedItem; quote?: boolean }) {
  const actor = actorOf(item);
  return (
    <p className="feed-line">
      {item.private && <SecretMark />}
      {actor !== null && <strong>{`${actor}:`}</strong>}
      {` ${quote ? `«${item.text}»` : item.text}`}
    </p>
  );
}

function Entry({ item, animate }: { item: FeedItem; animate: boolean }) {
  switch (item.kind) {
    case "narration":
      return (
        <Card appear={animate} className="feed-narration">
          <div className="ui-narration ui-narration--dropcap">
            {paragraphs(item.text)}
          </div>
        </Card>
      );
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
      return <Spoken item={item} />;
    case "camp_line":
      return <Spoken item={item} quote />;
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

/**
 * Лента игры: рассказ мастера крупным шрифтом для чтения вслух, карточки бросков, личные
 * сообщения «Только тебе», системные строки мелко, ходы игроков и реплики у костра.
 * Записи, что уже были на экране при показе, появляются спокойно; новые — с движением. К новой
 * записи лента прокручивает, только пока игрок у её конца и не набирает текст: тот, кто читает
 * выше или пишет ход, остаётся на месте.
 */
export function Feed({ items }: { items: readonly FeedItem[] }) {
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
          <Entry item={item} animate={!shown.has(item.id)} />
        </li>
      ))}
    </ol>
  );
}
