import { useCallback, useEffect, useState } from "react";
import { useApi } from "../api/context";
import type { GameSummary, PlayerSummary } from "../api/types";
import { connectionLost } from "../components/access";
import { ReconnectingNote } from "../components/StatusBar";
import { RETRY_PATIENCE_MS, useStaysFor } from "../components/waiting";
import { followGame, gameStage, leavingGame, useGame } from "../store/game";
import type { GameView } from "../store/game";
import {
  Alert,
  Avatar,
  Badge,
  Button,
  Card,
  EmptyState,
  Loading,
  Panel,
} from "../ui";
import type { BadgeProps } from "../ui";
import { Switch } from "./fields";
import { useAction, useLoaded } from "./hooks";
import {
  canShareInTelegram,
  copyText,
  inviteLink,
  shareInTelegram,
} from "./invite";
import "./screens.css";

// Фаза хода в начале игры: после `phase`-события верна она, до него — сводка игры.
function weavingTurnPhase(view: GameView): GameSummary["turn_phase"] {
  return view.phase.phase === "weaving"
    ? view.phase.turn_phase
    : view.game.turn_phase;
}

const isReady = (player: PlayerSummary) => player.ready && player.has_character;

function statusOf(player: PlayerSummary): {
  label: string;
  tone: BadgeProps["tone"];
} {
  if (isReady(player)) return { label: "Готов", tone: "cloth" };
  if (player.has_character)
    return { label: "Персонаж создан", tone: "neutral" };
  return { label: "Создаёт персонажа", tone: "amber" };
}

// --- вплетение ---

interface WeavingProps {
  gameId: string;
  game: GameSummary;
  turnPhase: GameSummary["turn_phase"];
  isHost: boolean;
}

function Weaving({ gameId, game, turnPhase, isHost }: WeavingProps) {
  const api = useApi();
  const { busy, error, run } = useAction();
  const failed = turnPhase === "dm_failed";
  const waitedLong = useStaysFor(
    failed ? null : (turnPhase ?? "none"),
    RETRY_PATIENCE_MS,
  );
  const canRetry = isHost && (failed || waitedLong);

  return (
    <Card title={game.adventure_title} icon="scroll">
      <div role="status">
        <EmptyState
          icon="candle"
          title="Мастер связывает ваши истории с миром…"
          titleAs="h3"
        >
          Это займёт немного времени: игра начнётся сама.
        </EmptyState>
      </div>
      <div className="screen-stack">
        {failed && (
          <Alert>
            Мастер не справился с началом игры.
            {isHost ? " Попробуйте ещё раз." : " Хост может повторить попытку."}
          </Alert>
        )}
        {!failed && waitedLong && isHost && (
          <p className="screen-muted">Мастер думает дольше обычного.</p>
        )}
        {error !== null && <Alert>{error}</Alert>}
        {canRetry && (
          <Button
            busy={busy}
            block
            onClick={() => void run(() => api.hostAction(gameId, "retry"))}
          >
            Повторить
          </Button>
        )}
      </div>
    </Card>
  );
}

// --- приглашение ---

function InviteCard({ game }: { game: GameSummary }) {
  const api = useApi();
  const loadConfig = useCallback(() => api.getConfig(), [api]);
  const [config] = useLoaded(loadConfig);
  const [note, setNote] = useState<string | null>(null);

  // Без настроенного бота ссылки нет, и друзьям отправляется сам код.
  const link =
    config.status === "ready"
      ? inviteLink(config.data.bot_username, game.code)
      : null;

  const copyInvite = async () => {
    const copied = await copyText(link ?? game.code);
    if (!copied) {
      setNote("Не удалось скопировать: выделите ссылку или код вручную.");
    } else {
      setNote(
        link !== null
          ? "Ссылка скопирована. Отправьте её друзьям."
          : "Код скопирован. Отправьте его друзьям.",
      );
    }
  };

  const shareInChat = (shareLink: string) => {
    try {
      shareInTelegram(
        shareLink,
        `Присоединяйся к игре «${game.adventure_title}»!`,
      );
    } catch {
      setNote("Не удалось открыть Telegram: скопируйте ссылку.");
    }
  };

  return (
    <Card title="Позовите друзей" icon="candle">
      <div className="screen-stack">
        <p>
          Код приглашения:{" "}
          <span className="lobby-invite__code">{game.code}</span>
        </p>
        {link !== null && <p className="lobby-invite__link">{link}</p>}
        <div className="screen-actions">
          <Button
            disabled={config.status === "loading"}
            onClick={() => void copyInvite()}
          >
            Пригласить
          </Button>
          {link !== null && canShareInTelegram() && (
            <Button variant="secondary" onClick={() => shareInChat(link)}>
              Отправить в Telegram
            </Button>
          )}
        </div>
        {note !== null && (
          <p role="status" className="screen-muted">
            {note}
          </p>
        )}
      </div>
    </Card>
  );
}

