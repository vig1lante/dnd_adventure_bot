import { useId, useState } from "react";
import { useApi } from "../api/context";
import type { FeedItem } from "../api/types";
import { CONNECTION_LOST_NOTE, connectionLost } from "../components/access";
import { Feed } from "../components/Feed";
import type { GameView, LiveConnection } from "../store/game";
import { useGame } from "../store/game";
import { useMotionClass } from "../theme/motion";
import { Alert, Button, Card } from "../ui";
import { classNames } from "../ui/classNames";
import { TextAreaField } from "./fields";
import { useAction } from "./hooks";
import "../components/game.css";

// Пределы сервера (dm/camp_flow.py): знаков в реплике и реплик одного героя за привал. Клиент
// лишь не предлагает лишнего; отказ всё равно приходит от сервера и показывается его текстом.
const CAMP_LINE_MAX_CHARS = 400;
const CAMP_LINES_PER_HERO = 10;

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

interface CampfireFormProps {
  gameId: string;
  /** Сколько реплик герой уже сказал за этот привал. */
  said: number;
  locked: string | null;
}

/** Поле «Сказать у костра»: одна реплика за раз, без лишних пробелов, с текстом отказа сервера. */
function CampfireForm({ gameId, said, locked }: CampfireFormProps) {
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
        hint="Реплику услышат все, а мастер ответит на неё утром."
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
 * Экран привала: последний рассказ мастера как тема, реплики героев и поле «Сказать у костра».
 * Отсчёт привала и кнопки хоста («+1 мин», «Закончить привал») живут выше, в строке статуса и
 * панели хоста: они нужны с любой вкладки. Реплики приходят в ленту событиями; ответ на свою
 * реплику кладётся туда же сразу. При обрыве связи поле закрыто с тем же пояснением, что и
 * поле хода, и открывается само, когда связь вернётся.
 */
export function Camp({ gameId, view }: CampProps) {
  const topicTitle = useId();
  const connection = useGame((state) => state.connection);
  const { topic, lines } = sinceLastNarration(view.feed);
  const heroId = view.character?.id ?? null;
  const said = lines.filter((item) => item.data?.actor_id === heroId).length;

  return (
    <>
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
          said={said}
          locked={lockReason(view, said, connection)}
        />
      </Card>
    </>
  );
}
