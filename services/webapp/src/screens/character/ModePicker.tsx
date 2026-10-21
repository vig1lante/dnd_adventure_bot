import { useCallback, useState } from "react";
import type { ReactNode } from "react";
import { ApiError } from "../../api/client";
import { useApi } from "../../api/context";
import type { CreationOptions, GamePhase, MyCharacter } from "../../api/types";
import { useGame } from "../../store/game";
import { Alert, Button, Card, EmptyState, Loading } from "../../ui";
import type { IconName } from "../../ui";
import { useLoaded } from "../hooks";
import type { Loaded } from "../hooks";
import "../screens.css";
import "./character.css";
import { Detailed } from "./Detailed";
import { Quick } from "./Quick";
import { Random } from "./Random";
import type { CharacterFormProps } from "./shared";

type Mode = "detailed" | "quick" | "random";

const MODES: readonly {
  id: Mode;
  label: string;
  icon: IconName;
  text: string;
}[] = [
  {
    id: "detailed",
    label: "Подробно",
    icon: "scroll",
    text: "Выберите всё сами: вид, класс, предысторию, характеристики и навыки.",
  },
  {
    id: "quick",
    label: "Быстро",
    icon: "candle",
    text: "Класс, вид и одна строка о герое. Имя и описание придумает мастер.",
  },
  {
    id: "random",
    label: "Случайно",
    icon: "d20",
    text: "Судьба бросит кубики и покажет героя. Не понравится — бросьте ещё раз.",
  },
];

/** Герой с сохранённым выбором: его форму можно открыть заполненной. */
type Editable = Extract<MyCharacter, { choices: object }>;

/** Открытая форма; `prefill` — прежний выбор («Изменить»), без него форма пустая. */
interface OpenForm {
  mode: Mode;
  prefill: Editable | null;
}

const editableOf = (hero: MyCharacter | null): Editable | null =>
  hero !== null && hero.choices !== null ? hero : null;

const modeLabel = (mode: Mode) => MODES.find((item) => item.id === mode)?.label;

function renderForm(
  { mode, prefill }: OpenForm,
  props: CharacterFormProps,
): ReactNode {
  switch (mode) {
    case "detailed":
      return (
        <Detailed
          {...props}
          initial={prefill?.mode === "detailed" ? prefill.choices : undefined}
        />
      );
    case "quick":
      return (
        <Quick
          {...props}
          initial={prefill?.mode === "quick" ? prefill.choices : undefined}
        />
      );
    case "random":
      return <Random {...props} />;
  }
}

interface ModeViewProps {
  form: OpenForm;
  gameId: string;
  options: Loaded<CreationOptions>;
  reload: () => void;
  onBack: () => void;
  onCreated: () => void;
}

/** Выбранный способ: пока правила создания не пришли, загрузка или ошибка с повтором. */
function ModeView({
  form,
  gameId,
  options,
  reload,
  onBack,
  onCreated,
}: ModeViewProps) {
  if (options.status === "loading") {
    return (
      <Card>
        <Loading label="Загружаем правила создания героя…" />
      </Card>
    );
  }
  if (options.status === "error") {
    return (
      <Card>
        <div className="screen-stack">
          <Alert>{options.message}</Alert>
          <div className="screen-actions">
            <Button onClick={reload}>Повторить</Button>
            <Button variant="secondary" onClick={onBack}>
              Назад
            </Button>
          </div>
        </div>
      </Card>
    );
  }
  return renderForm(form, { gameId, options: options.data, onBack, onCreated });
}

/** Игра ушла из лобби: героя уже не создать, а 409 после десяти шагов формы только огорчит. */
function GameStarted({ gameId, phase }: { gameId: string; phase: GamePhase }) {
  const show = useGame((state) => state.show);
  // Вплетение героев (начало игры) показывает лобби, всё остальное — экран игры.
  const inLobby = phase === "weaving";
  return (
    <Card title="Игра уже началась" icon="hourglass">
      <div className="screen-stack">
        <p>Героя можно создавать и менять только в лобби, до начала игры.</p>
        <Button
          onClick={() => show({ screen: inLobby ? "lobby" : "game", gameId })}
        >
          {inLobby ? "В лобби" : "К игре"}
        </Button>
      </div>
    </Card>
  );
}

/** Три способа создания: у каждого пустая форма. */
function ModeList({ onPick }: { onPick: (mode: Mode) => void }) {
  return (
    <ul className="char-modes">
      {MODES.map(({ id, label, icon, text }) => (
        <li key={id} className="char-mode">
          <Button block icon={icon} onClick={() => onPick(id)}>
            {label}
          </Button>
          <p>{text}</p>
        </li>
      ))}
    </ul>
  );
}

interface EditChoiceProps {
  hero: Editable;
  onEdit: () => void;
  onRestart: () => void;
}

/** У героя есть сохранённый выбор: поправить его или начать с пустой формы. */
function EditChoice({ hero, onEdit, onRestart }: EditChoiceProps) {
  return (
    <ul className="char-modes">
      <li className="char-mode">
        <Button block onClick={onEdit}>
          Изменить
        </Button>
        <p>{`Откроется форма «${modeLabel(hero.mode)}» с вашим прежним выбором.`}</p>
      </li>
      <li className="char-mode">
        <Button block variant="secondary" onClick={onRestart}>
          Создать заново
        </Button>
        <p>Выберите любой способ и начните с пустой формы.</p>
      </li>
    </ul>
  );
}

