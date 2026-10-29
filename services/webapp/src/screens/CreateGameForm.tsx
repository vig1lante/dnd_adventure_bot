import { useCallback, useState } from "react";
import type { FormEvent } from "react";
import { useApi } from "../api/context";
import type {
  AdventureSummary,
  CreateGameBody,
  ProviderOption,
} from "../api/types";
import { useGame } from "../store/game";
import { Alert, Button, Card, Loading } from "../ui";
import {
  ChoiceList,
  ErrorAlert,
  SelectField,
  Switch,
  TextField,
} from "./fields";
import type { Choice } from "./fields";
import { useAction, useLoaded } from "./hooks";
import "./screens.css";

// Границы длительностей — как в схеме сервера (games/schemas.py). Это подсказка игроку:
// окончательно значения проверяет сервер.
const DURATIONS = [
  { key: "collect_seconds", label: "Сбор ходов, сек.", min: 30, max: 600 },
  { key: "hurry_seconds", label: "«Поторопить», сек.", min: 15, max: 300 },
  { key: "camp_seconds", label: "Долгий привал, сек.", min: 30, max: 900 },
  {
    key: "short_rest_seconds",
    label: "Короткий привал, сек.",
    min: 30,
    max: 600,
  },
] as const;

type DurationKey = (typeof DURATIONS)[number]["key"];
type Durations = Record<DurationKey, string>;

const NO_DURATIONS: Durations = {
  collect_seconds: "",
  hurry_seconds: "",
  camp_seconds: "",
  short_rest_seconds: "",
};

function durationError(text: string, min: number, max: number): string | null {
  const trimmed = text.trim();
  if (trimmed === "") return null; // не задано — сервер возьмёт значение по умолчанию
  const value = /^\d+$/.test(trimmed) ? Number(trimmed) : Number.NaN;
  return value >= min && value <= max
    ? null
    : `От ${min} до ${max} секунд, целым числом.`;
}

const firstOpenAdventure = (adventures: readonly AdventureSummary[]) =>
  adventures.find((adventure) => !adventure.blocking)?.id ?? null;

// Заранее выбран только провайдер, который владелец сервера назвал по умолчанию, и только если
// он настроен. Иначе не выбрано ничего: первый попавшийся мог бы оказаться живой моделью
// (`claude_code_cli` по подписке владельца), а игрок этого не просил.
const preselectedProvider = (providers: readonly ProviderOption[]) =>
  providers.find((provider) => provider.is_default && provider.available)?.id ??
  null;

// Провайдер ведёт игру живой моделью по подписке владельца этого компьютера. Узнаётся по id,
// а не по названию: название сервер может поменять.
const LIVE_MODEL_PROVIDER = "claude_code_cli";
const LIVE_MODEL_NOTE =
  "Живая модель по подписке на этом компьютере (только dev).";

const hoursText = (hours: number) => `≈ ${String(hours).replace(".", ",")} ч`;

function adventureChoice(adventure: AdventureSummary): Choice {
  return {
    value: adventure.id,
    label: adventure.title,
    disabled: adventure.blocking,
    description: (
      <>
        <span>{adventure.pitch}</span>
        <span>{hoursText(adventure.target_hours)}</span>
        {adventure.blocking && (
          <span>
            Нельзя выбрать: в приключении есть блокирующие ошибки. Балл
            проверки: {adventure.score}.
          </span>
        )}
      </>
    ),
  };
}

function providerNote(provider: ProviderOption): string | undefined {
  if (!provider.available) return "Не настроен на сервере.";
  if (provider.id === LIVE_MODEL_PROVIDER) return LIVE_MODEL_NOTE;
  return provider.default_model === null
    ? undefined
    : `Модель по умолчанию: ${provider.default_model}`;
}

function providerChoice(provider: ProviderOption): Choice {
  return {
    value: provider.id,
    label: provider.title,
    disabled: !provider.available,
    description: providerNote(provider),
  };
}

const MODEL_HINT = "Можно оставить как есть: возьмётся модель по умолчанию.";

interface ModelFieldProps {
  provider: ProviderOption;
  value: string;
  onChange: (model: string) => void;
}

// Claude Code на этом компьютере принимает модель только из списка сервера (псевдонимы CLI
// и CLAUDE_CLI_MODEL), поэтому у него выбор из списка; остальным можно вписать любое имя.
function ModelField({ provider, value, onChange }: ModelFieldProps) {
  if (provider.id === LIVE_MODEL_PROVIDER) {
    return (
      <SelectField
        label="Модель"
        hint={MODEL_HINT}
        // Пустой вариант нужен, только когда модели по умолчанию нет: тогда её выбирает сам CLI.
        placeholder={
          provider.default_model === null ? "По умолчанию" : undefined
        }
        value={value}
        options={provider.models.map((name) => ({ value: name, label: name }))}
        onChange={onChange}
      />
    );
  }
  return (
    <TextField
      label="Модель"
      value={value}
      placeholder={provider.default_model ?? undefined}
      hint={MODEL_HINT}
      autoComplete="off"
      autoCapitalize="none"
      spellCheck={false}
      onChange={(event) => onChange(event.target.value)}
    />
  );
}

interface FormInput {
  adventureId: string;
  provider: ProviderOption;
  model: string;
  profanity: boolean;
  durations: Durations;
}

