import { useState } from "react";
import { createApi } from "../api/client";
import { sessionToken } from "../store/game";
import { applyTheme } from "../theme/telegram";
import type { ThemeName } from "../theme/telegram";
import { Icon } from "../ui";
import { installPreviewSocket, previewFetch } from "./backend";
import { PREVIEW_SCREENS, screensPreviewOf } from "./gate";
import type {
  PreviewScreen,
  PreviewVariant,
  ScreensPreviewParams,
} from "./gate";
import { NextScreen } from "./NextScreens";
import { NowScreen } from "./NowScreens";
import { enterScene, routesFor } from "./scenes";
import { SNAPSHOTS } from "./screenData";
import "./preview.css";
import "./screens-preview.css";

// Макеты B10.1 для владельца: каждый ключевой экран «как есть» (настоящий код на данных
// screenData.ts) и «как будет» (черновики предложений). Без входа и без сервера; попадает только
// в dev-сборку (см. main.tsx), снимки делает скрипт вне репозитория.

const TITLES: Record<PreviewScreen, string> = {
  login: "Вход: нет связи",
  games: "Мои игры",
  "games-loading": "Мои игры: загрузка",
  "games-empty": "Мои игры: пусто",
  "games-error": "Мои игры: ошибка",
  lobby: "Лобби",
  weaving: "Начало игры: мастер думает",
  create: "Создание героя",
  story: "Игра: рассказ (вкладки — вариант В)",
  "story-a": "Игра: рассказ (вкладки — вариант А)",
  thinking: "Игра: мастер думает",
  composer: "Игра: поле хода и клавиатура",
  combat: "Бой",
  "combat-toast": "Бой: «Твой ход», пока читаешь рассказ",
  camp: "Привал",
  journal: "Дневник",
  settings: "Настройки",
  authoring: "Конструктор",
};

const hrefOf = (
  screen: PreviewScreen,
  variant: PreviewVariant,
  theme: ThemeName,
) => `?preview=screens&screen=${screen}&variant=${variant}&theme=${theme}`;

function Contents() {
  return (
    <main className="app preview">
      <header className="app-header">
        <Icon name="d20" size={28} />
        <h1>Макеты экранов: как есть и как будет</h1>
      </header>
      <p className="preview-note">
        «Как есть» — настоящие экраны на выдуманных данных, «как будет» —
        черновики предложений блока «UX/UI». Без входа и без сервера; есть
        только в режиме разработки.
      </p>
      <ul className="preview-contents">
        {PREVIEW_SCREENS.map((screen) => (
          <li key={screen} className="preview-contents__item">
            <span>{TITLES[screen]}</span>
            <span className="preview-contents__links">
              {(["dark", "light"] as const).map((theme) =>
                (["now", "next"] as const).map((variant) => (
                  <a
                    key={theme + variant}
                    href={hrefOf(screen, variant, theme)}
                  >
                    {variant === "now" ? "как есть" : "как будет"}
                    {theme === "light" ? " (светлая)" : ""}
                  </a>
                )),
              )}
            </span>
          </li>
        ))}
      </ul>
    </main>
  );
}

/**
 * Готовит страницу до первого показа экранов: подменяет сокет игры, ставит тему и кладёт сцену
 * в хранилище. Не в эффекте: эффекты экранов (подключение к игре) идут раньше родительских.
 */
function prepare({ screen, theme }: ScreensPreviewParams) {
  installPreviewSocket(SNAPSHOTS);
  applyTheme(theme);
  const fetchImpl = previewFetch(screen === null ? {} : routesFor(screen));
  if (screen !== null) enterScene(screen);
  return { fetchImpl, api: createApi(fetchImpl, sessionToken) };
}

const CONTENTS: ScreensPreviewParams = {
  screen: null,
  variant: "now",
  theme: "dark",
};

export default function ScreensPreview() {
  const [params] = useState(
    () => screensPreviewOf(window.location.search) ?? CONTENTS,
  );
  const [{ fetchImpl, api }] = useState(() => prepare(params));
  const { screen, variant, theme } = params;

  if (screen === null) return <Contents />;
  return variant === "now" ? (
    <NowScreen screen={screen} api={api} fetchImpl={fetchImpl} />
  ) : (
    <NextScreen
      screen={screen}
      api={api}
      settingsHref={hrefOf("settings", "next", theme)}
    />
  );
}
