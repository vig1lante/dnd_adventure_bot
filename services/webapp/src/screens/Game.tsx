import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { ReactNode } from "react";
import { flushSync } from "react-dom";
import type { PhaseView } from "../api/types";
import { ActionComposer } from "../components/ActionComposer";
import {
  composerAccess,
  connectionLost,
  isHeroTurn,
} from "../components/access";
import { CharacterSheet } from "../components/CharacterSheet";
import { Feed } from "../components/Feed";
import { HostPanel } from "../components/HostPanel";
import { PartyList } from "../components/PartyList";
import { restLabel } from "../components/phases";
import { ReconnectingNote, StatusBar } from "../components/StatusBar";
import { phaseStartedAt, useThinking } from "../components/waiting";
import { followGame, gameStage, useGame } from "../store/game";
import type { GameView, LiveConnection } from "../store/game";
import { buzz } from "../theme/haptics";
import { useOnScreenKeyboard } from "../theme/keyboard";
import { useReducedMotion } from "../theme/motion";
import {
  Alert,
  Button,
  Card,
  EmptyState,
  Skeleton,
  TabBar,
  TabPanel,
  Toast,
  Waiting,
  tabIds,
} from "../ui";
import type { IconName, TabBarItem, ToastMessage } from "../ui";
import "../components/game.css";
import "./screens.css";
import { Camp } from "./Camp";
import { Combat } from "./Combat";
import { Journal } from "./Journal";

type TabId = "story" | "combat" | "camp" | "hero" | "party" | "journal";

const TABS: Record<TabId, { label: string; icon: IconName }> = {
  story: { label: "Рассказ", icon: "scroll" },
  combat: { label: "Бой", icon: "d20" },
  camp: { label: "Привал", icon: "candle" },
  hero: { label: "Персонаж", icon: "heart" },
  party: { label: "Отряд", icon: "shield" },
  journal: { label: "Дневник", icon: "seal" },
};

// Лист героя подгружает названия правил, поэтому, как и отряд, открывается лишь при первом заходе.
// Дневник тоже: пока игрок его не открыл, отметки «новое» не читаются и не пишутся.
const LAZY_TABS: ReadonlySet<TabId> = new Set(["journal", "hero", "party"]);

/**
 * Вкладки по состоянию игры в порядке «В» (решение владельца на B10.1.gate): «Бой» и «Привал»
 * появляются по фазе сразу за «Рассказом», «Дневник» — последний, читательский.
 */
function tabsOf(view: GameView): TabId[] {
  const tabs: TabId[] = ["story"];
  if (view.combat !== null) tabs.push("combat");
  if (view.phase.turn_phase === "camp") tabs.push("camp");
  tabs.push("hero", "party", "journal");
  return tabs;
}

// --- что нового: бейджи вкладок, тосты и вибрация ---

/** События, о которых игрок узнаёт тостом и вибрацией. */
type Notice = "your-turn" | "collecting" | "rest";

/** Вкладка, где игрок и так увидит то, о чём уведомление: на ней тост не нужен. */
const NOTICE_TABS: Record<Notice, TabId> = {
  "your-turn": "combat",
  collecting: "story",
  rest: "camp",
};

// Только факт события: тексты ходов, писем и рассказа сюда не попадают.
const NOTICES: Record<Notice, ToastMessage> = {
  "your-turn": { title: "Твой ход", text: "Бой идёт: открой вкладку «Бой»." },
  collecting: {
    title: "Хост собирает ходы",
    text: "Успей сдать ход до конца отсчёта.",
  },
  rest: {
    title: "Привал",
    text: "Отряд отдыхает у костра: загляни на вкладку «Привал».",
  },
};

/** Сколько нового на вкладках; вкладки без нового в карте нет. */
type Counts = Partial<Record<TabId, number>>;

