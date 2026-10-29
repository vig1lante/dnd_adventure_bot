import { useEffect, useId, useRef, useState } from "react";
import { setVibrationEnabled, vibrationEnabled } from "../theme/haptics";
import {
  currentHighContrast,
  currentTextSize,
  setHighContrast,
  setTextSize,
} from "../theme/settings";
import type { TextSize } from "../theme/settings";
import { Button } from "./Button";
import { Icon } from "./icons";

const TEXT_SIZES: readonly (readonly [TextSize, string])[] = [
  ["normal", "Обычный"],
  ["large", "Крупный"],
  ["huge", "Очень крупный"],
];

/** Значок настроек (ползунки) в стиле набора src/ui/icons. */
function SlidersGlyph() {
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

export interface SettingsButtonProps {
  id?: string;
  /** Панель открыта. */
  expanded: boolean;
  /** id панели настроек. */
  controls: string;
  onClick: () => void;
}

/** Кнопка «Настройки» в шапке: открывает и закрывает панель настроек. */
export function SettingsButton({
  id,
  expanded,
  controls,
  onClick,
}: SettingsButtonProps) {
  return (
    <Button
      id={id}
      variant="secondary"
      className="ui-settings-button"
      aria-label="Настройки"
      title="Настройки"
      aria-expanded={expanded}
      aria-controls={controls}
      onClick={onClick}
    >
      <SlidersGlyph />
    </Button>
  );
}

interface SettingSwitchProps {
  label: string;
  hint: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}

function SettingSwitch({ label, hint, checked, onChange }: SettingSwitchProps) {
  const hintId = useId();
  return (
    <div className="ui-settings__option">
      <label className="ui-settings__switch">
        <input
          type="checkbox"
          role="switch"
          className="ui-settings__switch-input"
          checked={checked}
          aria-describedby={hintId}
          onChange={(event) => onChange(event.target.checked)}
        />
        <span className="ui-settings__switch-label">{label}</span>
      </label>
      <p id={hintId} className="ui-settings__hint">
        {hint}
      </p>
    </div>
  );
}

export interface SettingsProps {
  /** id панели: на него ссылается кнопка в шапке. */
  id?: string;
  onClose: () => void;
}

/**
 * Настройки интерфейса этого телефона: размер текста, высокий контраст, вибрация. Выбор
 * действует сразу и запоминается (theme/settings, theme/haptics); открывается панель с тем, что
 * действует на странице, даже если хранилище недоступно. «Меньше движения» берётся из
 * системы, звука в приложении нет — переключателей для них нет. При открытии фокус на панели:
 * читалка объявит «Настройки»; Escape и «Готово» закрывают.
 */
export function Settings({ id, onClose }: SettingsProps) {
  const titleId = useId();
  const sizeLabelId = useId();
  const sizeGroup = useId();
  const panel = useRef<HTMLElement>(null);
  // Панель показывает то, что действует на странице, а не то, что успело записаться.
  const [size, setSize] = useState(currentTextSize);
  const [contrast, setContrast] = useState(currentHighContrast);
  const [vibration, setVibration] = useState(vibrationEnabled);

  useEffect(() => panel.current?.focus(), []);

  const pickSize = (next: TextSize) => {
    setSize(next);
    setTextSize(next);
  };

  return (
    <section
      id={id}
      ref={panel}
      tabIndex={-1}
      className="ui-card ui-settings"
      aria-labelledby={titleId}
      onKeyDown={(event) => {
        if (event.key === "Escape") onClose();
      }}
    >
      <div className="ui-section__header">
        <Icon name="candle" />
        <h2 id={titleId} className="ui-section__title">
          Настройки
        </h2>
      </div>
      <div className="ui-settings__body">
        <div
          role="radiogroup"
          aria-labelledby={sizeLabelId}
          className="ui-settings__group"
        >
          <p id={sizeLabelId} className="ui-settings__legend">
            Размер текста
          </p>
          <div className="ui-settings__sizes">
            {TEXT_SIZES.map(([value, label]) => (
              <label key={value} className="ui-settings__size-option">
                <input
                  type="radio"
                  name={sizeGroup}
                  value={value}
                  className="ui-settings__size-input"
                  checked={size === value}
                  onChange={() => pickSize(value)}
                />
                <span>{label}</span>
              </label>
            ))}
          </div>
        </div>
        <div className="ui-settings__sample ui-narration">
          <p>Так будет выглядеть рассказ мастера: ветер воет в трубе маяка.</p>
        </div>
        <SettingSwitch
          label="Высокий контраст"
          hint="Текст ярче, рамки и кнопки чётче. Цвета темы остаются прежними."
          checked={contrast}
          onChange={(on) => {
            setContrast(on);
            setHighContrast(on);
          }}
        />
        <SettingSwitch
          label="Вибрация"
          hint="Коротко, когда твой ход, когда пора сдать ход и на привале. Только в Telegram."
          checked={vibration}
          onChange={(on) => {
            setVibration(on);
            setVibrationEnabled(on);
          }}
        />
        <p className="ui-settings__note">
          Меньше движения — по настройке телефона. Настройки хранятся только на
          этом телефоне.
        </p>
        <div>
          <Button variant="secondary" onClick={onClose}>
            Готово
          </Button>
        </div>
      </div>
    </section>
  );
}
