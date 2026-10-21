import { StrictMode, Suspense, lazy } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { wantsStylePreview } from "./preview/gate";
import { startTheme } from "./theme/telegram";
import "./styles/fonts.css";
import "./styles/tokens.css";
import "./styles/theme.css";
import "./styles/global.css";
import "./styles/ui.css";

startTheme();

// Превью стиля есть только в dev: в прод-сборке import.meta.env.DEV становится false,
// и ветка вместе с модулем превью вырезается (проверено по содержимому dist).
const StylePreview = import.meta.env.DEV
  ? lazy(() => import("./preview/StylePreview"))
  : null;

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {StylePreview !== null && wantsStylePreview(window.location.search) ? (
      <Suspense fallback={null}>
        <StylePreview />
      </Suspense>
    ) : (
      <App />
    )}
  </StrictMode>,
);
