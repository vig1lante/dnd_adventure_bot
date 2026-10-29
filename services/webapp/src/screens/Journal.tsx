import { useEffect, useId, useState } from "react";
import type { JournalEntry, JournalSection } from "../api/types";
import { Badge, Card, EmptyState, onTabKeyDown } from "../ui";
import { classNames } from "../ui/classNames";
import "./journal.css";

/** Разделы дневника в порядке показа (дополнение к контракту B8, §1). */
const SECTIONS: readonly (readonly [JournalSection, string])[] = [
  ["main", "Главное"],
  ["places", "Места"],
  ["people", "Люди"],
  ["gods", "Боги"],
  ["factions", "Фракции"],
  ["world", "Мир"],
  ["rumors", "Слухи"],
  ["clues", "Улики"],
  ["items", "Предметы"],
];

/** Какую ревизию каждой записи игрок уже видел: `entry.id → revision`. */
type SeenMap = Readonly<Record<string, number>>;

// «Новое» помнит только этот браузер, отдельно для каждой игры и игрока (сервер о нём не знает).
const seenKey = (gameId: string, userId: string) =>
  `dnd-journal-seen:${gameId}:${userId}`;

// Хранилище бывает недоступно (частный режим, запрет сайта) или испорчено. Тогда записи просто
// снова «новые», а дневник работает как обычно: отметка прочтения — удобство, а не данные игры.
function readSeen(key: string): SeenMap {
  try {
    const parsed: unknown = JSON.parse(
      window.localStorage.getItem(key) ?? "{}",
    );
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed))
      return {};
    return Object.fromEntries(
      Object.entries(parsed).filter(
        ([, revision]) => typeof revision === "number",
      ),
    );
  } catch {
    return {};
  }
}

function writeSeen(key: string, seen: SeenMap): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(seen));
  } catch {
    // Не запомнили — в следующий раз эти записи снова будут «новыми»; больше ничего не теряется.
  }
}

/** Запись новая, пока игрок не видел её текущую ревизию: дополнение снова делает её новой. */
const isUnseen = (entry: JournalEntry, seen: SeenMap) =>
  (seen[entry.id] ?? 0) < entry.revision;

/** `seen` с отметками о записях `entries`; тот же объект, если отмечать нечего. */
function markSeen(seen: SeenMap, entries: JournalEntry[]): SeenMap {
  const unseen = entries.filter((entry) => isUnseen(entry, seen));
  if (unseen.length === 0) return seen;
  return {
    ...seen,
    ...Object.fromEntries(unseen.map((entry) => [entry.id, entry.revision])),
  };
}

/**
 * `seen` только о записях, которые есть в дневнике, и не выше их нынешней ревизии; тот же объект,
 * если подрезать нечего. После «Перегенерировать» запись могла пропасть или откатиться: вернувшись
 * или снова выросши, она должна стать «новой», даже с той ревизией, что игрок видел до отката.
 */
function pruneSeen(seen: SeenMap, entries: JournalEntry[]): SeenMap {
  const revisions = new Map(entries.map((entry) => [entry.id, entry.revision]));
  const kept = Object.entries(seen).flatMap(([id, revision]) => {
    const current = revisions.get(id);
    return current === undefined ? [] : [[id, Math.min(revision, current)]];
  });
  const same =
    kept.length === Object.keys(seen).length &&
    kept.every(([id, revision]) => seen[id] === revision);
  return same ? seen : Object.fromEntries(kept);
}

/**
 * Что в дневнике для игрока «новое». Увиденным становится раздел, который игрок открыл
 * (`open`; `null` — дневника на экране нет): его записи, и пришедшие при открытом разделе
 * тоже, сразу запоминаются. Пометки же сверяются с тем, что было увидено к открытию раздела,
 * и гаснут, когда игрок из него уходит, — иначе он не успел бы заметить, что нового. Обе карты
 * всегда подрезаны по нынешним записям (`pruneSeen`).
 */
function useUnseen(
  storageKey: string,
  entries: JournalEntry[],
  open: JournalSection | null,
): (entry: JournalEntry) => boolean {
  // `changed` — есть что записать: одно чтение хранилища записывать обратно незачем.
  const [seen, setSeen] = useState(() => ({
    map: readSeen(storageKey),
    changed: false,
  }));
  const known = pruneSeen(seen.map, entries);
  const [shown, setShown] = useState({ open, seen: known });
  const shownKnown = pruneSeen(shown.seen, entries);
  if (shown.open !== open) setShown({ open, seen: known });
  else if (shownKnown !== shown.seen) setShown({ open, seen: shownKnown });

  const opened =
    open === null ? [] : entries.filter((entry) => entry.section === open);
  const marked = markSeen(known, opened);
  if (marked !== seen.map) setSeen({ map: marked, changed: true });

  useEffect(() => {
    if (seen.changed) writeSeen(storageKey, seen.map);
  }, [storageKey, seen]);

  return (entry) => isUnseen(entry, shownKnown);
}

/** Когда отряд узнал о записи; у места, где отряд уже был, — когда пришёл туда. */
function whenText(entry: JournalEntry): string {
  if (entry.visited_turn !== null) {
    return entry.visited_turn === 0
      ? "были здесь с начала игры"
      : `были здесь: раунд ${entry.visited_turn}`;
  }
  return entry.turn === 0
    ? "известно с начала игры"
    : `узнали в раунде ${entry.turn}`;
}