// На сервер уходит только отличающееся от его настроек: провайдер по умолчанию, модель
// по умолчанию, включённый мат и незаданные длительности остаются за сервером.
function createBody({
  adventureId,
  provider,
  model,
  profanity,
  durations,
}: FormInput): CreateGameBody {
  const body: CreateGameBody = { adventure_id: adventureId };
  if (!provider.is_default) body.provider = provider.id;
  if (!profanity) body.profanity = false;
  const modelName = model.trim();
  if (
    provider.accepts_model &&
    modelName !== "" &&
    modelName !== provider.default_model
  ) {
    body.model = modelName;
  }
  for (const { key } of DURATIONS) {
    const text = durations[key].trim();
    if (text !== "") body[key] = Number(text);
  }
  return body;
}

interface OptionsFormProps {
  adventures: AdventureSummary[];
  providers: ProviderOption[];
  onCancel: () => void;
}

function OptionsForm({ adventures, providers, onCancel }: OptionsFormProps) {
  const api = useApi();
  const show = useGame((state) => state.show);
  const { busy, error, problems, run } = useAction();

  const [adventureId, setAdventureId] = useState(() =>
    firstOpenAdventure(adventures),
  );
  const [providerId, setProviderId] = useState(() =>
    preselectedProvider(providers),
  );
  // Правка модели хранится по провайдерам: у каждого свои имена моделей.
  const [models, setModels] = useState<Record<string, string>>({});
  const [profanity, setProfanity] = useState(true);
  const [durations, setDurations] = useState<Durations>(NO_DURATIONS);

  const provider = providers.find((item) => item.id === providerId) ?? null;
  const model =
    provider === null
      ? ""
      : (models[provider.id] ?? provider.default_model ?? "");
  const errors = DURATIONS.map(({ key, min, max }) =>
    durationError(durations[key], min, max),
  );
  const ready =
    adventureId !== null &&
    provider !== null &&
    errors.every((message) => message === null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (adventureId === null || provider === null || !ready || busy) return;
    await run(async () => {
      const game = await api.createGame(
        createBody({ adventureId, provider, model, profanity, durations }),
      );
      show({ screen: "lobby", gameId: game.id });
    });
  };

  const noOpenAdventures =
    adventures.length > 0 && firstOpenAdventure(adventures) === null;

  return (
    <form className="screen-stack" onSubmit={(event) => void submit(event)}>
      {adventures.length === 0 ? (
        <p className="screen-muted">На сервере нет приключений.</p>
      ) : (
        <ChoiceList
          legend="Приключение"
          name="adventure"
          choices={adventures.map(adventureChoice)}
          value={adventureId}
          onChange={setAdventureId}
        />
      )}
      {noOpenAdventures && (
        <p className="screen-muted">
          Нет приключений, которые можно использовать: во всех есть блокирующие
          ошибки.
        </p>
      )}

      {providers.length > 0 && (
        <ChoiceList
          legend="Нейронка для мастера"
          name="provider"
          choices={providers.map(providerChoice)}
          value={providerId}
          onChange={setProviderId}
        />
      )}
      {provider === null && (
        <p className="screen-muted">
          {providers.some((item) => item.available)
            ? "Выберите нейронку для мастера: настроенной по умолчанию нет."
            : "Нет доступных нейронок: проверьте настройки сервера."}
        </p>
      )}
      {provider?.accepts_model === true && (
        <ModelField
          provider={provider}
          value={model}
          onChange={(value) => setModels({ ...models, [provider.id]: value })}
        />
      )}
      {provider !== null && !provider.accepts_model && (
        <p className="screen-muted">Этот вариант сам выбирает модель.</p>
      )}

      <Switch
        label="Мат и грубая речь"
        hint="Мастер и NPC могут материться, но не в адрес игроков."
        checked={profanity}
        onChange={setProfanity}
      />

      <fieldset className="form-group">
        <legend className="form-legend">Время на ходы (необязательно)</legend>
        {DURATIONS.map(({ key, label, min, max }, index) => (
          <TextField
            key={key}
            label={label}
            value={durations[key]}
            hint={`От ${min} до ${max}. Пусто — как задано на сервере.`}
            error={errors[index]}
            inputMode="numeric"
            autoComplete="off"
            onChange={(event) =>
              setDurations({ ...durations, [key]: event.target.value })
            }
          />
        ))}
      </fieldset>

      <ErrorAlert error={error} problems={problems} />
      <div className="screen-actions">
        <Button type="submit" busy={busy} disabled={!ready}>
          Создать
        </Button>
        <Button variant="secondary" onClick={onCancel}>
          Отмена
        </Button>
      </div>
    </form>
  );
}

/** Форма новой игры: приключение, нейронка мастера, флажок мата и необязательные длительности. */
export function CreateGameForm({ onCancel }: { onCancel: () => void }) {
  const api = useApi();
  const loadOptions = useCallback(async () => {
    const [adventures, providers] = await Promise.all([
      api.listAdventures(),
      api.listProviders(),
    ]);
    return { adventures, providers: providers.providers };
  }, [api]);
  const [options, reload] = useLoaded(loadOptions);

  return (
    <Card title="Новая игра" icon="scroll">
      {options.status === "loading" && (
        <Loading label="Загружаем приключения и мастеров…" />
      )}
      {options.status === "error" && (
        <div className="screen-stack">
          <Alert>{options.message}</Alert>
          <div className="screen-actions">
            <Button onClick={reload}>Повторить</Button>
            <Button variant="secondary" onClick={onCancel}>
              Отмена
            </Button>
          </div>
        </div>
      )}
      {options.status === "ready" && (
        <OptionsForm
          adventures={options.data.adventures}
          providers={options.data.providers}
          onCancel={onCancel}
        />
      )}
    </Card>
  );
}