/** Сервер ждёт ход этого игрока в бою: поле хода на вкладке «Бой» открыто. */
const myCombatTurn = (view: GameView) =>
  view.combat !== null &&
  composerAccess(view.phase, view.combat, view.character?.id ?? null).open;

/** Ход игрока на этот раунд ещё не сдан: герой есть, а хода нет или он черновик. */
const turnNotSealed = (view: GameView) =>
  view.character !== null &&
  (view.action === null || view.action.status === "draft");

/** Сколько в `after` новых элементов по ключу по сравнению с `before`. */
function freshCount<T>(
  before: readonly T[],
  after: readonly T[],
  key: (item: T) => string | number,
): number {
  const known = new Set(before.map(key));
  return after.filter((item) => !known.has(key(item))).length;
}

/** Что принёс кадр игры: новое на вкладках, о чём уведомить и закончился ли мой ход в бою. */
interface News {
  counts: Counts;
  notice: Notice | null;
  /**
   * Мой ход в бою кончился (сходил, вышло время, авто-пас): «!» на «Бое» и тост «Твой ход»
   * больше не правда — они гаснут, даже если вкладку так и не открыли.
   */
  turnOver: boolean;
}

/**
 * Что нового принёс кадр игры: сколько записей на каких вкладках и о чём уведомить. Считает
 * только факты (сколько, чей ход), а не тексты: тайное письмо в бейдже — просто «+1».
 */
function newsBetween(before: GameView, after: GameView, userId: string): News {
  const revisions = new Map(
    before.journal.map((entry) => [entry.id, entry.revision]),
  );
  const yourTurn = myCombatTurn(after) && !myCombatTurn(before);
  const turnOver = myCombatTurn(before) && !myCombatTurn(after);
  const restStarted =
    after.phase.turn_phase === "camp" && before.phase.turn_phase !== "camp";
  // «Собрать ходы» зовёт сдать ход: хосту (он сам нажал) и тому, кто уже сдал или спасовал,
  // звать не к чему — ложная тревога приучает не замечать вибрацию.
  const collecting =
    after.phase.turn_phase === "collecting" &&
    before.phase.turn_phase !== "collecting" &&
    after.host_user_id !== userId &&
    turnNotSealed(after);
  const counts: Counts = {
    // Реплики у костра живут на вкладке «Привал»: в рассказе они не новость.
    story: freshCount(
      before.feed.filter((item) => item.kind !== "camp_line"),
      after.feed.filter((item) => item.kind !== "camp_line"),
      (item) => item.id,
    ),
    journal: after.journal.filter(
      (entry) => entry.revision > (revisions.get(entry.id) ?? 0),
    ).length,
    combat: yourTurn ? 1 : 0,
    camp:
      (restStarted ? 1 : 0) +
      freshCount(
        before.rest?.lines ?? [],
        after.rest?.lines ?? [],
        (item) => item.id,
      ) +
      Math.max(
        0,
        (after.rest?.my_ideas.length ?? 0) -
          (before.rest?.my_ideas.length ?? 0),
      ),
  };
  const notice: Notice | null = yourTurn
    ? "your-turn"
    : restStarted
      ? "rest"
      : collecting
        ? "collecting"
        : null;
  return { counts, notice, turnOver };
}

/**
 * Счётчики после кадра: прибавлено новое, кроме открытой вкладки; исчезнувших вкладок нет.
 * «!» боя — состояние «твой ход», а не счёт: ход кончился — отметка гаснет.
 */
function countsAfter(
  now: Counts,
  news: News,
  tabs: readonly TabId[],
  open: TabId,
): Counts {
  const next: Counts = {};
  for (const id of tabs) {
    const kept = id === "combat" && news.turnOver ? 0 : (now[id] ?? 0);
    const count = id === open ? 0 : kept + (news.counts[id] ?? 0);
    if (count > 0) next[id] = count;
  }
  return next;
}

const withoutTab = (counts: Counts, id: TabId): Counts =>
  Object.fromEntries(Object.entries(counts).filter(([key]) => key !== id));

