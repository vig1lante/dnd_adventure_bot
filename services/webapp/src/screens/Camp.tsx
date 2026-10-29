import { useId, useState } from "react";
import { useApi } from "../api/context";
import type { Character, FeedItem, RestKind, RestView } from "../api/types";
import { CONNECTION_LOST_NOTE, connectionLost } from "../components/access";
import { Feed } from "../components/Feed";
import { restLabel } from "../components/phases";
import type { GameView, LiveConnection } from "../store/game";
import { useGame } from "../store/game";
import { useMotionClass } from "../theme/motion";
import { Alert, Button, Card, SealedNote } from "../ui";
import { classNames } from "../ui/classNames";
import { TextAreaField } from "./fields";
import { useAction } from "./hooks";
import "../components/game.css";

// Пределы сервера (dm/camp_flow.py): знаков в реплике и реплик одного героя за привал. Клиент
// лишь не предлагает лишнего; отказ всё равно приходит от сервера и показывается его текстом.
const CAMP_LINE_MAX_CHARS = 400;
const CAMP_LINES_PER_HERO = 10;
// Как DEATH_SAVE_FAILURES_TO_DIE движка: павшему герою кнопку кости хитов не предлагаем.
// Решает всё равно сервер (409 «герой мёртв»).
const DEATH_SAVE_FAILURES_TO_DIE = 3;

const REST_NOTES: Record<RestKind, string> = {
  short:
    "Час отдыха: перевяжите раны и потратьте кости хитов. В конце мастер коротко расскажет, что изменилось.",
  long: "Ночь у костра: утром отряд полностью отдохнёт, а мастер расскажет, каким выдалось утро.",
};

/**
 * Тема привала и реплики этого привала. Темой служит последний рассказ мастера: это он
 * разбил лагерь. Реплики прежних привалов остались в ленте раньше этого рассказа.
 */
function sinceLastNarration(feed: readonly FeedItem[]) {
  // Цикл вместо findLastIndex: в старых WebView Telegram этого метода массива ещё нет.
  let last = feed.length - 1;
  while (last >= 0 && feed[last].kind !== "narration") last -= 1;
  return {
    topic: last < 0 ? null : feed[last],
    lines: feed.slice(last + 1).filter((item) => item.kind === "camp_line"),
  };
}

/**
 * Почему сейчас нельзя говорить у костра; `null` — можно. Обрыв связи называется первым, как и у
 * поля хода: за время обрыва пауза и привал на экране могли устареть.
 */
function lockReason(
  view: GameView,
  said: number,
  connection: LiveConnection,
): string | null {
  if (connectionLost(connection)) return CONNECTION_LOST_NOTE;
  if (view.phase.phase === "paused") {
    return "Игра на паузе: слово — после «Продолжить».";
  }
  if (view.phase.phase !== "playing") return "Игра сейчас не идёт.";
  if (view.character === null) return "У вас нет героя в этой игре.";
  if (said >= CAMP_LINES_PER_HERO) {
    return "Вы сказали у костра достаточно: подождите утра.";
  }
  return null;
}

/** Реплики у костра в порядке ленты; новые появляются мягко, уже бывшие на экране — сразу. */
function CampLines({ lines }: { lines: readonly FeedItem[] }) {
  const [shown] = useState(() => new Set(lines.map((line) => line.id)));
  const appear = useMotionClass("ui-anim-appear");
  if (lines.length === 0) {
    return <p className="camp__quiet">Пока тихо: скажите слово первым.</p>;
  }
  return (
    <ul className="camp-lines" aria-label="Реплики у костра">
      {lines.map((line) => {
        const actor = line.data?.actor;
        return (
          <li
            key={line.id}
            className={classNames("feed-line", !shown.has(line.id) && appear)}
          >
            {typeof actor === "string" && <strong>{`${actor}:`}</strong>}
            {` «${line.text}»`}
          </li>
        );
      })}
    </ul>
  );
}

/** Почему кость хитов сейчас не потратить; `null` — можно. */
function hitDieLock(character: Character): string | null {
  if (character.death_saves.failures >= DEATH_SAVE_FAILURES_TO_DIE) {
    return "Герой погиб: кости хитов ему уже не помогут.";
  }
  if (character.hit_dice === 0) {
    return "Костей хитов не осталось: они вернутся после долгого отдыха.";
  }
  return null;
}

/**
 * «Потратить кость хитов»: бросок и лечение делает сервер, экран показывает его остаток и хиты.
 * Ответ кладётся в лист героя сразу; событие `character` от сервера приносит те же числа.
 */
function HitDice({
  gameId,
  character,
}: {
  gameId: string;
  character: Character;
}) {
  const api = useApi();
  const setHitDie = useGame((state) => state.setHitDie);
  const { busy, error, run } = useAction();
  const noteId = useId();
  const locked = hitDieLock(character);

  const spend = () => run(async () => setHitDie(await api.spendHitDie(gameId)));

  return (
    <div className="camp__stack">
      <p className="camp__count">{`Хиты: ${character.hp} из ${character.hp_max}.`}</p>
      {locked !== null && (
        <p id={noteId} className="camp__note">
          {locked}
        </p>
      )}
      {error !== null && <Alert>{error}</Alert>}
      <Button
        busy={busy}
        disabled={locked !== null}
        aria-describedby={locked === null ? undefined : noteId}
        onClick={() => void spend()}
      >
        {`Потратить кость хитов (осталось ${character.hit_dice})`}
      </Button>
    </div>
  );
}

