import type { ReactNode } from "react";
import type { Api } from "../api/client";
import { ApiContext } from "../api/context";
import { SRD_ATTRIBUTION } from "../attribution";
import { Button, Icon } from "../ui";
import { classNames } from "../ui/classNames";

/**
 * Значок настроек (ползунки) в стиле набора src/ui/icons. Черновик: в наборе такого значка нет,
 * настоящий добавит фаза с настройками.
 */
function SettingsGlyph() {
  return (
    <svg
      width={24}
      height={24}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M4 7h9M17 7h3M4 17h3M11 17h9" />
      <circle cx="15" cy="7" r="2" />
      <circle cx="9" cy="17" r="2" />
    </svg>
  );
}

interface PreviewShellProps {
  api: Api;
  /** Кнопка «Настройки» в шапке — предложение (настройки открываются с любого экрана). */
  settingsHref?: string;
  className?: string;
  children: ReactNode;
}

/**
 * Каркас экрана макета — та же разметка, что у App: шапка, экран, атрибуция SRD. Нужен экранам,
 * которые App сам не открывает (редактор приключения, черновики «как будет»).
 */
export function PreviewShell({
  api,
  settingsHref,
  className,
  children,
}: PreviewShellProps) {
  return (
    <ApiContext value={api}>
      <main className={classNames("app", className)}>
        <header className="app-header">
          <Icon name="d20" size={28} />
          <h1>DnD Adventure Bot</h1>
          {settingsHref !== undefined && (
            <Button
              variant="secondary"
              className="preview-header__settings"
              aria-label="Настройки"
              title="Настройки"
              onClick={() => window.location.assign(settingsHref)}
            >
              <SettingsGlyph />
            </Button>
          )}
        </header>
        {children}
        <footer className="app-attribution" lang="en">
          {SRD_ATTRIBUTION}
        </footer>
      </main>
    </ApiContext>
  );
}