/** Отметка на вкладке: у боя — «!» (твой ход), у остальных — сколько нового. */
const badgeText = (id: TabId, count: number) =>
  id === "combat" ? "!" : count > 9 ? "9+" : String(count);

/**
 * Новое на вкладках и уведомления о событиях игры. Кадры игры приходят в хранилище по сокету:
 * хук сравнивает каждый кадр с прежним, а снимок при входе событием не считается. Открытая
 * вкладка прочитана: её отметка гаснет, тост о ней не нужен. Вибрация — на каждое событие
 * (игрок мог положить телефон), тост — только если игрок не на нужной вкладке.
 */
function useGameNews(userId: string, open: TabId) {
  const [counts, setCounts] = useState<Counts>({});
  const [toast, setToast] = useState<Notice | null>(null);
  // Открытая вкладка нужна обработчику кадров, а не отрисовке: он живёт вне цикла рендера.
  const openNow = useRef(open);
  useEffect(() => {
    openNow.current = open;
  }, [open]);

  useEffect(
    () =>
      useGame.subscribe((state, previous) => {
        const after = state.live.view;
        const before = previous.live.view;
        if (after === before || after === null || before === null) return;
        const news = newsBetween(before, after, userId);
        const shown = openNow.current;
        setCounts((now) => countsAfter(now, news, tabsOf(after), shown));
        if (news.turnOver) {
          setToast((now) => (now === "your-turn" ? null : now));
        }
        if (news.notice === null) return;
        buzz(news.notice);
        if (NOTICE_TABS[news.notice] !== shown) setToast(news.notice);
      }),
    [userId],
  );

  if (counts[open] !== undefined) setCounts(withoutTab(counts, open));
  if (toast !== null && NOTICE_TABS[toast] === open) setToast(null);

  const dismissToast = useCallback(() => setToast(null), []);
  return { counts, toast, dismissToast };
}

// --- вкладки ---

interface SideLinkProps {
  view: GameView;
  /** Открыть вкладку боя или привала. */
  open: (tab: "combat" | "camp") => void;
}

/**
 * На «Рассказе» в бою и на привале поля хода нет: оно на вкладке «Бой», а на привале ходов
 * не принимают. Вместо него — короткая подсказка, кто ходит, и кнопка на нужную вкладку.
 */
function SideLink({ view, open }: SideLinkProps) {
  const { combat, character } = view;
  if (combat === null) {
    return (
      <Card title={restLabel(view.rest?.kind ?? null)} icon="candle">
        <p className="game-goto__text">
          Герои отдыхают у костра: реплики, тема мастера и отсчёт.
        </p>
        <Button variant="secondary" onClick={() => open("camp")}>
          К костру
        </Button>
      </Card>
    );
  }
  const mine = isHeroTurn(combat, character?.id ?? null);
  const note = composerAccess(view.phase, combat, character?.id ?? null).note;
  return (
    <Card title="Идёт бой" icon="d20">
      <p className="game-goto__text">
        {mine && note === null ? "Сейчас ваш ход." : note}
      </p>
      <Button
        variant={mine ? "primary" : "secondary"}
        onClick={() => open("combat")}
      >
        К бою
      </Button>
    </Card>
  );
}

interface MasterThinkingProps {
  /** Серверное начало разбора (`phase.phase_since`); без него счёт идёт с показа экрана. */
  since: PhaseView["phase_since"];
  clockOffsetMs: number;
}

/**
 * «Мастер думает…» на «Рассказе»: фраза по времени и сколько уже ждём, пока идёт разбор. Счёт
 * идёт от серверного начала разбора, поэтому перезагрузка страницы его не обнуляет.
 */
function MasterThinking({ since, clockOffsetMs }: MasterThinkingProps) {
  const thinking = useThinking(
    `resolving@${since ?? ""}`,
    phaseStartedAt(since, clockOffsetMs),
  );
  return <Waiting {...thinking} />;
}

