import { useEffect, useState } from "react";

const COARSE_POINTER = "(pointer: coarse)";
// Поля, которые открывают экранную клавиатуру; флажки, переключатели и кнопки — нет.
const TEXT_FIELD =
  "textarea, input:not([type]), input[type='text'], input[type='search']";
// Касание без щелчка (долгое нажатие): дольше этого состояние не откладываем.
const GESTURE_MS = 1000;

/** Текстовое поле в фокусе или `null`. */
function focusedField(): Element | null {
  const active = document.activeElement;
  return active instanceof Element && active.matches(TEXT_FIELD)
    ? active
    : null;
}

/**
 * Какое текстовое поле открыло экранную клавиатуру телефона: на сенсорном экране — поле в
 * фокусе, иначе `null`. Мышь и настоящая клавиатура (компьютер) — всегда `null`; без
 * matchMedia (старый WebView) — тоже.
 *
 * Пока идёт касание, ответ не меняется: тап по кнопке под полем забирает у поля фокус ещё до
 * того, как палец отпущен. Если в этот миг вернуть нижнюю панель или вывести поле хода из
 * компактного вида, кнопка уедет из-под пальца и нажатие потеряется. Поэтому смена фокуса
 * посреди касания применяется после его конца: щелчка (после обработчиков кнопки), отмены
 * (началась прокрутка) или по сроку, если щелчка так и не было.
 */
export function useOnScreenKeyboardField(): Element | null {
  const [field, setField] = useState<Element | null>(null);
  useEffect(() => {
    if (window.matchMedia?.(COARSE_POINTER).matches !== true) return;
    let touching = false;
    let changed = false;
    let deadline: ReturnType<typeof setTimeout> | undefined;

    const apply = () => {
      changed = false;
      setField(focusedField());
    };
    const onFocusChange = () => {
      if (touching) changed = true;
      else apply();
    };
    const onGestureEnd = () => {
      touching = false;
      clearTimeout(deadline);
      if (changed) apply();
    };
    const onPointerDown = () => {
      touching = true;
      clearTimeout(deadline);
      deadline = setTimeout(onGestureEnd, GESTURE_MS);
    };

    // Нажатие ловим на погружении (раньше смены фокуса), а щелчок — на всплытии: к этому
    // моменту обработчик кнопки уже отработал.
    document.addEventListener("pointerdown", onPointerDown, true);
    document.addEventListener("pointercancel", onGestureEnd, true);
    document.addEventListener("click", onGestureEnd);
    document.addEventListener("focusin", onFocusChange);
    document.addEventListener("focusout", onFocusChange);
    return () => {
      clearTimeout(deadline);
      document.removeEventListener("pointerdown", onPointerDown, true);
      document.removeEventListener("pointercancel", onGestureEnd, true);
      document.removeEventListener("click", onGestureEnd);
      document.removeEventListener("focusin", onFocusChange);
      document.removeEventListener("focusout", onFocusChange);
    };
  }, []);
  return field;
}

/** Открыта ли экранная клавиатура телефона (см. `useOnScreenKeyboardField`). */
export function useOnScreenKeyboard(): boolean {
  return useOnScreenKeyboardField() !== null;
}