/** Почему форма героя без сохранённого выбора пустая: случайный герой или игра до обновления. */
const noChoicesNote = (hero: MyCharacter) =>
  hero.mode === "random"
    ? "Этот герой создан случайно, поэтому прежнего выбора нет: формы откроются пустыми."
    : "Этот герой создан до обновления игры, прежний выбор не сохранился: формы откроются пустыми.";

interface StartProps {
  hero: MyCharacter | null;
  /** Игрок нажал «Создать заново»: показаны три способа с пустыми формами. */
  fresh: boolean;
  onOpen: (form: OpenForm) => void;
  onFresh: (fresh: boolean) => void;
}

/** Первый экран: у нового игрока три способа, у героя — «Изменить» и «Создать заново». */
function Start({ hero, fresh, onOpen, onFresh }: StartProps) {
  const editable = editableOf(hero);
  const choosingMode = editable === null || fresh;
  return (
    <Card
      title={hero === null ? "Создание героя" : "Изменить героя"}
      icon="shield"
    >
      <div className="screen-stack">
        {hero !== null && (
          <p>
            У вас уже есть герой. Новый заменит его, а отметка «Готов» снимется.
          </p>
        )}
        {hero !== null && editable === null && <p>{noChoicesNote(hero)}</p>}
        {choosingMode ? (
          <ModeList onPick={(mode) => onOpen({ mode, prefill: null })} />
        ) : (
          <EditChoice
            hero={editable}
            onEdit={() => onOpen({ mode: editable.mode, prefill: editable })}
            onRestart={() => onFresh(true)}
          />
        )}
        {editable !== null && fresh && (
          <div className="screen-actions">
            <Button variant="secondary" onClick={() => onFresh(false)}>
              Назад
            </Button>
          </div>
        )}
      </div>
    </Card>
  );
}

/**
 * Героя не удалось получить (не 404). Игрока это не запирает: ниже три способа с пустыми
 * формами, как будто героя нет, а сервер при создании сам заменит прежнего.
 */
function HeroFailed({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) {
  return (
    <Card>
      <div className="screen-stack">
        <Alert>{message}</Alert>
        <p>
          Прежний выбор получить не удалось, поэтому формы откроются пустыми.
          Если герой уже есть, новый заменит его.
        </p>
        <div className="screen-actions">
          <Button variant="secondary" onClick={onRetry}>
            Повторить
          </Button>
        </div>
      </div>
    </Card>
  );
}

function Picker({ gameId }: { gameId: string }) {
  const api = useApi();
  const show = useGame((state) => state.show);
  const [form, setForm] = useState<OpenForm | null>(null);
  const [fresh, setFresh] = useState(false);

  // Правила создания грузятся один раз на экран, а не на каждый способ.
  const loadOptions = useCallback(() => api.getCreationOptions(), [api]);
  const [options, reloadOptions] = useLoaded(loadOptions);
  // Нужна только фаза игры: на сбое экран не ломается, героя проверит сам сервер.
  const loadGame = useCallback(() => api.getGame(gameId), [api, gameId]);
  const [game] = useLoaded(loadGame);
  // Свой герой и его прежний выбор. 404 — героя ещё нет: это обычное создание, а не сбой.
  const loadHero = useCallback(
    () =>
      api.myCharacter(gameId).catch((failure: unknown) => {
        if (failure instanceof ApiError && failure.status === 404) return null;
        throw failure;
      }),
    [api, gameId],
  );
  const [hero, reloadHero] = useLoaded(loadHero);

  const toLobby = () => show({ screen: "lobby", gameId });

  // Сведения об игре уже загружены, поэтому о старте игры знаем до первого шага формы.
  if (game.status === "ready" && game.data.phase !== "lobby") {
    return <GameStarted gameId={gameId} phase={game.data.phase} />;
  }

  return (
    <>
      <div>
        <Button variant="secondary" onClick={toLobby}>
          Назад в лобби
        </Button>
      </div>
      {hero.status === "loading" && (
        <Card>
          <Loading label="Загружаем вашего героя…" />
        </Card>
      )}
      {hero.status === "error" && form === null && (
        <HeroFailed message={hero.message} onRetry={reloadHero} />
      )}
      {hero.status !== "loading" &&
        (form === null ? (
          <Start
            hero={hero.status === "ready" ? hero.data : null}
            fresh={fresh}
            onOpen={setForm}
            onFresh={setFresh}
          />
        ) : (
          <ModeView
            form={form}
            gameId={gameId}
            options={options}
            reload={reloadOptions}
            onBack={() => setForm(null)}
            onCreated={toLobby}
          />
        ))}
    </>
  );
}

/** Экран создания героя: выбор способа, затем сам способ; готового героя ждёт лобби. */
export function ModePicker() {
  const gameId = useGame((state) => state.gameId);
  const show = useGame((state) => state.show);
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
  return <Picker gameId={gameId} />;
}
