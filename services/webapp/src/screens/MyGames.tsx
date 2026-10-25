import { useCallback, useState } from "react";
import type { FormEvent } from "react";
import { useApi } from "../api/context";
import type { GamePhase, GameSummary } from "../api/types";
import { parseJoinCode } from "../auth/session";
import { useGame } from "../store/game";
import { Alert, Badge, Button, Card, EmptyState, Loading } from "../ui";
import type { BadgeProps } from "../ui";
import { CreateGameForm } from "./CreateGameForm";
import { TextField } from "./fields";
import { AdventuresHome } from "./authoring/AdventuresHome";
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

function GameRow({ game }: { game: GameSummary }) {
  const show = useGame((state) => state.show);
  const phase = PHASES[game.phase];
  return (
    <li className="games-item">
      <div className="games-item__main">
        <span className="games-item__title">{game.adventure_title}</span>
        <div className="games-item__meta">
          <Badge tone={phase.tone}>{phase.label}</Badge>
          <span>Игроков: {game.players.length}</span>
        </div>
      </div>
      <Button
        variant="secondary"
        aria-label={`Открыть: ${game.adventure_title}`}
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

function Tabs({
  value,
  onChange,
}: {
  value: Tab;
  onChange: (tab: Tab) => void;
}) {
  return (
    <div role="tablist" aria-label="Разделы" className="authoring-tabs">
      {TABS.map((tab) => (
        <Button
          key={tab.value}
          role="tab"
          aria-selected={tab.value === value}
          variant={tab.value === value ? "primary" : "secondary"}
          onClick={() => onChange(tab.value)}
        >
          {tab.label}
        </Button>
      ))}
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

  return (
    <>
      <Card title="Мои игры" icon="scroll">
        <p className="screen-muted">Вы вошли как {userName}.</p>
        {games.status === "loading" && <Loading label="Загружаем игры…" />}
        {games.status === "error" && (
          <div className="screen-stack">
            <Alert>{games.message}</Alert>
            <div>
              <Button variant="secondary" onClick={reload}>
                Повторить
              </Button>
            </div>
          </div>
        )}
        {games.status === "ready" && games.data.length === 0 && (
          <EmptyState icon="candle" title="Пока нет игр">
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
        <JoinForm />
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

  return (
    <>
      {notice !== null && <Alert>{notice}</Alert>}
      {canCreate && <Tabs value={tab} onChange={setTab} />}
      {canCreate && tab === "adventures" ? (
        <AdventuresHome />
      ) : (
        <GamesTab canCreate={canCreate} />
      )}
    </>
  );
}