interface GameBodyProps {
  gameId: string;
  view: GameView;
  userId: string;
  clockOffsetMs: number;
  turn: number;
  connection: LiveConnection;
}

function GameBody({
  gameId,
  view,
  userId,
  clockOffsetMs,
  turn,
  connection,
}: GameBodyProps) {
  const base = useId();
  const [selected, setSelected] = useState<TabId>("story");
  const [visited, setVisited] = useState<readonly TabId[]>([]);
  const keyboardOpen = useOnScreenKeyboard();

  const tabs = tabsOf(view);
  // Вкладка боя или привала исчезла из-под игрока: возвращаем «Рассказ» и забываем выбор, чтобы
  // следующий бой не открылся сам.
  if (!tabs.includes(selected)) setSelected("story");
  const tab = tabs.includes(selected) ? selected : "story";
  const news = useGameNews(userId, tab);

  const select = (id: TabId) => {
    setSelected(id);
    setVisited((now) => (now.includes(id) ? now : [...now, id]));
  };

  /** Фокус на вкладку нижней панели (она всегда на месте, в отличие от нажатой кнопки). */
  const focusTab = (id: TabId) =>
    document.getElementById(tabIds(base, id).tab)?.focus();

  /**
   * Переход на вкладку кнопкой внутри экрана («К бою», «К костру»): кнопка исчезает вместе со
   * скрытой панелью или тостом, поэтому фокус уходит на открытую вкладку, а не на страницу.
   */
  const goTo = (id: TabId) => {
    select(id);
    focusTab(id);
  };

  const reduced = useReducedMotion();
  const heroIds = new Set(view.party.map((member) => member.id));
  if (view.character !== null) heroIds.add(view.character.id);

  /**
   * Имя героя в ленте ведёт к его листу: своего — на «Персонаж», чужого — на «Отряд», к его
   * карточке. Новых данных не нужно: всё уже в снимке. Вкладка открывается сразу (flushSync),
   * чтобы фокус перешёл на неё, а лист — прокрутился в видимость.
   */
  const openHero = (characterId: string) => {
    const own = characterId === view.character?.id;
    const target: TabId = own ? "hero" : "party";
    flushSync(() => select(target));
    focusTab(target);
    const panel = document.getElementById(tabIds(base, target).panel);
    const index = view.party.findIndex((member) => member.id === characterId);
    // Карточки отряда идут в порядке снимка: у чужого героя — его карточка, у своего — лист.
    const card = own
      ? panel
      : panel?.querySelectorAll(":scope > ul > li")[index];
    card?.scrollIntoView?.({
      block: own ? "start" : "center",
      behavior: reduced ? "auto" : "smooth",
    });
  };

  const items: TabBarItem<TabId>[] = tabs.map((id) => {
    const count = news.counts[id];
    return {
      id,
      ...TABS[id],
      badge: count === undefined ? undefined : badgeText(id, count),
    };
  });

  // Поле хода одно на игру: в бою оно на вкладке «Бой», на привале его нет, иначе — на «Рассказе».
  const sideScreen = view.combat !== null || view.phase.turn_phase === "camp";
  const resolving =
    view.phase.phase === "playing" && view.phase.turn_phase === "resolving";
  const content: Record<TabId, () => ReactNode> = {
    story: () => (
      <>
        <Feed items={view.feed} heroIds={heroIds} onHero={openHero} />
        {resolving && (
          <MasterThinking
            since={view.phase.phase_since}
            clockOffsetMs={clockOffsetMs}
          />
        )}
        {sideScreen ? (
          <SideLink view={view} open={goTo} />
        ) : (
          <ActionComposer key={turn} gameId={gameId} view={view} />
        )}
      </>
    ),
    journal: () => (
      <Journal
        gameId={gameId}
        userId={userId}
        entries={view.journal}
        active={tab === "journal"}
      />
    ),
    combat: () => <Combat gameId={gameId} view={view} turn={turn} />,
    camp: () => <Camp gameId={gameId} view={view} />,
    hero: () => <CharacterSheet character={view.character} />,
    party: () => <PartyList party={view.party} userId={userId} />,
  };

  return (
    <>
      <StatusBar
        view={view}
        clockOffsetMs={clockOffsetMs}
        connection={connection}
      />
      <HostPanel gameId={gameId} view={view} userId={userId} />
      <TabBar
        label="Разделы игры"
        idBase={base}
        items={items}
        selected={tab}
        onSelect={select}
        hidden={keyboardOpen}
      />
      <Toast
        message={news.toast === null ? null : NOTICES[news.toast]}
        onDismiss={news.dismissToast}
        onHide={() => {
          news.dismissToast();
          focusTab(tab);
        }}
        action={
          news.toast === "your-turn" && (
            <Button variant="secondary" onClick={() => goTo("combat")}>
              К бою
            </Button>
          )
        }
      />
      {tabs.map((id) => (
        <TabPanel
          key={id}
          idBase={base}
          id={id}
          selected={tab === id}
          className="game-panel"
        >
          {(!LAZY_TABS.has(id) || visited.includes(id)) && content[id]()}
        </TabPanel>
      ))}
    </>
  );
}