/**
 * Когда текст записи дополняли в последний раз; не дополняли — ничего. Первый приход в место
 * дополнением не считается (контракт B11, §1 п. 4), а сервер до B11 поля не присылает.
 */
function updatedText(entry: JournalEntry): string | null {
  const turn = entry.updated_turn ?? null;
  if (turn === null) return null;
  return turn === 0 ? "дополнено в начале игры" : `дополнено в раунде ${turn}`;
}

const squeezed = (text: string) => text.split(/\s+/).join(" ").trim();

/**
 * Абзацы записи. Дополнения приходят новыми абзацами через пустую строку. У записи без своего
 * имени (короткий слух, улика) название — это её текст: такой первый абзац не повторяем.
 */
function paragraphsOf(entry: JournalEntry): string[] {
  const paragraphs = entry.text.split("\n\n").filter((part) => part.trim());
  return squeezed(paragraphs[0] ?? "") === squeezed(entry.title)
    ? paragraphs.slice(1)
    : paragraphs;
}

function EntryCard({
  entry,
  unseen,
}: {
  entry: JournalEntry;
  unseen: boolean;
}) {
  const paragraphs = paragraphsOf(entry);
  const updated = updatedText(entry);
  return (
    <Card title={entry.title} headingLevel={3} className="journal-entry">
      {unseen && <Badge tone="amber">новое</Badge>}
      {paragraphs.map((paragraph, index) => (
        <p key={index} className="journal-entry__text">
          {paragraph}
        </p>
      ))}
      <p className="journal-entry__when">{whenText(entry)}</p>
      {updated !== null && <p className="journal-entry__when">{updated}</p>}
    </Card>
  );
}

interface JournalBodyProps {
  storageKey: string;
  entries: JournalEntry[];
  active: boolean;
}

function JournalBody({ storageKey, entries, active }: JournalBodyProps) {
  const base = useId();
  const [selected, setSelected] = useState<JournalSection>("main");
  const isUnseenNow = useUnseen(storageKey, entries, active ? selected : null);

  const inSection = (section: JournalSection) =>
    entries.filter((entry) => entry.section === section);
  const tabId = (section: JournalSection) => `${base}-tab-${section}`;
  const panelId = `${base}-panel`;
  const newHintId = `${base}-new`;
  const shown = inSection(selected);

  // Клавиатура — как у вкладок игры: в порядке табуляции один выбранный раздел, к остальным
  // ведут стрелки (по кругу), Home и End.
  const pick = (index: number) => setSelected(SECTIONS[index][0]);

  return (
    <div className="journal">
      <div
        role="tablist"
        aria-label="Разделы дневника"
        className="journal-sections"
      >
        {SECTIONS.map(([section, label], index) => {
          const current = section === selected;
          const items = inSection(section);
          const hasNew = items.some(isUnseenNow);
          return (
            <button
              key={section}
              id={tabId(section)}
              type="button"
              role="tab"
              aria-selected={current}
              aria-controls={current ? panelId : undefined}
              // Имя вкладки «<Раздел> <N>» не трогаем: подсветку счётчика читалка узнаёт отсюда.
              aria-describedby={hasNew ? newHintId : undefined}
              tabIndex={current ? 0 : -1}
              className={classNames(
                "journal-section",
                current && "journal-section--active",
              )}
              onClick={() => setSelected(section)}
              onKeyDown={(event) =>
                onTabKeyDown(event, index, SECTIONS.length, pick)
              }
            >
              <span className="journal-section__label">{label}</span>{" "}
              <span
                className={classNames(
                  "journal-section__count",
                  hasNew && "journal-section__count--new",
                )}
              >
                {items.length}
              </span>
            </button>
          );
        })}
      </div>
      <span id={newHintId} hidden>
        есть новое
      </span>
      <div id={panelId} role="tabpanel" aria-labelledby={tabId(selected)}>
        {shown.length === 0 ? (
          <Card>
            <EmptyState title="Пока ничего не известно" />
          </Card>
        ) : (
          <ul className="journal-entries">
            {shown.map((entry) => (
              <li key={entry.id}>
                <EntryCard entry={entry} unseen={isUnseenNow(entry)} />
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

export interface JournalProps {
  gameId: string;
  userId: string;
  /** Записи, которые сервер прислал этому игроку (общие и личные его героя), по порядку. */
  entries: JournalEntry[];
  /** Вкладка дневника сейчас на экране: только тогда открытый раздел считается увиденным. */
  active: boolean;
}

/**
 * Дневник отряда: разделы-вкладки со счётчиками записей и карточки записей с пометкой «новое».
 * Всё, что в нём есть, прислал сервер; клиент лишь помнит, какие записи игрок уже видел.
 */
export function Journal({ gameId, userId, entries, active }: JournalProps) {
  const storageKey = seenKey(gameId, userId);
  // Другая игра или другой игрок — свои отметки: состояние дневника начинается заново.
  return (
    <JournalBody
      key={storageKey}
      storageKey={storageKey}
      entries={entries}
      active={active}
    />
  );
}
