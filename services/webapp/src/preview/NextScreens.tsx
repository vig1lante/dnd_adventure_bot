import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import type { Api } from "../api/client";
import { ActionComposer } from "../components/ActionComposer";
import { CharacterSheet } from "../components/CharacterSheet";
import { HostPanel } from "../components/HostPanel";
import { PartyList } from "../components/PartyList";
import { Camp } from "../screens/Camp";
import { Combat } from "../screens/Combat";
import { Journal } from "../screens/Journal";
import { Lobby } from "../screens/Lobby";
import { MyGames } from "../screens/MyGames";
import { ModePicker } from "../screens/character/ModePicker";
import { DraftEditor } from "../screens/authoring/DraftEditor";
import { TextField } from "../screens/fields";
import { followGame, useGame } from "../store/game";
import type { GameView } from "../store/game";
import { Button, Card, EmptyState } from "../ui";
import {
  DraftComposerDock,
  DraftErrorState,
  DraftFeed,
  DraftKeyboard,
  DraftSettings,
  DraftSkeleton,
  DraftStatusLine,
  DraftTabBar,
  DraftThinking,
  DraftToast,
} from "./drafts";
import type { GameTab, TabOrder } from "./drafts";
import type { PreviewScreen } from "./gate";
import { PreviewShell } from "./PreviewShell";
import { LOBBY_GAME, PREVIEW_USER } from "./screenData";
import { applyTextSize } from "./settings";

// «Как будет»: черновики предложений на тех же данных, что «как есть».
// Где экран не меняется по существу, показан настоящий экран в новом каркасе (кнопка
// «Настройки» в шапке); новое — из черновиков drafts.tsx.

interface NextScreenProps {
  screen: PreviewScreen;
  api: Api;
  /** Адрес настроек макета в той же теме: на него ведёт кнопка в шапке. */
  settingsHref: string;
}

const noop = () => {};

// --- «Мои игры» в трёх состояниях ---

/** Каркас «Моих игр», как у настоящего экрана; меняется только содержимое карточки игр. */
function GamesFrame({ children }: { children: ReactNode }) {
  return (
    <>
      <div role="tablist" aria-label="Разделы" className="authoring-tabs">
        <Button role="tab" aria-selected>
          Мои игры
        </Button>
        <Button role="tab" aria-selected={false} variant="secondary">
          Приключения
        </Button>
      </div>
      <Card title="Мои игры" icon="scroll">
        <p className="screen-muted">Вы вошли как {PREVIEW_USER.name}.</p>
        {children}
      </Card>
      <Card title="Войти по коду" icon="shield">
        <div className="screen-stack">
          <TextField
            label="Код приглашения"
            hint="Код присылает хост игры."
            value=""
            onChange={noop}
          />
          <Button block>Войти</Button>
        </div>
      </Card>
      <Button icon="d20" block>
        Создать игру
      </Button>
    </>
  );
}

// --- игра ---

/** Игра из хранилища: превью подключается к ней так же, как экран игры (сокет — подмена). */
function useFollowedGame(gameId: string) {
  useEffect(() => followGame(gameId), [gameId]);
  const view = useGame((state) => state.live.view);
  const clockOffsetMs = useGame((state) => state.live.clockOffsetMs);
  const turn = useGame((state) => state.live.turn);
  return { view, clockOffsetMs, turn };
}

interface DraftGameProps {
  gameId: string;
  tab: GameTab;
  order?: TabOrder;
  /** Вместо поля хода — «Мастер думает…». */
  thinking?: boolean;
  /** Открыта клавиатура телефона: поле хода прижато к ней, панели вкладок не видно. */
  keyboard?: boolean;
  /** «Твой ход» в бою, пока игрок читает рассказ. */
  turnToast?: boolean;
  settingsHref: string;
  api: Api;
}

/** Вкладка боя или привала по фазе. */
const fightOf = (view: GameView) =>
  view.combat !== null
    ? "combat"
    : view.phase.turn_phase === "camp"
      ? "camp"
      : null;