/**
 * Экран игры: строка статуса с днём и отсчётом, кнопки хоста, нижняя панель вкладок с отметками
 * нового — «Рассказ» (лента и поле хода), «Бой» и «Привал» (только пока идут бой и привал),
 * «Персонаж», «Отряд» и «Дневник» — и тосты о событиях. Игру ведёт сокет: экран сам
 * подключается к ней (`followGame`) и отключается при уходе; всё, что на экране, приходит
 * снимком и событиями. Обрыв связи показывает строка статуса, а пока её нет (игра ещё
 * грузится), — плашка на её месте.
 */
export function Game() {
  const gameId = useGame((state) => state.gameId);
  const view = useGame((state) => state.live.view);
  const liveError = useGame((state) => state.live.error);
  const clockOffsetMs = useGame((state) => state.live.clockOffsetMs);
  const turn = useGame((state) => state.live.turn);
  const connection = useGame((state) => state.connection);
  const userId = useGame((state) => state.session?.user.id);
  const show = useGame((state) => state.show);

  useEffect(() => {
    if (gameId === null) return;
    return followGame(gameId);
  }, [gameId]);

  // Игра ещё не пошла (например, её вернули к началу): здесь делать нечего, экран ведёт лобби.
  const stage = view === null ? null : gameStage(view);
  useEffect(() => {
    if ((stage === "lobby" || stage === "weaving") && gameId !== null) {
      show({ screen: "lobby", gameId });
    }
  }, [stage, gameId, show]);

  if (gameId === null) {
    return (
      <Card>
        <EmptyState
          icon="scroll"
          title="Игра не выбрана"
          action={
            <Button
              variant="secondary"
              onClick={() => show({ screen: "games" })}
            >
              К моим играм
            </Button>
          }
        />
      </Card>
    );
  }

  const inGame = view !== null && stage === "game" && userId !== undefined;
  return (
    <>
      <div>
        <Button variant="secondary" onClick={() => show({ screen: "games" })}>
          К моим играм
        </Button>
      </div>
      {!inGame && connectionLost(connection) && <ReconnectingNote />}
      {liveError !== null && <Alert>{liveError}</Alert>}
      {view === null && <Skeleton label="Подключаемся к игре…" />}
      {view !== null && stage !== "game" && (
        <Skeleton label="Открываем лобби…" />
      )}
      {inGame && (
        <GameBody
          gameId={gameId}
          view={view}
          userId={userId}
          clockOffsetMs={clockOffsetMs}
          turn={turn}
          connection={connection}
        />
      )}
    </>
  );
}