// --- игроки ---

interface PlayerRowProps {
  player: PlayerSummary;
  isMe: boolean;
  /** Хост может исключить этого игрока. */
  canKick: boolean;
  confirming: boolean;
  busy: boolean;
  onAskKick: () => void;
  onCancelKick: () => void;
  onKick: () => void;
}

function PlayerRow({
  player,
  isMe,
  canKick,
  confirming,
  busy,
  onAskKick,
  onCancelKick,
  onKick,
}: PlayerRowProps) {
  const status = statusOf(player);
  return (
    <li className="lobby-player">
      <Avatar name={player.name} decorative />
      <span className="lobby-player__name">{player.name}</span>
      {isMe && <span className="screen-muted">(вы)</span>}
      <span className="lobby-player__tags">
        {player.is_host && <Badge tone="amber">Хост</Badge>}
        <Badge tone={status.tone}>{status.label}</Badge>
        {canKick && !confirming && (
          <Button
            variant="secondary"
            aria-label={`Исключить ${player.name}`}
            disabled={busy}
            onClick={onAskKick}
          >
            Исключить
          </Button>
        )}
      </span>
      {canKick && confirming && (
        <div className="lobby-confirm">
          <p>Исключить игрока {player.name}? Его герой будет удалён.</p>
          <div className="screen-actions">
            <Button variant="danger" busy={busy} onClick={onKick}>
              Да, исключить
            </Button>
            <Button variant="secondary" onClick={onCancelKick}>
              Отмена
            </Button>
          </div>
        </div>
      )}
    </li>
  );
}

// --- лобби ---