function DraftGame({
  gameId,
  tab: initialTab,
  order = "B",
  thinking = false,
  keyboard = false,
  turnToast = false,
  settingsHref,
  api,
}: DraftGameProps) {
  const { view, clockOffsetMs, turn } = useFollowedGame(gameId);
  const [tab, setTab] = useState<GameTab>(initialTab);
  if (view === null) return <PreviewShell api={api}>{null}</PreviewShell>;

  const fight = fightOf(view);
  const userId = PREVIEW_USER.id;
  const panels: Record<GameTab, () => ReactNode> = {
    story: () => (
      <>
        <DraftFeed items={view.feed} onHero={() => setTab("party")} />
        {thinking && <DraftThinking startSeconds={24} isHost />}
        {!thinking && fight === null && !keyboard && (
          <ActionComposer key={turn} gameId={gameId} view={view} />
        )}
      </>
    ),
    fight: () =>
      fight === "combat" ? (
        <Combat gameId={gameId} view={view} turn={turn} />
      ) : (
        <Camp gameId={gameId} view={view} />
      ),
    hero: () => <CharacterSheet character={view.character} />,
    party: () => <PartyList party={view.party} userId={userId} />,
    journal: () => (
      <Journal
        gameId={gameId}
        userId={userId}
        entries={view.journal}
        active={tab === "journal"}
      />
    ),
  };

  return (
    <PreviewShell
      api={api}
      settingsHref={settingsHref}
      className={
        keyboard ? "preview-game preview-game--keyboard" : "preview-game"
      }
    >
      <DraftStatusLine view={view} clockOffsetMs={clockOffsetMs} />
      <HostPanel gameId={gameId} view={view} userId={userId} />
      <div role="tabpanel" aria-label="Открытая вкладка" className="game-panel">
        {panels[tab]()}
      </div>
      {turnToast && tab !== "fight" && (
        <DraftToast
          title="Твой ход"
          action={
            <Button variant="secondary" onClick={() => setTab("fight")}>
              К бою
            </Button>
          }
        >
          Бой идёт: открой вкладку «Бой».
        </DraftToast>
      )}
      {keyboard ? (
        <div className="preview-keyboard-area">
          <DraftComposerDock text={view.action?.text ?? ""} />
          <DraftKeyboard />
        </div>
      ) : (
        <DraftTabBar
          order={order}
          fight={fight}
          active={tab}
          badges={
            turnToast
              ? { fight: "!", journal: "2" }
              : tab === "journal"
                ? {}
                : { journal: "2" }
          }
          onSelect={setTab}
        />
      )}
    </PreviewShell>
  );
}

// --- экраны ---

export function NextScreen({ screen, api, settingsHref }: NextScreenProps) {
  const shell = (children: ReactNode, className?: string) => (
    <PreviewShell api={api} settingsHref={settingsHref} className={className}>
      {children}
    </PreviewShell>
  );
  const game = (props: Omit<DraftGameProps, "settingsHref" | "api">) => (
    <DraftGame {...props} settingsHref={settingsHref} api={api} />
  );

  switch (screen) {
    case "login":
      return shell(
        <Card>
          <DraftErrorState title="Не получилось войти">
            Нет связи с сервером. Проверьте интернет и нажмите «Повторить».
          </DraftErrorState>
        </Card>,
      );
    case "games":
      return shell(<MyGames />);
    case "games-loading":
      return shell(
        <GamesFrame>
          <DraftSkeleton label="Загружаем игры…" />
        </GamesFrame>,
      );
    case "games-empty":
      return shell(
        <GamesFrame>
          <EmptyState
            icon="candle"
            title="Пока нет игр"
            action={<Button variant="secondary">Ввести код приглашения</Button>}
          >
            Создайте игру или войдите в игру друзей по коду приглашения.
          </EmptyState>
        </GamesFrame>,
      );
    case "games-error":
      return shell(
        <GamesFrame>
          <DraftErrorState title="Не удалось загрузить игры">
            Нет связи с сервером. Ваши игры никуда не делись — нажмите
            «Повторить».
          </DraftErrorState>
        </GamesFrame>,
      );
    case "lobby":
      return shell(<Lobby />);
    case "weaving":
      return shell(
        <>
          <div>
            <Button variant="secondary">К моим играм</Button>
          </div>
          <Card title={LOBBY_GAME.adventure_title} icon="scroll">
            <p className="screen-muted">
              Мастер вплетает ваших героев в мир. Игра начнётся сама.
            </p>
          </Card>
          <DraftThinking startSeconds={24} isHost />
        </>,
      );
    case "create":
      return <LargeText>{shell(<ModePicker />)}</LargeText>;
    case "story":
      return game({ gameId: "g-story", tab: "story" });
    case "story-a":
      return game({ gameId: "g-story", tab: "story", order: "A" });
    case "thinking":
      return game({ gameId: "g-think", tab: "story", thinking: true });
    case "composer":
      return game({ gameId: "g-story", tab: "story", keyboard: true });
    case "combat":
      return game({ gameId: "g-combat", tab: "fight" });
    case "combat-toast":
      return game({ gameId: "g-combat", tab: "story", turnToast: true });
    case "camp":
      return game({ gameId: "g-camp", tab: "fight" });
    case "journal":
      return game({ gameId: "g-story", tab: "journal" });
    case "settings":
      return shell(<DraftSettings />);
    case "authoring":
      return shell(
        <DraftEditor draftId="d-salt" onClose={noop} />,
        "preview-authoring",
      );
  }
}

/** Экран с настройкой «Крупный текст»: так видно, что она действует на все экраны. */
function LargeText({ children }: { children: ReactNode }) {
  useState(() => applyTextSize("large"));
  return children;
}
