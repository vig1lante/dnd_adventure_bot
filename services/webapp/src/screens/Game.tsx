import { useEffect, useId, useRef, useState } from "react";
import type { KeyboardEvent, ReactNode } from "react";
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
import { followGame, gameStage, useGame } from "../store/game";
import type { GameView, LiveConnection } from "../store/game";
import { Alert, Button, Card, EmptyState, Loading } from "../ui";
import { classNames } from "../ui/classNames";
import "../components/game.css";
import { Camp } from "./Camp";
import { Combat } from "./Combat";
import { Journal } from "./Journal";

type TabId = "story" | "journal" | "combat" | "camp" | "hero" | "party";

const LABELS: Record<TabId, string> = {
  story: "Рассказ",
  journal: "Дневник",
  combat: "Бой",
  camp: "Привал",
  hero: "Персонаж",
  party: "Отряд",
};

// Лист героя подгружает названия правил, поэтому, как и отряд, открывается лишь при первом заходе.
// Дневник тоже: пока игрок его не открыл, отметки «новое» не читаются и не пишутся.
const LAZY_TABS: ReadonlySet<TabId> = new Set(["journal", "hero", "party"]);

// Столько вкладок помещается в строку обычным шрифтом; больше (бой или привал) — мельче.
const MAX_ROOMY_TABS = 4;

/**
 * Вкладки по состоянию игры: «Дневник» сразу после «Рассказа», «Бой» живёт, пока идёт бой,
 * «Привал» — пока идёт привал.
 */
function tabsOf(view: GameView): TabId[] {
  const tabs: TabId[] = ["story", "journal"];
  if (view.combat !== null) tabs.push("combat");
  if (view.phase.turn_phase === "camp") tabs.push("camp");
  tabs.push("hero", "party");
  return tabs;
}

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
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);

  const tabs = tabsOf(view);
  // Вкладка боя или привала исчезла из-под игрока: возвращаем «Рассказ» и забываем выбор, чтобы
  // следующий бой не открылся сам.
  if (!tabs.includes(selected)) setSelected("story");
  const tab = tabs.includes(selected) ? selected : "story";

  const select = (id: TabId) => {
    setSelected(id);
    setVisited((now) => (now.includes(id) ? now : [...now, id]));
  };

  // Стрелки переключают вкладки по кругу, как в обычном наборе вкладок.
  const onKeyDown = (event: KeyboardEvent, index: number) => {
    const step =
      event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (step === 0) return;
    const next = (index + step + tabs.length) % tabs.length;
    select(tabs[next]);
    buttons.current[next]?.focus();
  };

  const idOf = (kind: "tab" | "panel", id: TabId) => `${base}-${kind}-${id}`;
  // Поле хода одно на игру: в бою оно на вкладке «Бой», на привале его нет, иначе — на «Рассказе».
  const sideScreen = view.combat !== null || view.phase.turn_phase === "camp";
  const content: Record<TabId, () => ReactNode> = {
    story: () => (
      <>
        <Feed items={view.feed} />
        {sideScreen ? (
          <SideLink view={view} open={select} />
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
      <div
        role="tablist"
        aria-label="Разделы игры"
        className={classNames(
          "game-tabs",
          tabs.length > MAX_ROOMY_TABS && "game-tabs--dense",
        )}
      >
        {tabs.map((id, index) => (
          <button
            key={id}
            ref={(node) => {
              buttons.current[index] = node;
            }}
            id={idOf("tab", id)}
            type="button"
            role="tab"
            aria-selected={tab === id}
            aria-controls={idOf("panel", id)}
            tabIndex={tab === id ? 0 : -1}
            className={classNames("game-tab", tab === id && "game-tab--active")}
            onClick={() => select(id)}
            onKeyDown={(event) => onKeyDown(event, index)}
          >
            {LABELS[id]}
          </button>
        ))}
      </div>
      {tabs.map((id) => (
        <div
          key={id}
          id={idOf("panel", id)}
          role="tabpanel"
          aria-labelledby={idOf("tab", id)}
          hidden={tab !== id}
          className="game-panel"
        >
          {(!LAZY_TABS.has(id) || visited.includes(id)) && content[id]()}
        </div>
      ))}
    </>
  );
}

/**
 * Экран игры: строка статуса с отсчётом, кнопки хоста и вкладки — «Рассказ» (лента и поле
 * хода), «Дневник», «Бой» и «Привал» (только пока идут бой и привал), «Персонаж» и «Отряд».
 * Игру ведёт сокет: экран сам подключается к ней (`followGame`) и отключается при уходе; всё, что на
 * экране, приходит снимком и событиями. Обрыв связи показывает строка статуса, а пока её
 * нет (игра ещё грузится), — плашка на её месте.
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
      {view === null && <Loading label="Подключаемся к игре…" />}
      {view !== null && stage !== "game" && (
        <Loading label="Открываем лобби…" />
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