function LobbyBody({ gameId, game }: { gameId: string; game: GameSummary }) {
  const api = useApi();
  const userId = useGame((state) => state.session?.user.id);
  const show = useGame((state) => state.show);
  const { busy, error, run } = useAction();
  const [kickingId, setKickingId] = useState<string | null>(null);
  const [leaving, setLeaving] = useState(false);

  const loadAdventures = useCallback(() => api.listAdventures(), [api]);
  const [adventures] = useLoaded(loadAdventures);
  const pitch =
    adventures.status === "ready"
      ? adventures.data.find((item) => item.id === game.adventure_id)?.pitch
      : undefined;

  const { players } = game;
  const me = players.find((player) => player.user_id === userId);
  const isHost = me?.is_host === true;
  const waiting = players.filter((player) => !isReady(player));

  const openCharacter = () => show({ screen: "create", gameId });

  const kick = (target: PlayerSummary) => {
    setKickingId(null);
    void run(() => api.kickPlayer(gameId, target.user_id));
  };

  const leave = () =>
    void run(async () => {
      // Сервер закроет сокет выходящего раньше ответа: сообщение «Игра недоступна» не нужно.
      await leavingGame(gameId, () => api.leaveGame(gameId));
      show({ screen: "games" });
    });

  return (
    <>
      {error !== null && <Alert>{error}</Alert>}
      <Card title={game.adventure_title} icon="scroll">
        {pitch !== undefined && <p>{pitch}</p>}
      </Card>

      {isHost && <InviteCard game={game} />}

      <Card title={`Игроки (${players.length})`} icon="shield">
        <ul className="lobby-players">
          {players.map((player) => (
            <PlayerRow
              key={player.user_id}
              player={player}
              isMe={player.user_id === userId}
              canKick={isHost && !player.is_host}
              confirming={kickingId === player.user_id}
              busy={busy}
              onAskKick={() => setKickingId(player.user_id)}
              onCancelKick={() => setKickingId(null)}
              onKick={() => kick(player)}
            />
          ))}
        </ul>
      </Card>

      {me !== undefined && (
        <Card title="Ваш герой" icon="heart">
          <div className="screen-stack">
            <Button
              variant={me.has_character ? "secondary" : "primary"}
              onClick={openCharacter}
            >
              {me.has_character ? "Изменить" : "Создать персонажа"}
            </Button>
            <Switch
              label="Готов"
              hint={me.has_character ? undefined : "Сначала создайте героя."}
              checked={me.ready}
              disabled={!me.has_character || busy}
              onChange={(ready) => void run(() => api.setReady(gameId, ready))}
            />
          </div>
        </Card>
      )}

      {isHost && (
        <Card title="Начало игры" icon="hourglass">
          <div className="screen-stack">
            <Button
              block
              busy={busy}
              disabled={waiting.length > 0}
              onClick={() => void run(() => api.startGame(gameId))}
            >
              Начать
            </Button>
            {waiting.length > 0 && (
              <p className="screen-muted">
                Ждём готовности:{" "}
                {waiting.map((player) => player.name).join(", ")}.
              </p>
            )}
          </div>
        </Card>
      )}

      {me !== undefined &&
        !isHost &&
        (leaving ? (
          <Card>
            <p>Выйти из лобби? Ваш герой будет удалён.</p>
            <div className="screen-actions">
              <Button variant="danger" busy={busy} onClick={leave}>
                Да, выйти
              </Button>
              <Button variant="secondary" onClick={() => setLeaving(false)}>
                Остаться
              </Button>
            </div>
          </Card>
        ) : (
          <Button variant="secondary" block onClick={() => setLeaving(true)}>
            Выйти из лобби
          </Button>
        ))}
    </>
  );
}

/**
 * Лобби игры: состав и готовность игроков, приглашение и старт у хоста, свой герой
 * и выход у игрока. Состояние приходит по сокету (снимок и события `lobby`/`phase`);
 * после «Начать» экран показывает вплетение, а когда игра пошла, открывает экран игры.
 * Обрыв связи показывает липкая плашка «Переподключаемся…»: список игроков длинный, и
 * хост, прокрутивший его до «Начать», должен видеть обрыв там же.
 */
export function Lobby() {
  const gameId = useGame((state) => state.gameId);
  const view = useGame((state) => state.live.view);
  const liveError = useGame((state) => state.live.error);
  const connection = useGame((state) => state.connection);
  const userId = useGame((state) => state.session?.user.id);
  const show = useGame((state) => state.show);
  const isHost =
    view?.game.players.find((player) => player.user_id === userId)?.is_host ===
    true;

  useEffect(() => {
    if (gameId === null) return;
    return followGame(gameId);
  }, [gameId]);

  const stage = view === null ? null : gameStage(view);
  useEffect(() => {
    if (stage === "game" && gameId !== null) show({ screen: "game", gameId });
  }, [stage, gameId, show]);

  return (
    <>
      <div>
        <Button variant="secondary" onClick={() => show({ screen: "games" })}>
          К моим играм
        </Button>
      </div>
      {connectionLost(connection) && (
        <Panel className="screen-sticky">
          <ReconnectingNote />
        </Panel>
      )}
      {liveError !== null && <Alert>{liveError}</Alert>}
      {view === null && <Loading label="Подключаемся к лобби…" />}
      {view !== null && gameId !== null && stage === "lobby" && (
        <LobbyBody gameId={gameId} game={view.game} />
      )}
      {view !== null && gameId !== null && stage === "weaving" && (
        <Weaving
          gameId={gameId}
          game={view.game}
          turnPhase={weavingTurnPhase(view)}
          isHost={isHost}
        />
      )}
      {stage === "game" && <Loading label="Открываем игру…" />}
    </>
  );
}
