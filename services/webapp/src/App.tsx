import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { ApiError, createApi, errorText } from "./api/client";
import type { Api } from "./api/client";
import { ApiContext } from "./api/context";
import type { DevLogin } from "./api/types";
import { SRD_ATTRIBUTION } from "./attribution";
import { DEV_LOGINS } from "./auth/dev";
import { signInAsDev, startSession } from "./auth/session";
import { readInitData, readStartParam, setupTelegram } from "./auth/telegram";
import { ModePicker } from "./screens/character/ModePicker";
import { Game } from "./screens/Game";
import { Lobby } from "./screens/Lobby";
import { MyGames } from "./screens/MyGames";
import { sessionToken, useGame } from "./store/game";
import { applySavedSettings } from "./theme/settings";
import { watchTelegramTheme } from "./theme/telegram";
import {
  Alert,
  Button,
  Card,
  EmptyState,
  Icon,
  Loading,
  Settings,
  SettingsButton,
} from "./ui";

/** Внешний мир приложения; тесты подменяют его заглушками (сеть, Telegram SDK). */
export interface AppDeps {
  fetchImpl: typeof fetch;
  readInitData: () => string | null;
  readStartParam: () => string | null;
  setupTelegram: () => void;
  /** Следит за темой клиента Telegram; вызывается сразу после `setupTelegram`. */
  watchTelegramTheme: () => void;
}

const REAL_DEPS: AppDeps = {
  fetchImpl: (input, init) => fetch(input, init),
  readInitData,
  readStartParam,
  setupTelegram,
  watchTelegramTheme,
};

type LoginView =
  | { kind: "loading" }
  | { kind: "choose-dev" }
  | { kind: "telegram-required" }
  | { kind: "error"; message: string };

function Login({ api, deps }: { api: Api; deps: AppDeps }) {
  const [view, setView] = useState<LoginView>({ kind: "loading" });
  const started = useRef(false);

  // Ошибки запросов (ApiError) несут понятный текст. Остальное — сбой самого приложения:
  // след в консоли нужен, но без текста ошибки, в котором может оказаться initData.
  const showError = useCallback((error: unknown) => {
    if (!(error instanceof ApiError)) {
      console.error(
        "Неожиданный сбой входа:",
        error instanceof Error ? error.name : typeof error,
      );
    }
    setView({ kind: "error", message: errorText(error) });
  }, []);

  const enter = useCallback(async () => {
    try {
      const outcome = await startSession({
        api,
        initData: deps.readInitData(),
        launchStartParam: deps.readStartParam(),
        search: window.location.search,
      });
      // Вошедший игрок уходит с этого экрана через хранилище.
      if (outcome !== "signed-in") setView({ kind: outcome });
    } catch (error) {
      showError(error);
    }
  }, [api, deps, showError]);

  // Вход один на показ экрана: StrictMode запускает эффект дважды, а второй
  // запрос входа (и приглашения) не нужен.
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    void enter();
  }, [enter]);

  const retry = () => {
    setView({ kind: "loading" });
    void enter();
  };

  const pickDev = async (login: DevLogin) => {
    setView({ kind: "loading" });
    try {
      await signInAsDev({ api, search: window.location.search }, login);
    } catch (error) {
      showError(error);
    }
  };

  switch (view.kind) {
    case "loading":
      return <Loading label="Входим…" />;
    case "telegram-required":
      return (
        <Card>
          <EmptyState icon="candle" title="Откройте приложение из Telegram">
            Игра запускается из чата с ботом. Откройте её оттуда, и мастер
            зажжёт свечи.
          </EmptyState>
        </Card>
      );
    case "choose-dev":
      return (
        <Card title="Выберите игрока" icon="d20">
          <p>
            Режим разработки: каждая вкладка браузера — один друг за столом.
          </p>
          <ul className="login-picker">
            {DEV_LOGINS.map((login) => (
              <li key={login}>
                <Button
                  variant="secondary"
                  block
                  onClick={() => void pickDev(login)}
                >
                  {login}
                </Button>
              </li>
            ))}
          </ul>
        </Card>
      );
    case "error":
      return (
        <Card>
          <Alert>{view.message}</Alert>
          <div className="login-actions">
            <Button onClick={retry}>Повторить</Button>
          </div>
        </Card>
      );
  }
}

function CurrentScreen({ api, deps }: { api: Api; deps: AppDeps }) {
  const screen = useGame((state) => state.screen);
  switch (screen) {
    case "login":
      return <Login api={api} deps={deps} />;
    case "games":
      return <MyGames />;
    case "lobby":
      return <Lobby />;
    case "create":
      return <ModePicker />;
    case "game":
      return <Game />;
  }
}

export default function App({ deps }: { deps?: Partial<AppDeps> }) {
  // Клиент и зависимости создаются один раз: от них зависит эффект входа.
  const [resolved] = useState<AppDeps>(() => ({ ...REAL_DEPS, ...deps }));
  const [api] = useState(() => createApi(resolved.fetchImpl, sessionToken));

  // SDK Telegram запускается один раз за жизнь страницы: повторный init() навешивает
  // слушатели и стили заново, а экран входа может показываться снова (после выхода).
  const telegramStarted = useRef(false);
  useEffect(() => {
    if (telegramStarted.current) return;
    telegramStarted.current = true;
    if (resolved.readInitData() !== null) {
      resolved.setupTelegram();
      resolved.watchTelegramTheme();
    }
  }, [resolved]);

  // Размер текста и контраст этого телефона — до первой отрисовки, чтобы экран не прыгал.
  useLayoutEffect(applySavedSettings, []);

  // Настройки открываются поверх текущего экрана, не заменяя его: игра и её сокет живут.
  const settingsId = useId();
  const settingsButtonId = useId();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const closeSettings = () => {
    setSettingsOpen(false);
    document.getElementById(settingsButtonId)?.focus();
  };

  return (
    <ApiContext value={api}>
      <main className="app">
        <header className="app-header">
          <Icon name="d20" size={28} />
          <h1>DnD Adventure Bot</h1>
          <SettingsButton
            id={settingsButtonId}
            expanded={settingsOpen}
            controls={settingsId}
            onClick={() =>
              settingsOpen ? closeSettings() : setSettingsOpen(true)
            }
          />
        </header>
        {settingsOpen && <Settings id={settingsId} onClose={closeSettings} />}
        <CurrentScreen api={api} deps={resolved} />
        {/* Лицензия SRD требует показать атрибуцию в приложении; английский текст помечен lang. */}
        <footer className="app-attribution" lang="en">
          {SRD_ATTRIBUTION}
        </footer>
      </main>
    </ApiContext>
  );
}