/** Вид привала и что он даёт; на коротком — кость хитов своего героя. */
function RestSummary({
  gameId,
  rest,
  character,
}: {
  gameId: string;
  rest: RestView;
  character: Character | null;
}) {
  return (
    <Card title={restLabel(rest.kind)} icon="hourglass">
      <div className="camp__stack">
        <p className="camp__note">{REST_NOTES[rest.kind]}</p>
        {rest.kind === "short" && character !== null && (
          <HitDice gameId={gameId} character={character} />
        )}
      </div>
    </Card>
  );
}

/** Идеи «расскажи о себе» для своего героя: их присылают только адресату, видит только он. */
function StoryIdeas({ ideas }: { ideas: readonly string[] }) {
  if (ideas.length === 0) return null;
  return (
    <Card title="Идея для рассказа" icon="seal">
      <div className="camp__stack">
        <SealedNote>
          {ideas.map((idea) => (
            <p key={idea}>{idea}</p>
          ))}
        </SealedNote>
        <p className="camp__note">
          Если захотите, расскажите об этом репликой у костра.
        </p>
      </div>
    </Card>
  );
}

interface CampfireFormProps {
  gameId: string;
  /** Вид привала; `null` — ещё не пришёл. */
  kind: RestKind | null;
  /** Сколько реплик герой уже сказал за этот привал. */
  said: number;
  locked: string | null;
}

// Когда мастер ответит на реплику: долгий привал кончается утром, короткий — через час. Пока вид
// привала не пришёл, «утра» не обещаем.
const ANSWER_HINTS: Record<RestKind | "unknown", string> = {
  short: "Реплику услышат все, а мастер ответит на неё в конце привала.",
  long: "Реплику услышат все, а мастер ответит на неё утром.",
  unknown: "Реплику услышат все, а мастер ответит на неё в конце привала.",
};

/** Поле «Сказать у костра»: одна реплика за раз, без лишних пробелов, с текстом отказа сервера. */
function CampfireForm({ gameId, kind, said, locked }: CampfireFormProps) {
  const api = useApi();
  const receive = useGame((state) => state.receive);
  const { busy, error, run } = useAction();
  const [text, setText] = useState("");
  const line = text.trim();

  const send = async () => {
    const sent = text;
    const ok = await run(async () => {
      const item = await api.sendCampLine(gameId, line);
      // Ту же запись пришлёт и сокет: хранилище не добавляет запись с тем же id дважды.
      receive({ type: "feed_item", item });
    });
    // Пока шёл запрос, игрок мог начать следующую реплику: её не стираем.
    if (ok) setText((now) => (now === sent ? "" : now));
  };

  return (
    <div className="camp__stack">
      <TextAreaField
        label="Сказать у костра"
        hint={ANSWER_HINTS[kind ?? "unknown"]}
        rows={3}
        maxLength={CAMP_LINE_MAX_CHARS}
        value={text}
        disabled={locked !== null}
        onChange={(event) => setText(event.target.value)}
      />
      <p className="camp__count">
        {`Знаков: ${text.length} из ${CAMP_LINE_MAX_CHARS}. Ваших реплик: ${said} из ${CAMP_LINES_PER_HERO}.`}
      </p>
      {locked !== null && (
        <p role="status" className="camp__note">
          {locked}
        </p>
      )}
      {error !== null && <Alert>{error}</Alert>}
      <Button
        busy={busy}
        disabled={locked !== null || line === ""}
        onClick={() => void send()}
      >
        Сказать
      </Button>
    </div>
  );
}

export interface CampProps {
  gameId: string;
  view: GameView;
}

/**
 * Экран привала любого вида: вид привала (на коротком — кость хитов своего героя), своя идея для
 * рассказа, последний рассказ мастера как тема, реплики героев и поле «Сказать у костра».
 * Отсчёт привала и кнопки хоста («+1 мин», «Закончить привал») живут выше, в строке статуса и
 * панели хоста: они нужны с любой вкладки. Реплики этого привала присылает сервер в блоке
 * привала, новые приходят событиями ленты; ответ на свою реплику кладётся туда же сразу. Пока
 * вид привала не пришёл, реплики берутся из ленты после рассказа мастера, как в DND-1. При
 * обрыве связи поле закрыто с тем же пояснением, что и поле хода, и открывается само, когда
 * связь вернётся.
 */
export function Camp({ gameId, view }: CampProps) {
  const topicTitle = useId();
  const connection = useGame((state) => state.connection);
  const { rest } = view;
  const recent = sinceLastNarration(view.feed);
  const topic = recent.topic;
  const lines = rest?.lines ?? recent.lines;
  const heroId = view.character?.id ?? null;
  const said = lines.filter((item) => item.data?.actor_id === heroId).length;

  return (
    <>
      {rest !== null && (
        <>
          <RestSummary gameId={gameId} rest={rest} character={view.character} />
          <StoryIdeas ideas={rest.my_ideas} />
        </>
      )}
      {topic !== null && (
        <section aria-labelledby={topicTitle} className="camp__topic">
          <h2 id={topicTitle} className="camp__heading">
            Последний рассказ мастера
          </h2>
          <Feed items={[topic]} />
        </section>
      )}
      <Card title="Что говорят у костра" icon="candle">
        <CampLines lines={lines} />
      </Card>
      <Card title="Ваша реплика" icon="scroll">
        <CampfireForm
          gameId={gameId}
          kind={rest?.kind ?? null}
          said={said}
          locked={lockReason(view, said, connection)}
        />
      </Card>
    </>
  );
}
