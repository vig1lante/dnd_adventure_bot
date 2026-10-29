import { useCallback, useId, useRef, useState } from "react";
import type { FormEvent } from "react";
import { useApi } from "../api/context";
import type { GamePhase, GameSummary } from "../api/types";
import { parseJoinCode } from "../auth/session";
import { useGame } from "../store/game";
import {
  Alert,
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  Skeleton,
  TabPanel,
  onTabKeyDown,
  tabIds,
} from "../ui";
import type { BadgeProps } from "../ui";
import { CreateGameForm } from "./CreateGameForm";
import { TextField } from "./fields";
import { AdventuresHome, momentText } from "./authoring/AdventuresHome";
import { useAction, useLoaded } from "./hooks";
import "./screens.css";
import "./authoring/authoring.css";

const PHASES: Record<GamePhase, { label: string; tone: BadgeProps["tone"] }> = {
  lobby: { label: "Лобби", tone: "neutral" },
  weaving: { label: "Начало игры", tone: "amber" },
  playing: { label: "Идёт игра", tone: "cloth" },
  paused: { label: "Пауза", tone: "neutral" },
  finished: { label: "Завершена", tone: "neutral" },
};

// Игра в начале (мастер вплетает героев) показывается в лобби: экран лобби знает, что
// в этой фазе делать, и сам переходит к игре, когда мастер закончил.
const screenFor = (phase: GamePhase) =>
  phase === "lobby" || phase === "weaving" ? "lobby" : "game";

const CODE_FORMAT_ERROR = "Код состоит из 6 символов: латинские буквы и цифры.";

function JoinForm() {
  const api = useApi();
  const show = useGame((state) => state.show);
  const [code, setCode] = useState("");
  const [formatError, setFormatError] = useState<string | null>(null);
  const { busy, error, run } = useAction();

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    // Неверный код на сервер не уходит: каждая неудачная попытка там считается,
    // а после десятой вход по кодам блокируется на 10 минут.
    const parsed = parseJoinCode(code);
    if (parsed === null) {
      setFormatError(CODE_FORMAT_ERROR);
      return;
    }
    setFormatError(null);
    await run(async () => {
      const game = await api.joinGame(parsed);
      show({ screen: screenFor(game.phase), gameId: game.id });
    });
  };

  return (
    <form className="screen-stack" onSubmit={(event) => void submit(event)}>
      <TextField
        label="Код приглашения"
        hint="Код присылает хост игры."
        value={code}
        error={formatError}
        autoComplete="off"
        autoCapitalize="characters"
        spellCheck={false}
        onChange={(event) => {
          setCode(event.target.value);
          setFormatError(null);
        }}
      />
      {error !== null && <Alert>{error}</Alert>}
      <Button type="submit" busy={busy} block>
        Войти
      </Button>
    </form>
  );
}

/**
 * Строка игры: название, фаза, число игроков и дата создания. Игры одного приключения различаются
 * датой — она же описывает кнопку «Открыть» для читалки. Сервер до B11 даты не присылает.
 */
function GameRow({ game }: { game: GameSummary }) {
  const show = useGame((state) => state.show);
  const metaId = useId();
  const phase = PHASES[game.phase];
  const created = momentText(game.created_at ?? "");
  return (
    <li className="games-item">
      <div className="games-item__main">
        <span className="games-item__title">{game.adventure_title}</span>
        <div id={metaId} className="games-item__meta">
          <Badge tone={phase.tone}>{phase.label}</Badge>
          <span>Игроков: {game.players.length}</span>
          {created !== "" && <span>создана {created}</span>}
        </div>
      </div>
      <Button
        variant="secondary"
        aria-label={`Открыть: ${game.adventure_title}`}
        aria-describedby={metaId}
        onClick={() => show({ screen: screenFor(game.phase), gameId: game.id })}
      >
        Открыть
      </Button>
    </li>
  );
}

type Tab = "games" | "adventures";

const TABS: readonly { value: Tab; label: string }[] = [
  { value: "games", label: "Мои игры" },
  { value: "adventures", label: "Приключения" },
];

interface TabsProps {
  value: Tab;
  onChange: (tab: Tab) => void;
  /** Общая часть id вкладок и панели (как у `TabPanel`). */
  idBase: string;
}

