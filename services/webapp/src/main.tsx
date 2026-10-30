import { StrictMode, Suspense, lazy } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { wantsScreensPreview, wantsStylePreview } from "./preview/gate";
import { startTheme } from "./theme/telegram";
import "./styles/fonts.css";
import "./styles/tokens.css";
import "./styles/theme.css";
import "./styles/global.css";
import "./styles/ui.css";

startTheme();

// Превью стиля и макеты экранов есть только в dev: в прод-сборке import.meta.env.DEV
// становится false, и ветки вместе с модулями превью вырезаются (проверено по содержимому dist).
const StylePreview = import.meta.env.DEV
  ? lazy(() => import("./preview/StylePreview"))
  : null;
const ScreensPreview = import.meta.env.DEV
  ? lazy(() => import("./preview/ScreensPreview"))
  : null;

/** Страница превью по адресу; `null` — обычное приложение. */
function previewPage(search: string) {
  if (StylePreview !== null && wantsStylePreview(search)) {
    return <StylePreview />;
  }
  if (ScreensPreview !== null && wantsScreensPreview(search)) {
    return <ScreensPreview />;
  }
  return null;
}

const preview = previewPage(window.location.search);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {preview !== null ? (
      <Suspense fallback={null}>{preview}</Suspense>
    ) : (
      <App />
    )}
  </StrictMode>,
);