/** Вкладки «Мои игры» и «Приключения»: стрелки, Home и End, как у остальных наборов вкладок. */
function Tabs({ value, onChange, idBase }: TabsProps) {
  const pick = (index: number) => onChange(TABS[index].value);
  return (
    <div role="tablist" aria-label="Разделы" className="authoring-tabs">
      {TABS.map((tab, index) => {
        const current = tab.value === value;
        return (
          <Button
            key={tab.value}
            id={tabIds(idBase, tab.value).tab}
            role="tab"
            aria-selected={current}
            // Панель на экране одна — у выбранной вкладки; невыбранной управлять нечем.
            aria-controls={
              current ? tabIds(idBase, tab.value).panel : undefined
            }
            tabIndex={current ? 0 : -1}
            variant={current ? "primary" : "secondary"}
            onClick={() => onChange(tab.value)}
            onKeyDown={(event) => onTabKeyDown(event, index, TABS.length, pick)}
          >
            {tab.label}
          </Button>
        );
      })}
    </div>
  );
}

/** Список игр игрока, вход по коду приглашения и (создателям) новая игра. */
function GamesTab({ canCreate }: { canCreate: boolean }) {
  const api = useApi();
  const userName = useGame((state) => state.session?.user.name);
  const [creating, setCreating] = useState(false);
  const loadGames = useCallback(() => api.listGames(), [api]);
  const [games, reload] = useLoaded(loadGames);
  const joinCard = useRef<HTMLDivElement>(null);

  // Следующий шаг пустого списка — войти по коду: кнопка ведёт прямо в поле кода.
  const focusJoinCode = () => joinCard.current?.querySelector("input")?.focus();

  return (
    <>
      <Card title="Мои игры" icon="scroll">
        <p className="screen-muted">Вы вошли как {userName}.</p>
        {games.status === "loading" && <Skeleton label="Загружаем игры…" />}
        {games.status === "error" && (
          <ErrorState title="Не удалось загрузить игры" onRetry={reload}>
            <p>{games.message}</p>
            <p>Ваши игры никуда не делись.</p>
          </ErrorState>
        )}
        {games.status === "ready" && games.data.length === 0 && (
          <EmptyState
            icon="candle"
            title="Пока нет игр"
            action={
              <Button variant="secondary" onClick={focusJoinCode}>
                Ввести код приглашения
              </Button>
            }
          >
            {canCreate
              ? "Создайте игру или войдите по коду приглашения."
              : "Войдите в игру друзей по коду приглашения."}
          </EmptyState>
        )}
        {games.status === "ready" && games.data.length > 0 && (
          <ul className="games-list">
            {games.data.map((game) => (
              <GameRow key={game.id} game={game} />
            ))}
          </ul>
        )}
      </Card>

      <Card title="Войти по коду" icon="shield">
        <div ref={joinCard}>
          <JoinForm />
        </div>
      </Card>

      {canCreate &&
        (creating ? (
          <CreateGameForm onCancel={() => setCreating(false)} />
        ) : (
          <Button icon="d20" block onClick={() => setCreating(true)}>
            Создать игру
          </Button>
        ))}
    </>
  );
}

/**
 * «Мои игры». Создателям игр — ещё вкладка «Приключения» (конструктор). Право проверяет
 * сервер (403); здесь флажок сессии только прячет вкладку от остальных.
 */
export function MyGames() {
  const canCreate = useGame(
    (state) => state.session?.user.can_create_games === true,
  );
  const notice = useGame((state) => state.notice);
  const [tab, setTab] = useState<Tab>("games");
  const base = useId();

  const body =
    canCreate && tab === "adventures" ? (
      <AdventuresHome />
    ) : (
      <GamesTab canCreate={canCreate} />
    );

  return (
    <>
      {notice !== null && <Alert>{notice}</Alert>}
      {canCreate ? (
        <>
          <Tabs value={tab} onChange={setTab} idBase={base} />
          {/* Панель одна — у выбранной вкладки: невыбранный раздел не держит опросов и запросов. */}
          <TabPanel idBase={base} id={tab} selected>
            {body}
          </TabPanel>
        </>
      ) : (
        body
      )}
    </>
  );
}
